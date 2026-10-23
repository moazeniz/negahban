import asyncio, json, logging, time
import httpx
import pytest
from negahban import jobs, obs, store
from negahban.liara import Liara, LiaraError
from negahban.resilience import BREAKERS, Breaker, CircuitOpen


def test_breaker_opens_and_half_opens():
    b = Breaker('t', threshold=2, cooldown=0.05)
    b.fail(); b.check(); b.fail()
    with pytest.raises(CircuitOpen):
        b.check()
    time.sleep(0.06); b.check()
    assert b.state == 'half_open'
    b.ok(); assert b.state == 'closed'


def test_liara_circuit_opens_after_repeated_5xx():
    BREAKERS.pop('liara', None)
    n = {'c': 0}

    def h(req):
        n['c'] += 1
        return httpx.Response(503)
    li = Liara(token='t-123456', team='x', base='https://api.test', transport=httpx.MockTransport(h))
    for _ in range(3):
        with pytest.raises(LiaraError):
            asyncio.run(li.req('GET', '/v1/projects', retries=2))
    calls = n['c']
    with pytest.raises(LiaraError) as e:
        asyncio.run(li.req('GET', '/v1/projects'))
    assert 'circuit' in e.value.message and n['c'] == calls  # short-circuited, no request sent
    BREAKERS.pop('liara', None)


def test_json_log_line_has_standard_fields(capsys):
    obs.setup()
    tok = obs.request_id.set('rid-123')
    obs.event('http.request', 'info', 'GET / 200', route='/', status=200, duration_ms=3.2)
    obs.request_id.reset(tok)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    d = json.loads(line)
    assert {'ts', 'level', 'app', 'env', 'event', 'request_id', 'msg'} <= set(d) and d['event'] == 'http.request' and d['request_id'] == 'rid-123' and d['status'] == 200
    logging.getLogger().handlers[:] = []


def test_job_retry_then_dead_and_replay(fresh_db):
    jobs.ensure(); store.x('delete from jobs')
    calls = []

    @jobs.handler('test.flaky', max_attempts=2)
    async def flaky(p):
        calls.append(p)
        raise RuntimeError('boom')
    jid = jobs.enqueue('test.flaky', {'x': 1})
    r = jobs.get(jid)
    asyncio.run(jobs._run_one(r))
    assert jobs.get(jid)['state'] == 'retrying'
    asyncio.run(jobs._run_one(jobs.get(jid)))
    j = jobs.get(jid)
    assert j['state'] == 'dead' and 'boom' in j['last_error'] and len(calls) == 2
    assert asyncio.run(jobs.replay(jid, 'owner')) == {'requeued': jid} and jobs.get(jid)['state'] == 'queued'


def test_idempotency_key_dedupes(fresh_db):
    jobs.ensure()
    a = jobs.enqueue('test.flaky', {}, idem='same-key'); b = jobs.enqueue('test.flaky', {}, idem='same-key')
    assert a == b


def test_interrupted_proposal_job_goes_dead_not_rerun(fresh_db):
    jobs.ensure(); store.x('delete from jobs')
    jid = jobs.enqueue('proposal.execute', {'pid': 'p1', 'actor': 'owner', 'extra': {}}, max_attempts=1)
    store.x("update jobs set state='running' where id=?", (jid,))
    jobs.recover_on_start()
    assert jobs.get(jid)['state'] == 'dead'


def test_global_error_handler_hides_stack():
    from fastapi.testclient import TestClient
    from negahban import main

    @main.app.get('/__boom')
    async def boom():
        raise ValueError('secret internals')
    r = TestClient(main.app, raise_server_exceptions=False).get('/__boom')
    assert r.status_code == 500 and 'secret internals' not in r.text and r.json()['request_id'] and r.headers['x-request-id']
