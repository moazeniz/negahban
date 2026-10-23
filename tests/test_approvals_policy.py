import asyncio, time
import pytest
from negahban import approvals, store, tools


def run(c):
    return asyncio.run(c)


@pytest.fixture
def fake(monkeypatch, fresh_db):
    state = {'fp': 'fp1', 'calls': []}

    async def fp(app):
        return state['fp']

    async def impact(tool, a):
        return {'downtime': 'x', 'cost': 'y', 'risk': 'z'}, 'rb'

    async def fake_restart(a, ctx):
        state['calls'].append(('restart', a['app']))
        return {'restarted': a['app']}

    async def verify(app, since, expect=None, attempts=8, wait=15):
        return {'passed': True, 'checks': [{'name': 'ok', 'ok': True, 'detail': ''}]}
    monkeypatch.setattr(approvals, 'state_fingerprint', fp)
    monkeypatch.setattr(approvals, 'impact', impact)
    monkeypatch.setitem(tools.TOOLS['restart_app'], 'fn', fake_restart)
    import negahban.diagnose as d
    monkeypatch.setattr(d, 'verify', verify)
    store.upsert_app('demo-shop', 'demo')
    return state


async def _approve_and_wait(pid):
    r = await approvals.approve(pid, 'owner')
    for _ in range(50):
        await asyncio.sleep(0.02)
        if store.get_proposal(pid)['status'] in ('done', 'failed', 'unverified'):
            break
    return r


def test_sensitive_tool_only_creates_proposal(fake):
    r = run(tools.run('restart_app', {'app': 'demo-shop'}, {'origin': 'chat'}))
    p = store.get_proposal(r['proposal']['id'])
    assert p['status'] == 'pending' and fake['calls'] == []


def test_approve_executes_once_and_verifies(fake):
    async def go():
        pid = (await tools.run('restart_app', {'app': 'demo-shop'}, {}))['proposal']['id']
        await _approve_and_wait(pid)
        again = await approvals.approve(pid, 'owner')
        return pid, again
    pid, again = run(go())
    assert fake['calls'] == [('restart', 'demo-shop')] and store.get_proposal(pid)['status'] == 'done'
    assert 'error' in again  # one-use


def test_expired_proposal_refused(fake):
    async def go():
        pid = (await tools.run('restart_app', {'app': 'demo-shop'}, {}))['proposal']['id']
        store.set_proposal(pid, expires_at=time.time() - 1)
        return await approvals.approve(pid, 'owner')
    assert 'error' in run(go()) and fake['calls'] == []


def test_stale_fingerprint_refused(fake):
    async def go():
        pid = (await tools.run('restart_app', {'app': 'demo-shop'}, {}))['proposal']['id']
        fake['fp'] = 'changed'
        r = await approvals.approve(pid, 'owner')
        return pid, r
    pid, r = run(go())
    assert 'error' in r and store.get_proposal(pid)['status'] == 'stale' and fake['calls'] == []


def test_write_lock_refuses_card_and_blocks_execution(fake):
    store.set_policy('demo-shop', {'write_lock': True})
    r = run(tools.run('restart_app', {'app': 'demo-shop'}, {}))
    assert r.get('refused') and 'فقط پایش' in r['error'] and not store.proposals('pending')
    # even a card that existed before the lock was set cannot execute
    store.set_policy('demo-shop', {'write_lock': False})
    pid = run(tools.run('restart_app', {'app': 'demo-shop'}, {}))['proposal']['id']
    store.set_policy('demo-shop', {'write_lock': True})
    assert 'error' in run(approvals.approve(pid, 'owner')) and fake['calls'] == []


def test_env_lock_blocks_env_changes(fake):
    store.set_policy('demo-shop', {'env_lock': True})
    assert 'قفل' in approvals.guard('set_env', {'app': 'demo-shop', 'key': 'X'}) and approvals.guard('restart_app', {'app': 'demo-shop'}) is None


def test_set_env_value_never_stored_in_proposal(fake):
    r = run(tools.run('set_env', {'app': 'demo-shop', 'key': 'API_KEY', 'value': 'super-secret-value-1'}, {}))
    p = store.get_proposal(r['proposal']['id'])
    assert 'super-secret-value-1' not in str(p) and p['args'].get('value_ref')


def test_unknown_tool_and_bad_args_rejected():
    with pytest.raises(tools.ToolError):
        tools.validate('rm_rf', {})
    with pytest.raises(tools.ToolError):
        tools.validate('resize_app', {'app': 'x', 'plan': 'galaxy-xxl'})
    with pytest.raises(tools.ToolError):
        tools.validate('get_logs', {'app': 'x', 'shell': 'ls'})


def test_delete_requires_typed_name(fake):
    with pytest.raises(tools.ToolError):
        run(tools.TOOLS['delete_app']['fn']({'app': 'demo-shop', 'confirm_name': 'demo'}, {}))


def test_default_policy_is_safe():
    p = store.policy('some-new-app')
    assert not p['autopilot'] and not p['resize'] and not p['auto_fix_code']
