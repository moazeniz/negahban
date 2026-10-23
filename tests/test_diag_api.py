import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from negahban import security, tools
from negahban.diag import api as dapi, register

register(tools.tool)
APP = FastAPI()
APP.include_router(dapi.router)


@pytest.fixture
def cl():
    c = TestClient(APP)
    tok, _ = security.make_session()
    c.cookies.set(security.COOKIE, tok)
    c.cookies.set(security.CSRF_COOKIE, 'csrf-123')
    return c


def test_auth_and_csrf_required(cl):
    assert TestClient(APP).get('/api/diag/tools').status_code == 401
    assert cl.post('/api/diag/exec', json={'app': 'demo-shop', 'command': 'df -h'}).status_code == 403


def test_classify_and_tools(cl):
    assert cl.get('/api/diag/classify', params={'command': 'df -h'}).json()['verdict'] == 'read'
    assert cl.get('/api/diag/classify', params={'command': 'rm -rf /'}).json()['verdict'] == 'blocked'
    names = {t['name'] for t in cl.get('/api/diag/tools').json()}
    assert {'container_diag', 'log_insights', 'run_runbook'} <= names


def test_exec_routes_by_verdict(cl, monkeypatch, fresh_db):
    calls = []

    async def fake_run(name, args, ctx):
        calls.append((name, args))
        return {'proposal': {'id': 'p1'}} if name == 'container_exec_any' else {'output': 'ok'}
    monkeypatch.setattr(tools, 'run', fake_run)
    h = {'X-CSRF-Token': 'csrf-123'}
    r = cl.post('/api/diag/exec', json={'app': 'demo-shop', 'command': 'df -h'}, headers=h).json()
    assert r['verdict'] == 'read' and r['result'] == {'output': 'ok'}
    r = cl.post('/api/diag/exec', json={'app': 'demo-shop', 'command': 'kill 7'}, headers=h).json()
    assert r['verdict'] == 'sensitive' and r['result']['proposal']['id'] == 'p1'
    assert cl.post('/api/diag/exec', json={'app': 'demo-shop', 'command': 'rm -rf /'}, headers=h).status_code == 400
    assert cl.post('/api/diag/exec', json={'app': 'negahban-sre', 'command': 'kill 7'}, headers=h).status_code == 403
    assert [c[0] for c in calls] == ['container_exec', 'container_exec_any']
    assert cl.post('/api/diag/run', json={'tool': 'restart_app', 'args': {'app': 'x'}}, headers=h).status_code == 404
