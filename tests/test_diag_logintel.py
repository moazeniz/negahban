from pathlib import Path
from negahban.diag import logintel as li

FIX = Path(__file__).parent / 'fixtures' / 'diag' / 'mixed.log'


def rows():
    return [{'ts': 1_800_000_000 + i, 'line': l} for i, l in enumerate(FIX.read_text().splitlines())]


def test_parse_line_kinds():
    j = li.parse_line('{"level":"error","event":"http.request","request_id":"r1","route":"/a/12","status":500,"duration_ms":12.5,"error_type":"KeyError"}')
    assert j['kind'] == 'json' and j['status'] == 500 and j['duration_ms'] == 12.5 and j['error_type'] == 'KeyError'
    u = li.parse_line('INFO:     10.0.0.5:51234 - "GET /api/items/44 HTTP/1.1" 200 OK')
    assert u['kind'] == 'access' and u['status'] == 200 and u['route'] == '/api/items/44'
    n = li.parse_line('10.0.0.9 - - [22/Oct/2026:10:00:02 +0000] "GET /api/report HTTP/1.1" 502 0 "-" "curl/8" 3.400')
    assert n['kind'] == 'nginx' and n['status'] == 502 and n['duration_ms'] == 3400.0 and n['level'] == 'error'
    k = li.parse_line('Killed process 7 (python) total-vm:900000kB')
    assert k['oom'] and k['level'] == 'critical'
    assert li.norm_route('/api/items/42?x=1') == '/api/items/:id' and li.norm_route('/o/3f2b1c4d-1111-2222-3333-444455556666/x') == '/o/:uuid/x'


def test_analyze_groups_routes_llm():
    s = li.analyze(rows())
    fps = {g['fingerprint']: g for g in s['error_groups']}
    # the JSON error event and the plain traceback share one fingerprint (type + innermost app frame)
    zd = fps['ZeroDivisionError@pricing.py:discount_percent']
    assert zd['count'] == 2 and 'req-bbb222' in zd['request_ids'] and zd['top_frame']['file'] == '/app/shop/pricing.py'
    assert 'TypeError@users.js:getUser' in fps                   # node stack, vendor frames skipped
    assert 'json.decoder.JSONDecodeError@catalog.py:load' in fps  # traceback whose header was cut off
    assert not any('ASGI' in f for f in fps)                      # uvicorn prelude line is not its own group
    assert s['requests'] == 7 and s['errors_5xx'] == 3
    slow = {r['route']: r for r in s['slowest_routes']}
    assert slow['POST /api/checkout']['p95_ms'] == 2600 and slow['GET /api/report']['p95_ms'] == 3400.0
    llm = {r['model']: r for r in s['llm']}
    assert llm['boxapi/openai/gpt-6-luna']['tokens_in'] == 1200 and llm['boxapi/openai/gpt-6-luna']['cost'] == 0.004
    assert llm['anthropic/claude-haiku-5-5']['errors'] == 1
    assert len(s['oom']) == 1 and s['startups'] == 1 and s['structured_ratio'] > 0.1


def test_compact_is_small_and_trace():
    s = li.analyze(rows() * 30)
    c = li.compact(s)
    import json
    assert len(json.dumps(c, ensure_ascii=False)) < 9000 and 'errors_per_minute' not in c
    t = li.trace(rows(), 'req-bbb222')
    assert t['count'] == 2 and t['errors'] == 1 and t['events'][0]['event'] == 'llm.call'


def test_restart_loop_detection():
    r = [{'ts': 1000 + i * 30, 'line': 'INFO:     Uvicorn running on http://0.0.0.0:8080'} for i in range(4)]
    assert li.analyze(r)['restart_loop']
