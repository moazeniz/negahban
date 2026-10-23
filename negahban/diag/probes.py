"""Outbound probes: HTTP(S) with a timing breakdown (DNS, connect, TLS, TTFB, total), redirect chain, TLS certificate,
body check; DNS lookups (DoH + plain UDP + system resolver, reachable from Iran); a bounded load test (sensitive).
Probes only target public addresses (SSRF guard); the load test only targets the team's own apps."""
import asyncio, os, socket, ssl, statistics, tempfile, time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
import httpx
from . import dnswire
from .common import DiagError, audit, check_public_url, spec, text

UA = 'negahban-probe/2.0'
KEEP_HEADERS = ('server', 'content-type', 'content-length', 'cache-control', 'location', 'x-request-id', 'retry-after', 'strict-transport-security',
                'x-powered-by', 'via', 'age', 'date', 'content-encoding', 'x-cache', 'cf-ray', 'ar-cache', 'x-liara-cache')
MAX_BODY = 512 * 1024
_transport = None  # test hook (httpx transport)


def _ms(a, b):
    return round((b - a) * 1000, 1) if a is not None and b is not None else None


def _cert_info(c):
    """ssl.getpeercert() dict -> compact summary with days_left."""
    if not c:
        return None
    flat = lambda seq: {k: v for t in seq or () for k, v in t}
    exp = datetime.strptime(c['notAfter'], '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc) if c.get('notAfter') else None
    start = datetime.strptime(c['notBefore'], '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc) if c.get('notBefore') else None
    days = round((exp - datetime.now(timezone.utc)).total_seconds() / 86400, 1) if exp else None
    iss = flat(c.get('issuer'))
    return {'subject_cn': flat(c.get('subject')).get('commonName'), 'issuer': iss.get('organizationName') or iss.get('commonName'), 'issuer_cn': iss.get('commonName'),
            'not_before': start.isoformat() if start else None, 'not_after': exp.isoformat() if exp else None, 'days_left': days,
            'sans': [v for k, v in c.get('subjectAltName', ()) if k == 'DNS'][:30], 'serial': c.get('serialNumber')}


def _decode_der(der):
    """Decode a certificate we could not verify (CPython's internal decoder needs a PEM file)."""
    with tempfile.NamedTemporaryFile('w', suffix='.pem', delete=False) as f:
        f.write(ssl.DER_cert_to_PEM_cert(der))
        p = f.name
    try:
        return ssl._ssl._test_decode_cert(p)
    except Exception:
        return None
    finally:
        os.unlink(p)


async def tls_check(host, port=443, timeout=10):
    """Handshake with verification; on failure retry unverified to still report expiry/issuer/SANs and the verify error."""
    out = {'host': host, 'port': port}
    for verify in (True, False):
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        t0 = time.perf_counter()
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), timeout)
        except ssl.SSLCertVerificationError as e:
            out['verified'], out['verify_error'] = False, (e.verify_message or str(e))[:200]
            continue
        except (OSError, asyncio.TimeoutError, ssl.SSLError) as e:
            out.update(ok=False, error=f'{type(e).__name__}: {str(e)[:160]}')
            return out
        so = w.get_extra_info('ssl_object')
        cert = so.getpeercert() if verify else _decode_der(so.getpeercert(binary_form=True))
        out.update(ok=verify, handshake_ms=_ms(t0, time.perf_counter()), tls_version=so.version(), cipher=(so.cipher() or [None])[0], cert=_cert_info(cert))
        if verify:
            out['verified'] = True
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return out
    return out


async def _hop(client, method, url, headers):
    marks = {}

    async def trace(name, info):
        marks[name] = time.perf_counter()
        if name == 'connection.start_tls.complete' and info.get('return_value') is not None:
            so = info['return_value'].get_extra_info('ssl_object')
            if so:
                marks['_tls'] = (so.version(), so.getpeercert())
    t_dns0 = time.perf_counter()
    host, port, ips = await check_public_url(url)  # resolves every address (SSRF guard) and times DNS
    t_dns1 = time.perf_counter()
    t0 = time.perf_counter()
    body, size = b'', 0
    async with client.stream(method, url, headers=headers, extensions={'trace': trace}) as r:
        t_head = time.perf_counter()
        async for chunk in r.aiter_bytes():
            size += len(chunk)
            if len(body) < MAX_BODY:
                body += chunk[:MAX_BODY - len(body)]
            if size > 8 * MAX_BODY:
                break
        t_end = time.perf_counter()
    g = marks.get
    tls = marks.get('_tls')
    return r, body, size, {
        'ips': ips, 'dns_ms': _ms(t_dns0, t_dns1), 'connect_ms': _ms(g('connection.connect_tcp.started'), g('connection.connect_tcp.complete')),
        'tls_ms': _ms(g('connection.start_tls.started'), g('connection.start_tls.complete')),
        'ttfb_ms': _ms(g('http11.send_request_headers.started') or g('http2.send_request_headers.started') or t0, g('http11.receive_response_headers.complete') or g('http2.receive_response_headers.complete') or t_head),
        'download_ms': _ms(t_head, t_end), 'total_ms': _ms(t0, t_end) + _ms(t_dns0, t_dns1)}, tls


async def http_probe(url, method='GET', expect_status=None, expect_text=None, timeout=15, max_redirects=5, headers=None):
    if method not in ('GET', 'HEAD'):
        raise DiagError('only GET and HEAD probes are allowed')
    hdr = {'User-Agent': UA, **(headers or {})}
    chain, cur, tls, final = [], url, None, None
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 10)), follow_redirects=False, transport=_transport, verify=True) as client:
        for _ in range(max_redirects + 1):
            try:
                r, body, size, tm, t = await _hop(client, method, cur, hdr)
            except DiagError:
                raise
            except httpx.ConnectError as e:
                err = str(e)
                kind = 'tls_verify_failed' if 'CERTIFICATE_VERIFY_FAILED' in err else 'connect_failed'
                chain.append({'url': cur, 'ok': False, 'error': kind, 'detail': text(err, 300)})
                if kind == 'tls_verify_failed':
                    u = urlsplit(cur)
                    chain[-1]['tls'] = await tls_check(u.hostname, u.port or 443)
                break
            except (httpx.TimeoutException, httpx.HTTPError, OSError) as e:
                chain.append({'url': cur, 'ok': False, 'error': type(e).__name__, 'detail': text(str(e), 300)})
                break
            tls = t or tls
            hop = {'url': cur, 'status': r.status_code, 'http_version': r.http_version, **tm,
                   'headers': {k: v for k, v in r.headers.items() if k.lower() in KEEP_HEADERS}}
            chain.append(hop)
            loc = r.headers.get('location')
            if r.status_code in (301, 302, 303, 307, 308) and loc:
                cur = urljoin(cur, loc)
                continue
            final = {'status': r.status_code, 'bytes': size, 'body': body}
            break
        else:
            chain.append({'error': f'more than {max_redirects} redirects'})
    out = {'url': url, 'redirects': max(0, len(chain) - 1), 'chain': chain}
    if final:
        last = chain[-1]
        txt = final['body'].decode('utf-8', 'replace')
        out.update(status=final['status'], bytes=final['bytes'], timing={k: last.get(k) for k in ('dns_ms', 'connect_ms', 'tls_ms', 'ttfb_ms', 'download_ms', 'total_ms')},
                   total_ms=round(sum((h.get('total_ms') or 0) for h in chain if 'status' in h), 1), body_snippet=text(txt[:400], 400))
        ok = final['status'] < 400
        if expect_status:
            out['status_ok'] = final['status'] == int(expect_status)
            ok = out['status_ok']
        if expect_text:
            out['text_found'] = expect_text in txt
            ok = ok and out['text_found']
        out['ok'] = ok
    else:
        out['ok'] = False
    if tls:
        out['tls'] = {'version': tls[0], 'cert': _cert_info(tls[1])}
    return out


# ---------------------------------------------------------------- DNS
DOH = [u.strip() for u in os.environ.get('DIAG_DOH_URLS', 'https://cloudflare-dns.com/dns-query,https://dns.google/dns-query').split(',') if u.strip()]
UDP = [u.strip() for u in os.environ.get('DIAG_DNS_SERVERS', '8.8.8.8,1.1.1.1,178.22.122.100,185.51.200.2').split(',') if u.strip()]
_udp_send = None  # test hook: async (server, payload) -> bytes


async def _doh(url, name, rtype, timeout=4):
    async with httpx.AsyncClient(timeout=timeout, transport=_transport) as c:
        r = await c.post(url, content=dnswire.build_query(name, rtype), headers={'Content-Type': 'application/dns-message', 'Accept': 'application/dns-message'})
        r.raise_for_status()
        return dnswire.parse_response(r.content)


async def _udp(server, name, rtype, timeout=3):
    q = dnswire.build_query(name, rtype)
    if _udp_send:
        return dnswire.parse_response(await _udp_send(server, q))
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not fut.done():
                fut.set_result(data)

        def error_received(self, exc):
            if not fut.done():
                fut.set_exception(exc)
    tr, _ = await loop.create_datagram_endpoint(P, remote_addr=(server, 53))
    try:
        tr.sendto(q)
        return dnswire.parse_response(await asyncio.wait_for(fut, timeout))
    finally:
        tr.close()


async def resolve(name, rtype, resolver='auto'):
    """One record type. resolver: auto (first answer among DoH+UDP), a DoH URL, a UDP server IP, or 'system' (A/AAAA only)."""
    if rtype not in dnswire.TYPES:
        raise DiagError(f'unsupported record type {rtype}')
    if resolver == 'system':
        fam = socket.AF_INET6 if rtype == 'AAAA' else socket.AF_INET
        try:
            infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(name, None, family=fam, type=socket.SOCK_STREAM), 6)
            return {'resolver': 'system', 'rcode': 'NOERROR', 'answers': [{'type': rtype, 'data': d} for d in sorted({i[4][0] for i in infos})]}
        except (OSError, asyncio.TimeoutError) as e:
            return {'resolver': 'system', 'error': type(e).__name__}
    targets = [resolver] if resolver != 'auto' else [*DOH, *UDP]
    async def one(t):
        t0 = time.perf_counter()
        r = await (_doh(t, name, rtype) if t.startswith('https://') else _udp(t, name, rtype))
        return {'resolver': t, 'ms': _ms(t0, time.perf_counter()), **r}
    tasks = [asyncio.ensure_future(one(t)) for t in targets]
    errors = {}
    try:
        for fut in asyncio.as_completed(tasks):
            try:
                return await fut
            except Exception as e:
                errors[len(errors)] = type(e).__name__
    finally:
        for t in tasks:
            t.cancel()
    return {'resolver': resolver, 'error': 'no resolver answered', 'errors': list(errors.values())}


async def dns_lookup(name, types=('A', 'AAAA', 'CNAME', 'TXT'), resolver='auto', compare=False):
    name = name.strip().rstrip('.').lower()
    if not name or len(name) > 253 or any(not l or len(l) > 63 for l in name.split('.')):
        raise DiagError('invalid domain name')
    res = await asyncio.gather(*[resolve(name, t, resolver) for t in types])
    out = {'name': name, 'records': {t: ([a['data'] for a in r.get('answers', []) if a['type'] == t] if 'error' not in r else None) for t, r in zip(types, res)},
           'rcode': {t: r.get('rcode') or r.get('error') for t, r in zip(types, res)}, 'via': {t: r.get('resolver') for t, r in zip(types, res)},
           'cname_chain': sorted({a['data'] for r in res for a in r.get('answers', []) if a['type'] == 'CNAME'}),
           'ttl': {t: min((a['ttl'] for a in r.get('answers', []) if 'ttl' in a), default=None) for t, r in zip(types, res)}}
    if compare:
        rows = await asyncio.gather(*[resolve(name, 'A', s) for s in [*DOH, *UDP, 'system']])
        out['compare_A'] = {r['resolver']: sorted(a['data'] for a in r.get('answers', []) if a['type'] == 'A') if 'error' not in r else r['error'] for r in rows}
        vals = [tuple(v) for v in out['compare_A'].values() if isinstance(v, list) and v]
        out['consistent'] = len(set(vals)) <= 1
    return out


# ---------------------------------------------------------------- load test
def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


async def load_test(url, requests=30, concurrency=5, timeout=15):
    requests, concurrency = max(1, min(int(requests), 50)), max(1, min(int(concurrency), 5))
    await check_public_url(url)
    lat, codes, errors = [], {}, {}
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=_transport, headers={'User-Agent': UA + ' load'},
                                 limits=httpx.Limits(max_connections=concurrency)) as c:
        async def one():
            async with sem:
                t0 = time.perf_counter()
                try:
                    r = await c.get(url)
                    await r.aread()
                    lat.append((time.perf_counter() - t0) * 1000)
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                except Exception as e:
                    errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
        t0 = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(requests)])
        wall = time.perf_counter() - t0
    bad = sum(v for k, v in codes.items() if k >= 500) + sum(errors.values())
    return {'url': url, 'requests': requests, 'concurrency': concurrency, 'wall_s': round(wall, 2), 'rps': round(requests / wall, 1) if wall else None,
            'p50_ms': _pct(lat, 50), 'p95_ms': _pct(lat, 95), 'p99_ms': _pct(lat, 99), 'max_ms': round(max(lat), 1) if lat else None,
            'mean_ms': round(statistics.mean(lat), 1) if lat else None, 'status_counts': codes, 'errors': errors, 'error_rate': round(bad / requests, 3)}


def _team_url(app, path):
    if not path.startswith('/') or '..' in path:
        raise DiagError('path must start with / and not contain ..')
    reg = None
    try:
        from .. import store
        reg = store.get_app(app)
    except Exception:
        pass
    return ((reg or {}).get('url') or f'https://{app}.liara.run').rstrip('/') + path


# ---------------------------------------------------------------- tools
@spec('http_probe', 'در حال بررسی HTTP و زمان‌بندی پاسخ…', 'read',
      'Probe a public URL (or an app path) with a timing breakdown (DNS, TCP connect, TLS, TTFB, download, total), status, key headers, the '
      'full redirect chain, the TLS certificate (issuer, expiry, days_left, SANs) and an optional body check. GET/HEAD only.',
      {'url': {'type': 'string', 'description': 'full https URL; or give app (+path)'}, 'app': {'type': 'string'}, 'path': {'type': 'string'},
       'method': {'type': 'string', 'enum': ['GET', 'HEAD']}, 'expect_status': {'type': 'integer', 'minimum': 100, 'maximum': 599},
       'expect_text': {'type': 'string', 'maxLength': 200}}, timeout=40)
async def http_probe_tool(a, ctx):
    url = a.get('url') or (_team_url(a['app'], a.get('path') or '/') if a.get('app') else None)
    if not url:
        raise DiagError('give url or app')
    return await http_probe(url, a.get('method') or 'GET', a.get('expect_status'), a.get('expect_text'))


@spec('tls_check', 'در حال بررسی گواهی TLS…', 'read',
      'Check the TLS certificate of a host: verification result (and the exact verify error), issuer, expiry date, days_left, SANs, TLS version.',
      {'host': {'type': 'string', 'pattern': r'^[A-Za-z0-9.-]{3,253}$'}, 'port': {'type': 'integer', 'minimum': 1, 'maximum': 65535}}, ['host'], timeout=30)
async def tls_check_tool(a, ctx):
    await check_public_url(f"https://{a['host']}:{a.get('port') or 443}/")
    return await tls_check(a['host'], a.get('port') or 443)


@spec('dns_lookup', 'در حال بررسی رکوردهای DNS…', 'read',
      'Resolve DNS records (A, AAAA, CNAME, TXT, MX, NS, CAA, SOA) through DoH and plain DNS resolvers that work from Iran. '
      'compare=true shows the A answer of every resolver (propagation / split-horizon check).',
      {'name': {'type': 'string', 'maxLength': 253}, 'types': {'type': 'array', 'items': {'type': 'string', 'enum': list(dnswire.TYPES)}, 'maxItems': 9},
       'compare': {'type': 'boolean'}}, ['name'], timeout=30)
async def dns_lookup_tool(a, ctx):
    return await dns_lookup(a['name'], tuple(a.get('types') or ('A', 'AAAA', 'CNAME', 'TXT')), compare=bool(a.get('compare')))


def _lt_title(a):
    return f"تست بار {a.get('app')}{a.get('path') or '/'} ({min(int(a.get('requests') or 30), 50)} درخواست)"


async def _lt_impact(a):
    n, c = min(int(a.get('requests') or 30), 50), min(int(a.get('concurrency') or 5), 5)
    return ({'downtime': 'ندارد', 'cost': 'بدون هزینه‌ی مستقیم', 'risk': 'کم: حداکثر ۵۰ درخواست با همزمانی ۵', 'requests': n, 'concurrency': c,
             'target': _team_url(a['app'], a.get('path') or '/')}, 'نیازی به بازگشت نیست؛ تست فقط درخواست GET می‌فرستد.')


@spec('load_test', 'ساخت کارت تأیید برای تست بار…', 'sensitive',
      'Bounded load test of one of the team apps (max 50 GET requests, concurrency <= 5): p50/p95/p99 latency, status counts, errors, rps.',
      {'app': {'type': 'string'}, 'path': {'type': 'string'}, 'requests': {'type': 'integer', 'minimum': 1, 'maximum': 50},
       'concurrency': {'type': 'integer', 'minimum': 1, 'maximum': 5}, 'reason': {'type': 'string'}}, ['app'],
      title=_lt_title, impact=_lt_impact, timeout=120)
async def load_test_tool(a, ctx):
    url = _team_url(a['app'], a.get('path') or '/')
    r = await load_test(url, a.get('requests') or 30, a.get('concurrency') or 5)
    audit(ctx, 'load_test', a['app'], {'url': url, 'requests': r['requests'], 'p95_ms': r['p95_ms'], 'error_rate': r['error_rate']})
    return r
