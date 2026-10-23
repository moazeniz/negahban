import asyncio
import pytest
from negahban import agent, approvals, memory, store, tools


def test_remember_and_recall_across_sessions(fresh_db):
    memory.ensure(); store.x('delete from memory')
    asyncio.run(tools.run('remember', {'text': 'نام مالک امیر است و پروژه‌ی اصلی bazargard-ai است'}, {'actor': 'agent'}))
    hits = asyncio.run(tools.run('recall', {'query': 'پروژه‌ی اصلی'}, {}))
    assert hits and 'bazargard-ai' in hits[0]['text']
    assert 'امیر' in memory.context_block('another-session')


def test_incident_memory_feeds_new_diagnosis(fresh_db):
    memory.ensure(); store.x('delete from memory')
    inc = {'id': 'inc_1', 'app': 'demo-shop', 'title': 'OOM', 'kind': 'memory', 'diagnosis': {'hypotheses': [{'id': 'oom_leak', 'title': 'نشت حافظه'}]}}
    memory.remember_incident(inc, 'ری‌استارت')
    from test_classifiers import ev, trend
    from negahban.diagnose import hypotheses
    H = hypotheses(ev(events=[{'action': 'oom', 'release': 'r'}], mem=trend([(i * 60, 0.25 + 0.025 * i) for i in range(10)], 0.5)))
    assert H[0]['id'] == 'oom_leak' and H[0].get('seen_before') == 1 and any('قبلاً' in e for e in H[0]['evidence'])


def test_reply_cannot_claim_card_that_was_not_created():
    steps = [{'tool': 'delete_app', 'risk': 'sensitive', 'ok': False}]
    out = asyncio.run(agent._ground('کارت تأیید حذف برنامه ثبت شد؛ نام را تایپ کنید.', steps, 'demo-shop را حذف کن'))
    assert 'هیچ کارت تأییدی ساخته نشد' in out


def test_reply_mentions_real_card_id():
    out = asyncio.run(agent._ground('کارت تأیید ساخته شد.', [{'tool': 'restart_app', 'risk': 'sensitive', 'proposal': 'prp_abc123'}], 'ری‌استارت کن'))
    assert 'prp_abc123' in out


@pytest.fixture
def quiet(monkeypatch, fresh_db):
    async def fp(app):
        return 'f'

    async def imp(tool, a):
        return {}, ''
    monkeypatch.setattr(approvals, 'state_fingerprint', fp); monkeypatch.setattr(approvals, 'impact', imp)


def test_delete_app_creates_card_without_confirm_name(quiet):
    r = asyncio.run(tools.run('delete_app', {'app': 'demo-shop'}, {}))
    p = store.get_proposal(r['proposal']['id'])
    assert p['status'] == 'pending' and p['tool'] == 'delete_app'


def test_locked_app_never_gets_a_card(quiet):
    store.set_policy('meowmeow-wallex', {'write_lock': True, 'env_lock': True})
    for tool, args in (('delete_app', {'app': 'meowmeow-wallex'}), ('restart_app', {'app': 'meowmeow-wallex'}), ('set_env', {'app': 'meowmeow-wallex', 'key': 'X', 'value': 'y'})):
        r = asyncio.run(tools.run(tool, args, {}))
        assert r.get('refused') and 'proposal' not in r, (tool, r)
    assert not [p for p in store.proposals(None, 50) if p['app'] == 'meowmeow-wallex']


def test_topology_uses_live_provider(monkeypatch):
    from negahban import llm as L
    monkeypatch.setattr(L.llm, 'info', lambda: {'provider': 'anthropic', 'model': 'claude-haiku-5-5', 'fallback': 'boxapi'})

    async def apps():
        return [{'project_id': 'demo-shop', 'planID': 'small-g2'}]
    monkeypatch.setattr(tools.liara, 'list_apps', apps)
    m = asyncio.run(tools.TOOLS['topology_diagram']['fn']({}, {}))['_attach']['mermaid']
    assert 'Anthropic claude-haiku-5-5' in m and 'پشتیبان' in m


def test_default_subdomain_tool_is_sensitive_with_title(quiet):
    async def subimp(a):
        return {}, ''
    r = asyncio.run(tools.run('set_default_subdomain', {'app': 'demo-shop', 'enable': False}, {}))
    assert r['proposal']['title'].startswith('غیرفعال کردن زیردامنه')


def test_delete_executes_only_with_typed_name(quiet, monkeypatch):
    calls = []

    async def fake_delete(name):
        calls.append(name)
    monkeypatch.setattr(tools.liara, 'delete_app', fake_delete)

    async def verify(*a, **k):
        return {'passed': True, 'checks': []}
    import negahban.diagnose as d
    monkeypatch.setattr(d, 'verify', verify)
    store.upsert_app('scratch-app', 'monitored')

    async def go(name):
        pid = (await tools.run('delete_app', {'app': 'scratch-app'}, {}))['proposal']['id']
        await approvals.approve(pid, 'owner', {'confirm_name': name})
        for _ in range(50):
            await asyncio.sleep(0.02)
            if store.get_proposal(pid)['status'] in ('done', 'failed'):
                break
        return store.get_proposal(pid)['status']
    assert asyncio.run(go('wrong')) == 'failed' and calls == []
    assert asyncio.run(go('scratch-app')) == 'done' and calls == ['scratch-app']


def test_vm_root_password_never_leaves_iaas(monkeypatch):
    from negahban.diag import iaas

    async def fake_get(path, **kw):
        vm = {'_id': 'v1', 'name': 'srv1', 'config': {'rootPassword': 'S3cr3t-root-pw', 'SSHKeys': ['ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample']}}
        return {'vms': [vm]} if path == '/vm' else {'vm': vm}
    monkeypatch.setattr(iaas, 'get', fake_get)
    import json
    lst = asyncio.run(iaas.vms()); det = asyncio.run(iaas.detail('v1'))
    assert 'S3cr3t-root-pw' not in json.dumps(lst) + json.dumps(det)


def test_no_redundant_correction_when_reply_already_says_no_card():
    out = asyncio.run(agent._ground('نمی‌توانم؛ برنامه قفل است و کارت تأییدی ساخته نشد.', [{'tool': 'restart_app', 'risk': 'sensitive', 'ok': False}], 'ری‌استارت کن'))
    assert 'تصحیح خودکار' not in out
