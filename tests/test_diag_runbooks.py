import asyncio
import pytest
from negahban.diag import runbooks as rb


def run(c):
    return asyncio.run(c)


def fake(value):
    async def f(*a, **k):
        if isinstance(value, Exception):
            raise value
        return value(*a, **k) if callable(value) else value
    return f


class FakeLiara:
    def __init__(self, envs=None, plan='small-g2', releases=None):
        self._envs, self._plan, self._rel = envs or {}, plan, releases or {'releases': [{'_id': 'r1', 'tag': 'v3', 'state': 'READY'}], 'currentRelease': 'r1'}

    async def app(self, name):
        return {'planID': self._plan, 'bundlePlanID': 'basic', 'envs': [{'key': k, 'value': v} for k, v in self._envs.items()]}

    async def envs(self, name):
        return dict(self._envs)

    async def releases(self, name, count=10):
        return self._rel


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(rb, 'liara', FakeLiara())
    monkeypatch.setattr(rb.logintel, 'fetch', fake([]))

    async def cat():
        return {}
    monkeypatch.setattr(rb.plans, 'catalogue', cat)
    return monkeypatch


def test_oom_runbook_leak(env):
    env.setattr(rb.liara_data, 'events_timeline', fake({'oom': 3, 'crashes': 3, 'unplanned_restarts': 2}))
    env.setattr(rb, 'run_checks', fake({'memory': {'util': 0.93, 'usage_mb': 476, 'limit_mb': 512, 'oom_kills': 3},
                                        'processes': {'processes': [{'name': 'python', 'rss_mb': 450, 'pid': 1}]}}))
    env.setattr(rb, '_forecast', fake({'verdict': 'leak_likely', 'slope_per_hour': 40.0, 'r2': 0.9, 'hours_to_limit': 0.9, 'util': 0.93}))
    r = run(rb.run('demo-shop', 'oom', 60))
    ids = [h['id'] for h in r['hypotheses']]
    assert ids[0] == 'memory_leak' and 'plan_too_small' in ids
    tools = [a['tool'] for a in r['recommended_actions']]
    assert 'restart_app' in tools and 'resize_app' in tools
    assert next(a for a in r['recommended_actions'] if a['tool'] == 'resize_app')['args']['plan'] == 'medium-g2'
    assert any(f['severity'] == 'critical' for f in r['findings'])
    assert all(s['ok'] for s in r['steps'])
    assert 'envs' not in str(r['evidence'])


def test_crash_loop_missing_env(env):
    env.setattr(rb.liara_data, 'events_timeline', fake({'oom': 0, 'crashes': 5, 'unplanned_restarts': 4}))
    env.setattr(rb, '_logs', fake({'restart_loop': True, 'error_groups': [{'error_type': 'KeyError', 'count': 5, 'fingerprint': 'KeyError@main.py:<module>', 'message': "'PAYMENT_GATEWAY_KEY'"}]}))
    env.setattr(rb.liara_data, 'release_details', fake({'is_current': True, 'tag': 'v9', 'port': 8080, 'finished_at': 'x'}))
    env.setattr(rb, 'run_checks', fake({'ports': {'listening': [{'port': 8080}]}, 'env_names': {'names': []}, 'memory': {}}))
    env.setattr(rb, '_missing_env_from_logs', fake(['PAYMENT_GATEWAY_KEY']))
    r = run(rb.run('demo-shop', 'crash_loop', 30))
    assert r['hypotheses'][0]['id'] == 'missing_env'
    a = r['recommended_actions'][0]
    assert a['tool'] == 'set_env' and a['args'] == {'app': 'demo-shop', 'key': 'PAYMENT_GATEWAY_KEY', 'restore': True}
    assert 'port_mismatch' not in [h['id'] for h in r['hypotheses']]


def test_build_failure_and_failed_steps_do_not_abort(env):
    env.setattr(rb.liara_data, 'release_details', fake({'tag': 'v4', 'state': 'FAILED', 'fail_reason': None, 'stderr_lines': ['ERROR: Could not find a version that satisfies the requirement foo==9'],
                                                        'build_summary': {'error_lines': ['ERROR: No matching distribution found for foo==9'], 'missing_package': 'foo==9'}}))
    r = run(rb.run('demo-shop', 'build_failure'))
    assert r['hypotheses'][0]['id'] == 'missing_package' and r['recommended_actions'][0]['tool'] == 'propose_code_fix'
    env.setattr(rb.liara_data, 'events_timeline', fake(RuntimeError('liara down')))
    env.setattr(rb, 'run_checks', fake({'memory': {}, 'processes': {}}))
    env.setattr(rb, '_forecast', fake({'verdict': 'stable'}))
    r = run(rb.run('demo-shop', 'oom'))
    st = {s['key']: s for s in r['steps']}
    assert not st['events']['ok'] and 'liara down' in st['events']['error'] and r['hypotheses']


def test_triage_and_auto(env):
    env.setattr(rb.liara_data, 'events_timeline', fake({'oom': 0, 'crashes': 0, 'unplanned_restarts': 0}))
    env.setattr(rb, 'liara', FakeLiara(releases={'releases': [{'_id': 'r9', 'state': 'FAILED'}]}))
    env.setattr(rb.probes, 'http_probe', fake({'ok': True, 'status': 200, 'total_ms': 120, 'chain': [{}]}))
    assert run(rb.triage('demo-shop', 30))[0] == 'build_failure'
    env.setattr(rb, 'liara', FakeLiara())
    env.setattr(rb.probes, 'http_probe', fake({'ok': False, 'chain': [{'error': 'tls_verify_failed'}]}))
    assert run(rb.triage('demo-shop', 30))[0] == 'tls_problem'


def test_propose_actions_only_sensitive_registered(env, fresh_db):
    import negahban.approvals as ap
    calls = []

    async def propose(tool, args, ctx, why=None, auto=False, incident_id=None):
        calls.append((tool, args, why))
        return {'id': 'p1', 'tool': tool}
    env.setattr(ap, 'propose', propose)
    res = {'runbook': 'oom', 'recommended_actions': [{'tool': 'log_insights', 'args': {'app': 'a'}}, {'tool': None, 'manual': 'x'},
                                                     {'tool': 'resize_app', 'args': {'app': 'a', 'plan': None}}, {'tool': 'restart_app', 'args': {'app': 'a'}, 'why': 'leak'}]}
    cards = run(rb.propose_actions(res, {'actor': 't'}))
    assert [c[0] for c in calls] == ['restart_app'] and cards == [{'id': 'p1', 'tool': 'restart_app'}] and 'runbook oom' in calls[0][2]
