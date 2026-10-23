import asyncio, json
import httpx
from negahban import llm as L


def test_openai_to_anthropic_roundtrip():
    msgs = [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 't1', 'type': 'function', 'function': {'name': 'get_logs', 'arguments': '{"app":"x"}'}}]},
            {'role': 'tool', 'tool_call_id': 't1', 'content': 'data'}, {'role': 'tool', 'tool_call_id': 't2', 'content': 'more'}]
    tools = [{'type': 'function', 'function': {'name': 'get_logs', 'description': 'd', 'parameters': {'type': 'object', 'properties': {}}}}]
    system, out, atools = L.to_anthropic(msgs, tools)
    assert system == ['S'] and out[1]['content'][0] == {'type': 'tool_use', 'id': 't1', 'name': 'get_logs', 'input': {'app': 'x'}}
    assert [b['type'] for b in out[2]['content']] == ['tool_result', 'tool_result']  # parallel results in ONE user message
    assert atools == [{'name': 'get_logs', 'description': 'd', 'input_schema': {'type': 'object', 'properties': {}}}]
    m = L.from_anthropic({'content': [{'type': 'text', 'text': 'ok'}, {'type': 'tool_use', 'id': 'a', 'name': 'diagnose', 'input': {'app': 'y'}}], 'stop_reason': 'tool_use'})
    assert m['tool_calls'][0]['function']['name'] == 'diagnose' and json.loads(m['tool_calls'][0]['function']['arguments']) == {'app': 'y'}
    # raw blocks are replayed verbatim (append-only history)
    _, out2, _ = L.to_anthropic([{'role': 'user', 'content': 'q'}, m], None)
    assert out2[1]['content'] == m['_anthropic_content']


def test_fallback_from_gate_503_to_boxapi(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.url.host + req.url.path)
        if req.url.path.endswith('/v1/messages'):
            return httpx.Response(503, text='kill switch')
        assert 'max_completion_tokens' in json.loads(req.content) and 'temperature' not in json.loads(req.content)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'from box'}}], 'usage': {'total_tokens': 3}})
    monkeypatch.setattr(L, 'GATE_URL', 'http://gate.test/meow-llm'); monkeypatch.setattr(L, 'GATE_TOKEN', 'gate-token-xyz')
    c = L.LLM(keys=['k1-aaaaaaaa'], base='http://box.test/v1', transport=httpx.MockTransport(handler))
    monkeypatch.setattr(c, 'provider', lambda: 'anthropic')
    m = asyncio.run(c.chat([{'role': 'user', 'content': 'x'}]))
    assert m['content'] == 'from box' and m['_provider'] == 'boxapi' and c.stats['fallbacks'] == 1 and c.gate['last_status'] == 503


def test_anthropic_request_shape(monkeypatch):
    seen = {}

    def handler(req):
        seen['body'] = json.loads(req.content); seen['auth'] = req.headers['authorization']
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'سلام'}], 'stop_reason': 'end_turn', 'usage': {'input_tokens': 5, 'output_tokens': 2}})
    monkeypatch.setattr(L, 'GATE_URL', 'http://gate.test/meow-llm'); monkeypatch.setattr(L, 'GATE_TOKEN', 'gate-token-xyz')
    c = L.LLM(keys=[], transport=httpx.MockTransport(handler))
    monkeypatch.setattr(c, 'provider', lambda: 'anthropic')
    tools = [{'type': 'function', 'function': {'name': 'a', 'description': 'b', 'parameters': {'type': 'object'}}}]
    m = asyncio.run(c.chat([{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'hi'}], tools=tools, max_tokens=9000))
    b = seen['body']
    assert m['content'] == 'سلام' and b['max_tokens'] == 5000 and b['system'][0]['text'] == 'sys' and b['system'][0]['cache_control'] == {'type': 'ephemeral'} and b['tools'][-1]['cache_control'] == {'type': 'ephemeral'} and 'temperature' not in b and b['model'] == 'claude-haiku-5-5' and b['thinking'] == {'type': 'disabled'}
    assert seen['auth'] == 'Bearer gate-token-xyz'


def test_usage_is_logged_and_persisted(monkeypatch, fresh_db):
    def handler(req):
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn',
                                         'usage': {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 4000, 'cache_creation_input_tokens': 0}})
    monkeypatch.setattr(L, 'GATE_URL', 'http://gate.test/meow-llm'); monkeypatch.setattr(L, 'GATE_TOKEN', 'gate-token-xyz')
    c = L.LLM(keys=[], transport=httpx.MockTransport(handler))
    monkeypatch.setattr(c, 'provider', lambda: 'anthropic')
    L.feature.set('chat')
    m = asyncio.run(c.chat([{'role': 'user', 'content': 'hi'}]))
    assert m['_usage']['cache_read'] == 4000 and m['_usage']['cost_usd'] > 0
    rows = [r for r in L.usage_rows(1) if r['provider'] == 'anthropic' and r['feature'] == 'chat']
    assert rows and rows[0]['feature'] == 'chat' and rows[0]['cache_read'] == 4000 and rows[0]['calls'] == 1
