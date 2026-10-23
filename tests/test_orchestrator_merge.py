import asyncio
from negahban import orchestrator, store


def test_symptoms_of_same_cause_merge_into_one_incident(monkeypatch, fresh_db):
    store.upsert_app('demo-shop', 'demo')
    diag = {'hypotheses': [{'id': 'missing_env', 'title': 'متغیر PAYMENT_GATEWAY_KEY تنظیم نشده', 'confidence': 0.9, 'evidence': [], 'fixes': [], 'kind': 'config'}]}

    async def fake_diag(app, minutes=20, with_llm=True):
        return diag
    import negahban.diagnose as d
    monkeypatch.setattr(d, 'diagnose', fake_diag)
    acted = []

    async def fake_act(*a, **k):
        acted.append(a[1]['id'])
    monkeypatch.setattr(orchestrator, 'act', fake_act)
    a, _ = store.open_incident('demo-shop', 'crash', 'high', 'crash', 'crash')
    b, _ = store.open_incident('demo-shop', 'errors', 'high', 'errors', 'errors')
    info, pl, s = {'planID': 'small-g2'}, {'cpu': 0.5, 'ram': 0.5}, {'cpu': 0.1, 'mem': 0.1, 'latency_ms': 50}
    asyncio.run(orchestrator.handle_incident('demo-shop', a, 'crash', info, pl, s))
    asyncio.run(orchestrator.handle_incident('demo-shop', b, 'errors', info, pl, s))
    assert acted == [a['id']]
    assert store.get_incident(b['id'])['status'] == 'resolved' and 'ادغام' in store.get_incident(b['id'])['timeline'][-1]['text']
