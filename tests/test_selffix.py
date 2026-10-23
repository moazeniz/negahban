"""Self-repair pipeline on the real demo app source: buggy release -> traceback -> LLM patch (mocked) -> sandbox tests."""
import asyncio
import pytest
from negahban import scenarios, selffix, store
from negahban import workspace as ws

FIXED = open(__import__('pathlib').Path(__file__).resolve().parent.parent / 'demo-shop' / 'shop' / 'pricing.py').read()
TB = ['Traceback (most recent call last):', '  File "/app/main.py", line 34, in product_view', '    return x',
      '  File "/app/shop/pricing.py", line 7, in discount_percent', '    return round(100 * (old - p["price"]) / old)', 'ZeroDivisionError: division by zero']


@pytest.fixture
def demo(monkeypatch, fresh_db, tmp_path):
    store.upsert_app('demo-shop', 'demo')
    import shutil
    shutil.rmtree(ws.ws_dir('demo-shop'), ignore_errors=True)
    ws.ensure('demo-shop')
    ws.write_files('demo-shop', [{'path': 'shop/pricing.py', 'content': scenarios.BUGGY_PRICING}], 'refactor: simplify discount')

    async def logs(app, start=None, end=None, search=None, release_id=None, max_lines=600):
        return [{'ts': 1000 + i, 'type': 'stderr', 'line': l, 'release': 'r'} for i, l in enumerate(TB)]

    async def releases(app, n=10):
        return {'releases': [{'_id': 'r', 'state': 'READY'}], 'currentRelease': 'r'}

    async def complete_json(system, user, max_tokens=0, reasoning=None):
        assert '<untrusted-data' in user and 'ZeroDivisionError' in user
        return {'root_cause': 'تقسیم بر صفر', 'technical': 'guard zero old price', 'files': [{'path': 'shop/pricing.py', 'content': FIXED}], 'confidence': 0.9}

    proposed = []

    async def propose(tool, args, ctx, why=None, auto=False, incident_id=None):
        proposed.append((tool, args, ctx))
        return {'id': 'prp_x', 'title': 't'}
    monkeypatch.setattr(selffix.liara, 'logs', logs)
    monkeypatch.setattr(selffix.liara, 'releases', releases)
    monkeypatch.setattr(selffix.llm, 'complete_json', complete_json)
    import negahban.approvals as ap
    monkeypatch.setattr(ap, 'propose', propose)
    return proposed


def test_buggy_release_fails_tests():
    store.upsert_app('demo-shop', 'demo')
    ws.ensure('demo-shop')
    r = asyncio.run(ws.run_tests('demo-shop', [{'path': 'shop/pricing.py', 'content': scenarios.BUGGY_PRICING}]))
    assert not r['passed'] and 'failed' in r['output']


def test_prepare_fix_reproduces_patches_and_proposes(demo):
    r = asyncio.run(selffix.prepare_fix('demo-shop', origin='test'))
    assert r['status'] == 'proposed', r
    assert not r['tests_before']['passed'] and r['tests_after']['passed']
    assert '+    if not old or old <= p["price"]:' in r['diff']
    tool, args, ctx = demo[0]
    assert tool == 'apply_code_fix' and ctx['extra']['tests_after']['passed']
    assert ws.read('demo-shop', 'shop/pricing.py') == scenarios.BUGGY_PRICING  # nothing applied before approval


def test_patch_cannot_escape_workspace(demo, monkeypatch):
    async def evil(system, user, max_tokens=0, reasoning=None):
        return {'files': [{'path': '../../etc/passwd', 'content': 'x'}]}
    monkeypatch.setattr(selffix.llm, 'complete_json', evil)
    with pytest.raises(ValueError):
        asyncio.run(selffix.prepare_fix('demo-shop'))
