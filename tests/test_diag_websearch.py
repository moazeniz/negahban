import asyncio, json
import httpx
import pytest
from negahban import llm
from negahban.diag import websearch as ws

GATE_REPLY = {'content': [
    {'type': 'server_tool_use', 'id': 's1', 'name': 'web_search', 'input': {'query': 'sqlite database is locked fastapi'}},
    {'type': 'web_search_tool_result', 'tool_use_id': 's1', 'content': [{'type': 'web_search_result', 'title': 'Fix locked', 'url': 'https://a.example/x', 'page_age': '2025'}]},
    {'type': 'text', 'text': '- Cause: one writer. '},
    {'type': 'text', 'text': 'Use WAL mode', 'citations': [{'type': 'web_search_result_location', 'url': 'https://a.example/x', 'title': 'Fix locked', 'cited_text': 'WAL reduces lock contention'}]}],
    'stop_reason': 'end_turn'}


def run(c):
    return asyncio.run(c)


def test_scrub_removes_personal_data():
    q = ws.scrub('KeyError token=liara-test-token-abcdef user ali@example.com from 10.1.2.3:5432 id 3f2b1c4d5e6f7a8b9c0d /home/ali/projects/shop/app/main.py')
    assert 'liara-test-token-abcdef' not in q and 'ali@example.com' not in q and '10.1.2.3' not in q and '3f2b1c4d5e6f7a8b9c0d' not in q
    assert 'main.py' in q and '/home/ali' not in q


def test_parse_anthropic_and_responses():
    p = ws.parse_anthropic(GATE_REPLY)
    assert p['citations'] == [{'url': 'https://a.example/x', 'title': 'Fix locked', 'cited_text': 'WAL reduces lock contention'}]
    assert p['results'][0]['url'] == 'https://a.example/x' and p['searches'] == ['sqlite database is locked fastapi'] and 'WAL' in p['answer']
    r = ws.parse_responses({'output': [{'type': 'web_search_call'}, {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Use WAL mode now',
                            'annotations': [{'type': 'url_citation', 'url': 'https://b.example', 'title': 'B', 'start_index': 4, 'end_index': 12}]}]}]})
    assert r['citations'][0]['cited_text'] == 'WAL mode' and r['answer'] == 'Use WAL mode now'


def test_gate_request_shape_and_cache(monkeypatch, fresh_db):
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json=GATE_REPLY)
    monkeypatch.setattr(ws, '_transport', httpx.MockTransport(h))
    monkeypatch.setattr(llm, 'GATE_URL', 'https://gate.test')
    monkeypatch.setattr(llm, 'GATE_TOKEN', 'gate-token-123456')
    r = run(ws.search_error('sqlite3.OperationalError: database is locked', 'FastAPI'))
    assert r['available'] and r['provider'] == 'anthropic-gate' and r['citations'][0]['url'] == 'https://a.example/x'
    body = json.loads(seen[0].content)
    assert seen[0].url == 'https://gate.test/v1/messages' and seen[0].headers['authorization'] == 'Bearer gate-token-123456'
    assert body['tools'][0]['type'] == 'web_search_20250305' and body['max_tokens'] <= 5000 and 'temperature' not in body and body['output_config'] == {'effort': 'low'}
    r2 = run(ws.search_error('sqlite3.OperationalError: database is locked', 'FastAPI'))
    assert r2.get('cached') and len(seen) == 1


def test_falls_back_to_boxapi_then_degrades(monkeypatch, fresh_db):
    def h(req):
        if 'gate.test' in str(req.url):
            return httpx.Response(503, json={'error': 'kill switch'})
        return httpx.Response(200, json={'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'fix it', 'annotations': []}]}]})
    monkeypatch.setattr(ws, '_transport', httpx.MockTransport(h))
    monkeypatch.setattr(llm, 'GATE_URL', 'https://gate.test')
    monkeypatch.setattr(llm, 'GATE_TOKEN', 'gate-token-123456')
    r = run(ws.search_error('ModuleNotFoundError: No module named foo', None))
    assert r['available'] and r['provider'] == 'boxapi' and r['answer'] == 'fix it'
    monkeypatch.setattr(ws, '_transport', httpx.MockTransport(lambda req: httpx.Response(503)))
    r = run(ws.search_error('ImportError: cannot import name bar', None))
    assert r['available'] is False and 'gate HTTP 503' in r['reason'] and len(r['suggested_queries']) == 3
