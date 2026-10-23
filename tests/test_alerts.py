import asyncio, json, time
from negahban import alerts, store


def setup_function(_):
    alerts.ensure()
    store.x('delete from notifications'); store.x("delete from kv where k like 'alertdur:%'")


def test_defaults_seeded_and_validation():
    ids = {r['id'] for r in alerts.rules()}
    assert {'r-5xx', 'r-crashlog', 'r-credit'} <= ids
    for bad in ({'name': 'x', 'kind': 'metric', 'metric': 'nope', 'op': '>', 'threshold': 1, 'severity': 'info', 'action': 'notify'},
                {'name': 'x', 'kind': 'log', 'pattern': 're:([', 'severity': 'info', 'action': 'notify'},
                {'name': 'x', 'kind': 'metric', 'metric': 'cpu_pct', 'op': '>', 'threshold': 1, 'severity': 'info', 'action': 'autopilot:delete_app'}):
        try:
            alerts.save_rule(bad); assert False, bad
        except ValueError:
            pass


def test_rule_versioning():
    r = alerts.save_rule({'id': 'r-test', 'name': 'آزمون', 'kind': 'metric', 'metric': 'cpu_pct', 'op': '>', 'threshold': 50, 'severity': 'warning', 'action': 'notify'})
    r2 = alerts.save_rule({**r, 'threshold': 60})
    assert r2['version'] == r['version'] + 1 and alerts.versions('r-test')[0]['version'] == r2['version']


def test_pattern_matching_kinds():
    assert alerts.match_line('event=app.crash', json.dumps({'event': 'app.crash', 'level': 'critical'}))
    assert not alerts.match_line('event=app.crash level=error', json.dumps({'event': 'app.crash', 'level': 'critical'}))
    assert alerts.match_line(r're:Traceback \(most', 'Traceback (most recent call last):')
    assert alerts.match_line('ZeroDivision', 'zerodivisionerror: division by zero')


def test_metric_rule_fires_with_dedup_and_cooldown(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts.bus, 'publish', lambda kind, **d: sent.append(kind))
    store.upsert_app('demo-shop', 'demo')
    n1 = asyncio.run(alerts.evaluate_metrics('demo-shop', {'cpu_pct': 99.0, 'http_5xx_rate': 0}))
    # r-cpu needs 5 min sustained -> not yet; latency/5xx below thresholds
    assert not [n for n in n1 if n['rule_id'] == 'r-cpu']
    store.kv_set('alertdur:r-cpu:demo-shop', time.time() - 400)
    n2 = asyncio.run(alerts.evaluate_metrics('demo-shop', {'cpu_pct': 99.0}))
    assert [n for n in n2 if n['rule_id'] == 'r-cpu'] and 'notify' in sent
    n3 = asyncio.run(alerts.evaluate_metrics('demo-shop', {'cpu_pct': 99.0}))
    assert not [n for n in n3 if n['rule_id'] == 'r-cpu']  # already open -> no duplicate


def test_log_rule_wake_opens_incident(monkeypatch):
    submitted = []
    import negahban.jobs as J
    monkeypatch.setattr(J, 'submit', lambda kind, payload, **k: submitted.append((kind, payload)))
    store.upsert_app('demo-shop', 'demo')
    out = asyncio.run(alerts.evaluate_lines('demo-shop', [{'line': json.dumps({'event': 'app.crash', 'level': 'critical', 'msg': 'boom'})}], 'push'))
    n = [x for x in out if x['rule_id'] == 'r-crashlog'][0]
    assert n['link'].startswith('#incidents/') and submitted and submitted[0][0] == 'incident.handle'
    alerts.set_status(n['id'], 'acked')
    assert alerts.listing('acked')[0]['id'] == n['id']


def test_escalation_reminder(monkeypatch):
    monkeypatch.setattr(alerts.bus, 'publish', lambda *a, **k: None)
    nid = 'ntf_x'
    store.x("insert into notifications(id,ts,rule_id,app,severity,title,body,status,level,last_sent,dedup) values(?,?,?,?,?,?,?,?,?,?,?)",
            (nid, time.time() - 700, 'r-cpu', 'demo-shop', 'warning', 't', 'b', 'open', 0, time.time() - 700, 'd'))
    asyncio.run(alerts.escalate())
    r = store.q('select * from notifications where id=?', (nid,), one=True)
    assert r['level'] == 1 and r['severity'] == 'critical'


def test_ingest_endpoint_requires_app_token_and_fires():
    from fastapi.testclient import TestClient
    from negahban import main
    store.upsert_app('demo-shop', 'demo')
    c = TestClient(main.app)
    assert c.post('/api/ingest/event', json={'event': 'app.crash'}).status_code == 401
    tok = alerts.ingest_token('demo-shop', rotate=True)
    r = c.post('/api/ingest/event', json={'event': 'job.dead_letter', 'level': 'error', 'msg': 'x'}, headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 200 and r.json()['accepted'] == 1 and r.json()['fired']


def test_llm_budget_cap_switches_ai_off(monkeypatch):
    from negahban import control, llm as L
    monkeypatch.setattr(L, 'usage_rows', lambda days=1: [{'day': time.strftime('%Y-%m-%d', time.gmtime()), 'feature': 'chat', 'cost_usd': 5.0}])
    async def nofire(*a, **k):
        return None
    monkeypatch.setattr(alerts, 'fire', nofire)
    control.set_ai(True)
    async def go():
        return alerts.llm_budget_ok('chat')
    assert asyncio.run(go()) is False and not control.ai_enabled()
    control.set_ai(True)


def test_auto_action_cap():
    alerts.set_guardrails({'max_auto_actions_per_hour': 1})
    store.x('delete from audit')
    assert alerts.auto_actions_ok()
    store.audit('autopilot', 'approve', 'demo-shop', {})
    assert not alerts.auto_actions_ok()
    alerts.set_guardrails({'max_auto_actions_per_hour': 6})
