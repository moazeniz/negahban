"""Control-plane self-protection: AI on/off switch, supervised background loops (restart with backoff), container
memory/CPU self-check with load shedding, bounded chat concurrency, and periodic SQLite snapshots of Negahban's state.
Monitoring, autopilot, health checks, status page and reports never depend on the LLM (see docs/CONTROL_PLANE.md)."""
import asyncio, gzip, os, shutil, sqlite3, time, traceback
from contextlib import closing
from pathlib import Path
from . import store
from .config import settings
from .obs import event

STATE = {'loops': {}, 'shed': False, 'shed_reason': None, 'mem': None, 'restart_requested': False}
CHAT_SLOTS = asyncio.Semaphore(3)   # at most 3 chat turns run at once; the rest are refused (back-pressure)
CHAT_WAITING = {'n': 0}
MAX_CHAT_WAITING = 6


# ---------------------------------------------------------------- AI switch
def ai_enabled():
    v = store.kv_get('ai_enabled')
    return True if v is None else bool(v)


def set_ai(on, actor='owner'):
    store.kv_set('ai_enabled', bool(on))
    store.audit(actor, 'ai_mode', None, {'ai_enabled': bool(on)})
    event('control.ai_mode', 'warn' if not on else 'info', f"AI {'enabled' if on else 'disabled'}", ai_enabled=bool(on))


# ---------------------------------------------------------------- supervisor
async def supervise(name, factory, base=2, cap=120):
    """Run a background loop forever; if it crashes, log app.crash + loop.restart and restart it with backoff."""
    delay = base
    st = STATE['loops'].setdefault(name, {'restarts': 0, 'last_error': None, 'running': False, 'since': None})
    while True:
        st.update(running=True, since=time.time())
        try:
            await factory()
            st['running'] = False
            event('loop.exit', 'warn', f'{name} returned; restarting', loop=name)
        except asyncio.CancelledError:
            st['running'] = False
            raise
        except Exception as e:
            st.update(running=False, last_error=f'{type(e).__name__}: {e}'[:300])
            event('app.crash', 'critical', f'{name} crashed', loop=name, error_type=type(e).__name__, error=str(e)[:300],
                  stack=traceback.format_exc()[-2000:])
        st['restarts'] += 1
        event('loop.restart', 'warn', f'restarting {name} in {delay}s', loop=name, backoff_ms=delay * 1000, attempt=st['restarts'])
        await asyncio.sleep(delay)
        delay = min(cap, delay * 2) if time.time() - (st['since'] or 0) < 300 else base


# ---------------------------------------------------------------- resource self-check
def _read(p):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return None


def mem_usage():
    """cgroup v2 (or v1) memory usage vs limit of THIS container."""
    cur, mx = _read('/sys/fs/cgroup/memory.current'), _read('/sys/fs/cgroup/memory.max')
    if cur is None:
        cur, mx = _read('/sys/fs/cgroup/memory/memory.usage_in_bytes'), _read('/sys/fs/cgroup/memory/memory.limit_in_bytes')
    try:
        c = int(cur)
        m = None if mx in (None, 'max') else int(mx)
        if m and m > 1 << 50:
            m = None
        return {'used_mb': round(c / 2 ** 20), 'limit_mb': round(m / 2 ** 20) if m else None, 'ratio': round(c / m, 3) if m else None}
    except (TypeError, ValueError):
        return None


async def self_check(every=20):
    high = 0
    while True:
        m = mem_usage()
        STATE['mem'] = m
        r = (m or {}).get('ratio') or 0
        if r >= 0.85 and not STATE['shed']:
            STATE.update(shed=True, shed_reason=f'memory {round(r * 100)}% of container limit')
            event('control.shed', 'warn', 'shedding non-essential work (chat/LLM paused, monitoring continues)', metric='memory_ratio', value=r)
        elif r < 0.75 and STATE['shed']:
            STATE.update(shed=False, shed_reason=None)
            event('control.recover', 'info', 'load shedding ended', metric='memory_ratio', value=r)
        high = high + 1 if r >= 0.95 else 0
        if high >= 3 and not STATE['restart_requested']:
            # cannot recover by itself: /healthz turns unhealthy so the external watchdog restarts the container via the Liara API
            STATE['restart_requested'] = True
            event('control.restart_requested', 'critical', 'memory stuck above 95%: asking the external watchdog for a restart', value=r)
        await asyncio.sleep(every)


# ---------------------------------------------------------------- snapshots
SNAP_DIR = lambda: settings.data_dir / 'snapshots'


def snapshot(kind='hourly'):
    """Consistent online copy of the state DB (sqlite backup API), gzip-compressed; retention 24 hourly + 7 daily."""
    d = SNAP_DIR(); d.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    tmp = d / f'.{ts}.db'
    with closing(sqlite3.connect(settings.data_dir / 'negahban.db', timeout=30)) as src, closing(sqlite3.connect(tmp)) as dst:
        src.backup(dst)
    out = d / f'negahban-{kind}-{ts}.db.gz'
    with open(tmp, 'rb') as f, gzip.open(out, 'wb', compresslevel=6) as g:
        shutil.copyfileobj(f, g)
    tmp.unlink(missing_ok=True)
    for k, keep in (('hourly', 24), ('daily', 7)):
        files = sorted(d.glob(f'negahban-{k}-*.db.gz'))
        for old in files[:-keep]:
            old.unlink(missing_ok=True)
    store.kv_set('snapshot_last', {'file': out.name, 'ts': time.time(), 'bytes': out.stat().st_size, 'kind': kind})
    event('snapshot.created', 'info', out.name, bytes=out.stat().st_size, kind=kind)
    return out


def snapshots():
    d = SNAP_DIR()
    if not d.exists():
        return []
    return [{'file': p.name, 'bytes': p.stat().st_size, 'ts': p.stat().st_mtime} for p in sorted(d.glob('negahban-*.db.gz'), reverse=True)]


async def snapshot_loop():
    while True:
        last = store.kv_get('snapshot_last') or {}
        if time.time() - (last.get('ts') or 0) >= 3600:
            daily_due = time.time() - (store.kv_get('snapshot_daily_ts') or 0) >= 86400
            await asyncio.to_thread(snapshot, 'daily' if daily_due else 'hourly')
            if daily_due:
                store.kv_set('snapshot_daily_ts', time.time())
        await asyncio.sleep(300)


def status():
    return {'ai_enabled': ai_enabled(), 'loops': STATE['loops'], 'shed': STATE['shed'], 'shed_reason': STATE['shed_reason'], 'memory': STATE['mem'],
            'restart_requested': STATE['restart_requested'], 'chat_waiting': CHAT_WAITING['n'], 'snapshot_last': store.kv_get('snapshot_last'),
            'snapshots': snapshots()[:10]}
