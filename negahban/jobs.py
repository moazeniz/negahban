"""Durable SQLite job queue with retries and a dead-letter state (TEAM_BRIEF standard).
States: queued -> running -> succeeded | retrying -> dead. Idempotency keys de-duplicate. Non-idempotent work
(executing an approved proposal) gets max_attempts=1; if Negahban restarts mid-run the job is moved to dead with a note
and can only be *replayed as a new approval card* - never silently re-executed."""
import asyncio, json, time, traceback
from . import bus, store
from .obs import event
from .redact import redact
from .resilience import backoff

HANDLERS = {}
SCHEMA = """create table if not exists jobs(id text primary key, kind text, payload text, state text, attempts int default 0,
  max_attempts int default 3, next_at real, last_error text, idem text unique, created_at real, updated_at real, result text);
create index if not exists jobs_state on jobs(state, next_at);"""
_wake = None


def ensure():
    with store._lock:
        from contextlib import closing
        with closing(store._db()) as c:
            c.executescript(SCHEMA); c.commit()


def handler(kind, max_attempts=3):
    def deco(fn):
        HANDLERS[kind] = (fn, max_attempts)
        return fn
    return deco


_loop = None


def running():
    """True only if the worker runs in the *current* event loop (a worker started by another loop, e.g. a test client, is dead)."""
    try:
        return _wake is not None and _loop is asyncio.get_running_loop()
    except RuntimeError:
        return False


def submit(kind, payload, idem=None, max_attempts=None):
    """Durable when the worker runs (production); direct task otherwise (tests, CLI scripts)."""
    if running():
        return enqueue(kind, payload, idem, max_attempts)
    fn, _ = HANDLERS[kind]
    asyncio.get_event_loop().create_task(fn(payload))
    return None


def enqueue(kind, payload, idem=None, max_attempts=None, delay=0):
    ensure()
    if idem:
        ex = store.q('select id from jobs where idem=?', (idem,), one=True)
        if ex:
            return ex['id']
    jid = store.uid('job_')
    now = time.time()
    store.x('insert into jobs(id,kind,payload,state,max_attempts,next_at,idem,created_at,updated_at) values(?,?,?,?,?,?,?,?,?)',
            (jid, kind, json.dumps(payload, ensure_ascii=False, default=str), 'queued', max_attempts or HANDLERS.get(kind, (None, 3))[1], now + delay, idem, now, now))
    if _wake:
        _wake.set()
    return jid


def get(jid):
    r = store.q('select * from jobs where id=?', (jid,), one=True)
    if r:
        r['payload'] = json.loads(r['payload'] or '{}')
    return r


def listing(state=None, limit=100):
    ensure()
    rows = store.q('select * from jobs ' + ('where state=? ' if state else '') + 'order by updated_at desc limit ?', ((state, limit) if state else (limit,)))
    for r in rows:
        r['payload'] = {k: v for k, v in json.loads(r['payload'] or '{}').items() if k not in ('value',)}
    return rows


def stats():
    ensure()
    rows = store.q('select state, count(*) n from jobs group by state')
    return {r['state']: r['n'] for r in rows}


def recover_on_start():
    """Jobs left 'running' by a crash/restart: idempotent kinds are re-queued, non-idempotent ones go to dead."""
    ensure()
    for r in store.q("select * from jobs where state='running'"):
        if r['max_attempts'] > 1:
            store.x("update jobs set state='queued', next_at=?, updated_at=? where id=?", (time.time(), time.time(), r['id']))
        else:
            store.x("update jobs set state='dead', last_error=?, updated_at=? where id=?", ('interrupted by a restart of Negahban (not re-run automatically)', time.time(), r['id']))
            event('job.dead_letter', 'error', 'interrupted job moved to dead-letter', queue='main', job_id=r['id'], error_type='Interrupted')


async def _run_one(r):
    fn, _ = HANDLERS.get(r['kind'], (None, 1))
    store.x("update jobs set state='running', attempts=attempts+1, updated_at=? where id=?", (time.time(), r['id']))
    attempt = r['attempts'] + 1
    try:
        if not fn:
            raise RuntimeError(f"no handler for {r['kind']}")
        pl = r['payload'] if isinstance(r['payload'], dict) else json.loads(r['payload'] or '{}')
        res = await fn(pl)
        store.x("update jobs set state='succeeded', result=?, updated_at=? where id=?", (json.dumps(res, default=str)[:4000] if res is not None else None, time.time(), r['id']))
    except Exception as e:
        err = redact(f'{type(e).__name__}: {e}')[:500]
        if attempt < r['max_attempts']:
            delay = backoff(attempt, base=5, cap=300)
            store.x("update jobs set state='retrying', last_error=?, next_at=?, updated_at=? where id=?", (err, time.time() + delay, time.time(), r['id']))
            event('job.retry', 'warn', f"{r['kind']} retry {attempt}/{r['max_attempts']}", queue='main', job_id=r['id'], attempt=attempt,
                  max_attempts=r['max_attempts'], backoff_ms=round(delay * 1000), error=err)
        else:
            store.x("update jobs set state='dead', last_error=?, updated_at=? where id=?", (err, time.time(), r['id']))
            event('job.dead_letter', 'error', f"{r['kind']} dead after {attempt} attempts", queue='main', job_id=r['id'], error=err,
                  stack=redact(traceback.format_exc())[-2000:])
            bus.publish('job', job={'id': r['id'], 'kind': r['kind'], 'state': 'dead', 'error': err})


async def worker():
    global _wake, _loop
    _wake = asyncio.Event()
    _loop = asyncio.get_running_loop()
    ensure()
    recover_on_start()
    last_metric = 0
    while True:
        due = store.q("select * from jobs where state in ('queued','retrying') and next_at<=? order by next_at limit 5", (time.time(),))
        for r in due:
            asyncio.create_task(_run_one(r))
            store.x("update jobs set state='running' where id=? and state in ('queued','retrying')", (r['id'],))
        if time.time() - last_metric > 60:
            st = stats(); last_metric = time.time()
            event('queue.metrics', 'info', 'queue depth', queue='main', metric='queue.depth', value=st.get('queued', 0) + st.get('retrying', 0), dead=st.get('dead', 0))
        try:
            await asyncio.wait_for(_wake.wait(), 2)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


async def replay(jid, actor):
    """Dead job -> back to the queue (idempotent kinds) or -> a NEW approval card (proposal executions)."""
    r = get(jid)
    if not r or r['state'] != 'dead':
        return {'error': 'فقط کارهای مرده (dead) قابل اجرای دوباره هستند'}
    store.audit(actor, 'job_replay', None, {'job': jid, 'kind': r['kind']})
    if r['kind'] == 'proposal.execute':
        p = store.get_proposal(r['payload'].get('pid'))
        if not p:
            return {'error': 'پیشنهاد اصلی پیدا نشد'}
        from .approvals import propose
        new = await propose(p['tool'], {k: v for k, v in (p['args'] or {}).items() if k != 'value_ref'}, {'origin': 'replay', 'actor': actor, 'why': f"اجرای دوباره‌ی کار مرده {jid}: {p['why']}"})
        store.x("update jobs set state='discarded', updated_at=? where id=?", (time.time(), jid))
        return {'replayed_as_proposal': new['id']}
    store.x("update jobs set state='queued', attempts=0, next_at=?, updated_at=? where id=?", (time.time(), time.time(), jid))
    if _wake:
        _wake.set()
    return {'requeued': jid}


def discard(jid, actor):
    store.audit(actor, 'job_discard', None, {'job': jid})
    store.x("update jobs set state='discarded', updated_at=? where id=? and state='dead'", (time.time(), jid))
    return {'discarded': jid}
