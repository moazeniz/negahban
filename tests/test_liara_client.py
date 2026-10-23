import asyncio, json
import httpx
import pytest
from negahban.liara import Liara, LiaraError, make_tarball


def run(c):
    return asyncio.run(c)


def client(handler):
    return Liara(token='tok-xyz-123456', team='team1', base='https://api.test', transport=httpx.MockTransport(handler))


def test_team_id_and_bearer_on_every_call():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={'projects': [{'project_id': 'a'}]})
    assert run(client(h).list_apps()) == [{'project_id': 'a'}]
    assert seen[0].url.params['teamID'] == 'team1' and seen[0].headers['authorization'] == 'Bearer tok-xyz-123456'


def test_get_retries_on_5xx_then_succeeds():
    n = {'c': 0}

    def h(req):
        n['c'] += 1
        return httpx.Response(502) if n['c'] < 2 else httpx.Response(200, json={'project': {'planID': 'small-g2'}})
    assert run(client(h).app('x'))['planID'] == 'small-g2' and n['c'] == 2


def test_post_not_retried_and_error_mapped():
    n = {'c': 0}

    def h(req):
        n['c'] += 1
        return httpx.Response(428, json={'message': 'limit', 'data': {'code': 'max_deployment_count_in_day'}})
    with pytest.raises(LiaraError) as e:
        run(client(h).restart('x'))
    assert n['c'] == 1 and e.value.status == 428 and 'max_deployment_count_in_day' in e.value.message and 'محدودیت' in e.value.fa()


def test_logs_flattened_sorted_and_paged():
    calls = []

    def h(req):
        calls.append(dict(req.url.params))
        if len(calls) == 1:  # newest 100 lines, two groups, unsorted
            vals = [[str((1000 + i) * 10 ** 9), json.dumps({'_entry': f'l{i}', 'type': 'stdout'})] for i in range(100, 0, -1)]
            return httpx.Response(200, json={'data': [{'metaData': {'releaseId': 'r1'}, 'values': vals[:50]}, {'metaData': {'releaseId': 'r2'}, 'values': vals[50:]}]})
        return httpx.Response(200, json={'data': [{'metaData': {'releaseId': 'r1'}, 'values': [[str(990 * 10 ** 9), json.dumps({'_entry': 'old', 'type': 'stderr'})]]}]})
    out = run(client(h).logs('app', start=900, end=1200))
    assert [l['line'] for l in out[:2]] == ['old', 'l1'] and out[-1]['line'] == 'l100' and len(out) == 101
    assert calls[1]['end'] == '1002'  # second page re-includes the boundary second of page one (dedup by `seen`)


def test_metrics_sum_across_applets():
    def h(req):
        return httpx.Response(200, json={'result': [{'applet': 'a', 'values': [[10, '0.1'], [40, '0.2']]}, {'applet': 'b', 'values': [[10, '0.3']]}]})
    assert run(client(h).metrics('x', 'cpu', 0)) == [(10, pytest.approx(0.4)), (40, 0.2)]


def test_events_time_normalised():
    def h(req):
        return httpx.Response(200, json={'data': [{'action': 'start', 'time': '20', 'release': 'r'}, {'action': 'oom', 'time': '10', 'release': 'r'}]})
    ev = run(client(h).events('x', 0, 30))
    assert [e['action'] for e in ev] == ['oom', 'start'] and ev[1]['time'] == 20


def test_tarball_skips_secrets_and_vcs(tmp_path):
    (tmp_path / 'a.py').write_text('x'); (tmp_path / '.env').write_text('SECRET=1'); (tmp_path / '.git').mkdir(); (tmp_path / '.git' / 'c').write_text('x')
    import io, tarfile
    names = tarfile.open(fileobj=io.BytesIO(make_tarball(tmp_path))).getnames()
    assert names == ['a.py']
    assert make_tarball(tmp_path) == make_tarball(tmp_path)  # deterministic


def test_create_app_uses_team_network_or_creates_one(monkeypatch):
    # verified live in the empty test team: '"network" is not allowed to be empty', and POST /v1/networks needs networkName
    from negahban.config import settings
    monkeypatch.setattr(settings, 'liara_network', '')
    nets, seen = [], []

    def h(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path == '/v1/networks' and req.method == 'GET':
            return httpx.Response(200, json={'networks': nets})
        if req.url.path == '/v1/networks':
            nets.append({'_id': 'net1', 'name': 'negahban-net'})
            return httpx.Response(201, json={})
        return httpx.Response(201, json={'ok': True})
    run(client(h).create_app('a1', 'small-g2'))
    assert ('POST', '/v1/networks', {'networkName': 'negahban-net'}) in seen
    assert seen[-1] == ('POST', '/v1/projects', {'name': 'a1', 'planID': 'small-g2', 'bundlePlanID': 'basic', 'platform': 'docker', 'network': 'net1'})
