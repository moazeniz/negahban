import asyncio, gzip, json, sqlite3, time
import pytest
from negahban import approvals, control, store, tools


def test_ai_off_blocks_every_llm_call_but_not_rules(fresh_db, monkeypatch):
    from negahban import llm as L
    control.set_ai(False)
    try:
        assert not L.llm.available
        with pytest.raises(L.LLMError):
            asyncio.run(L.llm.chat([{'role': 'user', 'content': 'x'}]))
        # rule-based diagnosis still works without the LLM
        from test_classifiers import ev, trend
        from negahban.diagnose import hypotheses
        H = hypotheses(ev(cpu=trend([(i * 60, 0.48) for i in range(6)], 0.5)))
        assert H[0]['id'] == 'cpu_saturation'
    finally:
        control.set_ai(True)


def test_supervisor_restarts_crashed_loop():
    runs = []

    async def flaky():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError('boom')
        await asyncio.sleep(10)

    async def go():
        t = asyncio.create_task(control.supervise('t', flaky, base=0.01))
        await asyncio.sleep(0.2)
        t.cancel()
    asyncio.run(go())
    assert len(runs) == 3 and control.STATE['loops']['t']['restarts'] == 2


def test_snapshot_is_consistent_and_retained(fresh_db):
    store.kv_set('probe-key', 'v1')
    for _ in range(3):
        p = control.snapshot('hourly')
    with gzip.open(p) as g, open('/tmp/ngh-snap-test.db', 'wb') as f:
        f.write(g.read())
    c = sqlite3.connect('/tmp/ngh-snap-test.db')
    assert c.execute("select v from kv where k='probe-key'").fetchone()[0] == '"v1"'
    assert control.snapshots() and store.kv_get('snapshot_last')['file'] == p.name


def test_healthz_turns_unhealthy_when_restart_requested():
    from fastapi.testclient import TestClient
    from negahban import main
    c = TestClient(main.app)
    control.STATE['restart_requested'] = True
    try:
        assert c.get('/healthz').status_code == 503
    finally:
        control.STATE['restart_requested'] = False
    assert c.get('/healthz').status_code == 200


def test_proposals_carry_rule_vs_ai_badge():
    assert approvals.public({'origin': 'autopilot', 'tool': 'resize_app'})['decision'] == 'rules'
    assert approvals.public({'origin': 'orchestrator', 'tool': 'apply_code_fix'})['decision'] == 'ai'
    assert approvals.public({'origin': 'chat', 'tool': 'restart_app'})['decision'] == 'ai'


def test_daily_report_without_llm(fresh_db, monkeypatch):
    from negahban import reports
    store.upsert_app('demo-shop', 'demo')
    now = time.time()
    for i in range(20):
        store.add_sample('demo-shop', ts=now - i * 60, cpu=0.45, mem=0.1, cpu_limit=0.5, mem_limit=0.5, latency_ms=100 + i, status=200 if i else 500, ok=bool(i))

    async def apps():
        return [{'project_id': 'demo-shop', 'planID': 'small-g2', 'bundlePlanID': 'standard', 'scale': 1}]

    async def usage(a, c):
        return {'balance_toman': 100000, 'total_toman_per_hour': 9000, 'hours_left': 11}
    monkeypatch.setattr(reports.liara, 'list_apps', apps)
    import negahban.tools as T
    monkeypatch.setattr(T, 'team_usage', usage)
    rep = asyncio.run(reports.build(with_llm=False))
    a = rep['apps'][0]
    assert 94 < a['uptime_pct'] < 96.5 and a['cpu_peak_pct'] == 90.0 and rep['summary']
    assert any('اعتبار' in r for r in rep['recommendations']) and any('سقف منابع' in r for r in rep['recommendations'])
    assert reports.get()['id'] == rep['id']


@pytest.fixture
def quiet(monkeypatch, fresh_db):
    async def fp(app):
        return 'f'
    monkeypatch.setattr(approvals, 'state_fingerprint', fp)


def test_launch_card_keeps_secrets_out_and_executor_deploys(quiet, monkeypatch, tmp_path):
    from negahban import onboarding, analyzer
    pid = 'proj_0123456789ab'
    d = analyzer.project_dir(pid); d.mkdir(parents=True, exist_ok=True)
    (d / 'main.py').write_text('from fastapi import FastAPI\napp = FastAPI()\n@app.get("/health")\ndef h():\n    return {}\n')
    (d / 'Dockerfile').write_text('FROM python:3.12-slim\nEXPOSE 8000\n')
    store.kv_set(f'project:{pid}', {'port': 8000, 'health_path': '/health', 'framework': 'FastAPI', 'env_schema': [{'name': 'API_KEY', 'secret': True, 'required': True}]})
    calls = []

    async def app_(name):
        from negahban.liara import LiaraError
        raise LiaraError(404, 'not found')

    async def create(*a, **k):
        calls.append(('create', a))

    async def deploy(app, tb, port, envs, disks, msg, hc=None):
        calls.append(('deploy', app, port, envs)); return 'rel1'

    async def wait(app, rid, ctx):
        return 'READY', {}
    monkeypatch.setattr(onboarding.liara, 'app', app_); monkeypatch.setattr(onboarding.liara, 'create_app', create)
    monkeypatch.setattr(onboarding.liara, 'deploy_tar', deploy)
    import negahban.deployer as D
    monkeypatch.setattr(D, '_wait', wait)
    ref = onboarding.stash_secrets({'API_KEY': 'super-secret-1234'})
    r = asyncio.run(tools.run('launch_project', {'project_id': pid, 'app': 'my-new-app', 'plan': 'small-g2', 'envs': {'APP_ENV': 'prod'}, 'secret_ref': ref}, {}))
    p = store.get_proposal(r['proposal']['id'])
    assert 'super-secret-1234' not in json.dumps(p, ensure_ascii=False) and p['impact']['secrets_provided'] == ['API_KEY']
    res = asyncio.run(tools.TOOLS['launch_project']['fn'](p['args'], {}))
    assert res['release_state'] == 'READY' and res['created_app'] and calls[-1][3] == {'APP_ENV': 'prod', 'API_KEY': 'super-secret-1234'}
    assert store.get_app('my-new-app')['port'] == 8000
