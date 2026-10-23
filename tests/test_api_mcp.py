import asyncio
from fastapi.testclient import TestClient
from negahban import main, mcp

c = TestClient(main.app)


def login(cl):
    r = cl.post('/api/login', json={'password': 'test-admin-pw'})
    assert r.status_code == 200
    return r.json()['csrf']


def test_auth_required():
    cl = TestClient(main.app)
    assert cl.get('/api/overview').status_code == 401 and cl.get('/api/proposals').status_code == 401


def test_wrong_password_and_rate_limit():
    cl = TestClient(main.app, headers={'x-forwarded-for': '9.9.9.9'})
    codes = [cl.post('/api/login', json={'password': 'nope'}).status_code for _ in range(7)]
    assert codes[:6] == [401] * 6 and codes[6] == 429


def test_csrf_required_for_mutations():
    cl = TestClient(main.app, headers={'x-forwarded-for': '8.8.8.8'})
    csrf = login(cl)
    assert cl.post('/api/proposals/x/reject').status_code == 403
    assert cl.post('/api/proposals/x/reject', headers={'X-CSRF-Token': csrf}).status_code == 409


def test_security_headers_and_cache_busting():
    r = c.get('/')
    assert "script-src 'self'" in r.headers['content-security-policy'] and r.headers['x-frame-options'] == 'DENY'
    assert '?v=1"' not in r.text and 'no-store' in r.headers['cache-control']


def test_mcp_requires_bearer_and_lists_tools():
    assert c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).status_code == 401
    r = c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, headers={'Authorization': 'Bearer mcp-test-token-123'})
    names = {t['name'] for t in r.json()['result']['tools']}
    assert {'diagnose', 'restart_app', 'resize_app', 'get_logs', 'chart_metrics'} <= names


def test_mcp_initialize_and_sensitive_call_makes_proposal(monkeypatch, fresh_db):
    async def fp(app):
        return 'f'

    async def imp(tool, a):
        return {}, ''
    import negahban.approvals as ap
    monkeypatch.setattr(ap, 'state_fingerprint', fp); monkeypatch.setattr(ap, 'impact', imp)
    init = asyncio.run(mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}))
    assert init['result']['serverInfo']['name'] == 'negahban-sre'
    r = asyncio.run(mcp.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'restart_app', 'arguments': {'app': 'demo-shop'}}}))
    assert 'pending_approval' in r['result']['content'][0]['text']


def test_watchdog_ping_needs_token():
    assert c.post('/api/watchdog/ping', json={}).status_code == 401
    assert c.post('/api/watchdog/ping', json={'host': 'h'}, headers={'Authorization': 'Bearer wd-test-token-123'}).status_code == 200


def test_status_vantage_and_page(monkeypatch):
    import time as _t
    from negahban import store
    monkeypatch.setenv('STATUS_INGEST_TOKEN', 'status-tok-123456')
    store.upsert_app('demo-shop', 'demo')
    store.add_sample('demo-shop', ts=_t.time(), cpu=0.01, mem=0.05, cpu_limit=0.5, mem_limit=0.5, latency_ms=80, status=200, ok=True)
    assert c.get('/api/status/vantage').status_code == 401
    r = c.get('/api/status/vantage', headers={'Authorization': 'Bearer status-tok-123456'}).json()
    app = next(a for a in r['apps'] if a['name'] == 'demo-shop')
    assert r['vantage'] == 'liara-internal' and app['ok'] and app['status'] == 200 and app['checked_at'].endswith('Z')
    body = {'generated_at': 'now', 'apps': [{'name': 'demo-shop', 'up': True, 'verdict': 'degraded_external_only', 'vantages': {'external': {'ok': False, 'latency_ms': None}, 'internal': {'ok': True, 'latency_ms': 80}}}],
            'notes': [{'kind': 'vantage_unreachable', 'vantage': 'hetzner', 'start': 'a', 'end': 'b', 'minutes': 2}], 'slo': {'rules': 'down only if both agree'}}
    assert c.post('/api/status/ingest', json=body, headers={'Authorization': 'Bearer status-tok-123456'}).status_code == 200
    page = c.get('/status').text
    assert 'فقط از بیرون قطع' in page and 'vantage_unreachable' in page and 'پایش داخل لیارا' in page
