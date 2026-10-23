import asyncio, struct
import httpx
import pytest
from negahban.diag import common, dnswire, probes
from negahban.diag.common import DiagError


def run(c):
    return asyncio.run(c)


@pytest.fixture
def public(monkeypatch):
    async def fake_check(url, allow_private=False):
        from urllib.parse import urlsplit
        u = urlsplit(url)
        return u.hostname, u.port or 443, ['185.208.181.133']
    monkeypatch.setattr(probes, 'check_public_url', fake_check)


def answer_packet(qid, name, records):
    """Build a DNS response with name compression (pointer to the question name at offset 12)."""
    q = dnswire.build_query(name, 'A', qid)
    hdr = struct.pack('>HHHHHH', qid, 0x8180, 1, len(records), 0, 0)
    body = q[12:]
    for rtype, rdata in records:
        body += b'\xc0\x0c' + struct.pack('>HHIH', dnswire.TYPES[rtype], 1, 300, len(rdata)) + rdata
    return hdr + body


def test_dnswire_roundtrip_a_txt_cname():
    pkt = answer_packet(7, 'shop.example.ir', [('CNAME', b'\x05cname\xc0\x0c'), ('A', bytes([185, 208, 181, 133])), ('TXT', b'\x0bv=spf1 -all')])
    r = dnswire.parse_response(pkt)
    assert r['id'] == 7 and r['rcode'] == 'NOERROR'
    assert [a['data'] for a in r['answers']] == ['cname.shop.example.ir.', '185.208.181.133', 'v=spf1 -all']
    with pytest.raises(ValueError):
        dnswire.build_query('a' * 70 + '.ir', 'A')


def test_resolve_udp_hook_and_compare(monkeypatch):
    async def send(server, q):
        qid = struct.unpack('>H', q[:2])[0]
        return answer_packet(qid, 'demo-shop.liara.run', [('A', bytes([185, 208, 181, 133]))])
    monkeypatch.setattr(probes, '_udp_send', send)
    monkeypatch.setattr(probes, 'DOH', [])
    monkeypatch.setattr(probes, 'UDP', ['8.8.8.8'])
    r = run(probes.dns_lookup('demo-shop.liara.run', ('A',)))
    assert r['records']['A'] == ['185.208.181.133'] and r['via']['A'] == '8.8.8.8'
    with pytest.raises(DiagError):
        run(probes.dns_lookup('bad..name'))


def test_check_public_url_blocks_internal_targets():
    for url in ('http://127.0.0.1:8080/', 'http://localhost/', 'ftp://example.com/', 'http://user:pw@example.com/'):
        with pytest.raises(DiagError):
            run(common.check_public_url(url))


def test_http_probe_redirect_chain_body_check(monkeypatch, public):
    def h(req):
        if req.url.path == '/':
            return httpx.Response(301, headers={'location': '/home', 'server': 'Liara'})
        return httpx.Response(200, text='<h1>فروشگاه</h1> ok', headers={'content-type': 'text/html', 'set-cookie': 'secret=1'})
    monkeypatch.setattr(probes, '_transport', httpx.MockTransport(h))
    r = run(probes.http_probe('https://demo-shop.liara.run/', expect_text='فروشگاه', expect_status=200))
    assert r['ok'] and r['status'] == 200 and r['redirects'] == 1 and r['chain'][0]['status'] == 301
    assert r['chain'][1]['url'] == 'https://demo-shop.liara.run/home' and 'set-cookie' not in r['chain'][1]['headers']
    assert set(r['timing']) >= {'dns_ms', 'connect_ms', 'tls_ms', 'ttfb_ms', 'total_ms'} and r['text_found']
    r = run(probes.http_probe('https://demo-shop.liara.run/home', expect_text='missing'))
    assert not r['ok'] and r['text_found'] is False


def test_http_probe_connect_error_and_method_guard(monkeypatch, public):
    def h(req):
        raise httpx.ConnectError('boom')
    monkeypatch.setattr(probes, '_transport', httpx.MockTransport(h))
    r = run(probes.http_probe('https://down.example.ir/'))
    assert not r['ok'] and r['chain'][0]['error'] == 'connect_failed'
    with pytest.raises(DiagError):
        run(probes.http_probe('https://x.ir/', method='POST'))


def test_cert_info_days_left():
    c = {'subject': ((('commonName', 'liara.run'),),), 'issuer': ((('organizationName', "Let's Encrypt"),), (('commonName', 'YE1'),)),
         'notBefore': 'Sep 21 08:15:01 2026 GMT', 'notAfter': 'Dec 20 08:15:00 2099 GMT', 'subjectAltName': (('DNS', '*.liara.run'), ('DNS', 'liara.run'))}
    i = probes._cert_info(c)
    assert i['issuer'] == "Let's Encrypt" and i['sans'] == ['*.liara.run', 'liara.run'] and i['days_left'] > 1000


def test_load_test_bounds_and_percentiles(monkeypatch, public):
    n = {'c': 0}

    def h(req):
        n['c'] += 1
        return httpx.Response(500 if n['c'] % 10 == 0 else 200, text='x')
    monkeypatch.setattr(probes, '_transport', httpx.MockTransport(h))
    r = run(probes.load_test('https://demo-shop.liara.run/', requests=500, concurrency=50))
    assert r['requests'] == 50 and r['concurrency'] == 5 and n['c'] == 50
    assert r['status_counts'] == {200: 45, 500: 5} and r['error_rate'] == 0.1 and r['p50_ms'] is not None and r['p99_ms'] >= r['p50_ms']
    assert probes._pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5.5


def test_load_test_targets_only_team_apps(fresh_db):
    assert probes._team_url('demo-shop', '/x') == 'https://demo-shop.liara.run/x'
    with pytest.raises(DiagError):
        probes._team_url('demo-shop', '/../etc')
    imp, rb = run(probes._lt_impact({'app': 'demo-shop', 'requests': 99}))
    assert imp['requests'] == 50 and imp['target'].startswith('https://demo-shop.liara.run')
