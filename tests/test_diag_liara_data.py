import asyncio
import httpx
import pytest
from negahban.liara import Liara
from negahban.diag import liara_data as ld
from negahban.diag.common import DiagError


def run(c):
    return asyncio.run(c)


@pytest.fixture
def api(monkeypatch):
    """Route table {path: json | callable(req)} served by a mocked core Liara client."""
    routes, seen = {}, []

    def h(req):
        seen.append(req)
        key = f'{req.method} {req.url.host}{req.url.path}'
        r = routes.get(key) or routes.get(f'{req.method} {req.url.path}')
        if r is None:
            return httpx.Response(404, json={'message': 'not found'})
        r = r(req) if callable(r) else r
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)
    cl = Liara(token='tok-xyz-123456', team='team1', base='https://api.test', transport=httpx.MockTransport(h))
    monkeypatch.setattr(ld, 'liara', cl)
    monkeypatch.setattr(ld, '_ids', {})
    return routes, seen


def test_release_details_pages_build_logs(api):
    routes, seen = api
    routes['GET /v1/projects/demo-shop/releases'] = {'currentRelease': 'r2', 'releases': [
        {'_id': 'r2', 'tag': 'v2', 'type': 'DEPLOY', 'state': 'FAILED', 'createdAt': '2026-10-22T10:00:00Z', 'finishedAt': '2026-10-22T10:01:30Z'}]}
    routes['GET /v1/releases/r2'] = {'release': {'state': 'FAILED', 'failReason': 'build_failed'}}
    pages = {'1': [{'line': 'Step 1/3 : FROM python', 'stream': 'STDOUT', 'createdAt': 'a'}, {'line': 'ERROR: No matching distribution found for fastapi==99', 'stream': 'STDERR', 'createdAt': 'b'}],
             'b': [{'line': 'tail line', 'stream': 'STDOUT', 'createdAt': 'c'}], 'c': []}
    routes['GET /v2/releases/r2/build-logs'] = lambda req: {'release': {'state': 'FAILED'}, 'buildOutput': pages[req.url.params['since']]}
    r = run(ld.release_details('demo-shop'))
    assert r['state'] == 'FAILED' and r['fail_reason'] == 'build_failed' and r['duration_s'] == 90 and r['is_current']
    assert r['build_lines'] == 3 and r['stderr_lines'] == ['ERROR: No matching distribution found for fastapi==99']
    assert r['build_summary']['missing_package'] == 'fastapi==99'
    assert all(q.url.params['teamID'] == 'team1' for q in seen)


def test_events_timeline_counts(api):
    routes, _ = api
    routes['GET /v1/projects/demo-shop/events'] = {'data': [
        {'action': 'start', 'time': '100', 'release': 'r1', 'tag': 'v1'}, {'action': 'oom', 'time': '200', 'release': 'r1'}, {'action': 'die', 'time': '201', 'release': 'r1'},
        {'action': 'start', 'time': '205', 'release': 'r1', 'tag': 'v1'}, {'action': 'exec_start: cat /proc/loadavg', 'time': '300'}, {'action': 'exec_die', 'time': '301'}]}
    r = run(ld.events_timeline('demo-shop', 24))
    assert r['oom'] == 1 and r['crashes'] == 1 and r['unplanned_restarts'] == 1 and r['exec_sessions'] == 2 and len(r['events']) == 4


def test_disks_backups_and_file_browser(api, monkeypatch):
    routes, seen = api
    routes['GET /v1/projects/bazargard-ai'] = {'project': {'_id': 'P1', 'envs': [{'key': 'SECRET', 'value': 'x'}]}}
    routes['GET /v1/projects/P1/disks'] = {'disks': [{'name': 'data', 'size': 3, 'filebrowserUrl': 'https://fb.test'}], 'mounts': [{'name': 'data', 'mountedTo': '/data'}]}
    routes['GET /v1/projects/P1/disks/data/metrics'] = {'result': {'size': '3221225472', 'usage': '15', 'reportedAt': 1792687274}}
    routes['GET /v1/projects/P1/disks/data/backups'] = {'backups': [{'name': 'b1', 'lastModified': '2026-10-22', 'size': 1048576}]}
    routes['GET fb.test/v1/projects/P1/disks/data/list'] = lambda req: {'items': [{'name': 'app.log', 'path': './logs/app.log', 'size': 2048, 'isDir': False}], 'numFiles': 1, 'path': '/logs'} \
        if req.url.params['path'] == './logs' else httpx.Response(400)
    routes['POST fb.test/v1/projects/P1/disks/data/createlink'] = lambda req: {'link': 'https://fb.test/serve?path=' + req.url.params['path'] + '&token=abc'}
    d = run(ld.disks('bazargard-ai'))
    assert d['disks'][0]['usage_pct'] == 15.0 and d['disks'][0]['mounted_to'] == '/data' and d['disks'][0]['backup_count'] == 1
    assert 'SECRET' not in str(d)
    lst = run(ld.list_files('bazargard-ai', 'data', '/logs'))
    assert lst['items'][0]['name'] == 'app.log' and lst['items'][0]['kb'] == 2.0
    body = {'./logs/app.log': b'line1\nAuthorization: Bearer abcdefghijklmnopqrstu\n', './x.bin': b'\x00\x01\x02'}
    monkeypatch.setattr(ld, '_transport', httpx.MockTransport(lambda req: httpx.Response(200, content=body[req.url.params['path']])))
    f = run(ld.read_file('bazargard-ai', 'data', '/logs/app.log'))
    assert 'line1' in f['content'] and 'abcdefghijklmnopqrstu' not in f['content']
    with pytest.raises(DiagError, match='binary'):
        run(ld.read_file('bazargard-ai', 'data', '/x.bin'))
    with pytest.raises(DiagError, match='secret'):
        run(ld.read_file('bazargard-ai', 'data', '/.env'))
    with pytest.raises(DiagError):
        run(ld.read_file('bazargard-ai', 'data', '/../etc/passwd'))


@pytest.mark.parametrize('q', ['SELECT * FROM users LIMIT 5', 'select count(*) from orders where note = \'drop table x\'', 'EXPLAIN SELECT 1',
                               'WITH a AS (SELECT 1) SELECT * FROM a', 'show tables', 'SELECT 1;'])
def test_readonly_sql_ok(q):
    assert ld.check_readonly_sql(q)


@pytest.mark.parametrize('q', ['DELETE FROM users', 'select 1; drop table users', 'UPDATE t SET a=1', 'insert into t values(1)', 'SELECT pg_sleep(10)',
                               'WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x', 'select * into outfile \'/tmp/x\' from t', 'truncate t', 'COPY t TO \'/tmp/x\''])
def test_readonly_sql_rejected(q):
    with pytest.raises(DiagError):
        ld.check_readonly_sql(q)


def test_db_query_uses_inspector(api):
    routes, seen = api
    routes['GET /v1/databases/D1'] = {'database': {'_id': 'D1', 'databaseInspectorUrl': 'https://insp.test', 'defaultDatabase': 'shop'}}
    routes['POST insp.test/databases/D1/v1/shop/queries'] = lambda req: {'data': [{'n': i} for i in range(150)]}
    r = run(ld.db_query('D1', 'select n from t'))
    assert r['row_count'] == 150 and len(r['rows']) == 100 and r['truncated']
    assert seen[-1].content and b'select n from t' in seen[-1].content


def test_billing_details(api):
    routes, _ = api
    routes['GET /v1/billing'] = {'user': {'balance': 0}, 'team': {'balance': 240000, 'minCreditAmount': 20000}}
    routes['GET /v1/usage-report'] = {'projects': [{'project_id': 'a', 'totalHourlyPrice': 1000}, {'project_id': 'b', 'totalHourlyPrice': 2000}], 'totalHourlyPrice': 3000, 'totalMonthlyPrice': 2160000}
    routes['GET /v1/billing-invoices'] = {'billingInvoices': [], 'totalBillingInvoices': 0}
    b = run(ld.billing())
    assert b['balance_toman'] == 240000 and b['hours_left'] == 80.0 and b['days_left'] == 3.3 and b['apps'][0]['app'] == 'b'
