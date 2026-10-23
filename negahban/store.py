"""SQLite state: apps, samples, incidents, proposals, audit log, policies, release archive index, chat, kv.
Connections are opened per operation and always closed (no leaked handles on the Liara disk)."""
import json, sqlite3, threading, time, uuid
from contextlib import closing
from .config import settings
from .redact import redact, redact_obj

_lock = threading.RLock()
SCHEMA = """
create table if not exists apps(name text primary key, role text, url text, health_path text default '/', port int default 8080,
  added_at real, meta text default '{}');
create table if not exists samples(app text, ts real, cpu real, mem real, cpu_limit real, mem_limit real, latency_ms real, status int, ok int);
create index if not exists samples_app_ts on samples(app, ts);
create table if not exists incidents(id text primary key, app text, kind text, severity text, title text, status text,
  opened_at real, updated_at real, resolved_at real, diagnosis text default '{}', fingerprint text, timeline text default '[]');
create table if not exists proposals(id text primary key, app text, tool text, args text, title text, why text, impact text,
  rollback text, status text, created_at real, expires_at real, fingerprint text, origin text, incident_id text,
  result text, verification text, decided_by text, decided_at real, auto int default 0);
create table if not exists audit(id integer primary key autoincrement, ts real, actor text, action text, app text, detail text, ok int);
create table if not exists policies(app text primary key, value text);
create table if not exists archive(id text primary key, app text, ts real, release_id text, sha256 text, path text, message text,
  commit_sha text, origin text, size int, state text);
create table if not exists chat(id integer primary key autoincrement, session text, role text, content text, ts real, meta text default '{}');
create table if not exists kv(k text primary key, v text);
"""


def _db():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.data_dir / 'negahban.db', timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, closing(_db()) as c:
        c.executescript(SCHEMA)
        c.execute('pragma journal_mode=wal')
        c.commit()


def q(sql, args=(), one=False):
    with _lock, closing(_db()) as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock, closing(_db()) as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


J = lambda o: json.dumps(o, ensure_ascii=False, default=str)
uid = lambda p='': p + uuid.uuid4().hex[:12]


def _load(row, *keys):
    if row:
        for k in keys:
            if row.get(k):
                try:
                    row[k] = json.loads(row[k])
                except ValueError:
                    pass
    return row


# ---- kv -------------------------------------------------------------------
def kv_get(k, d=None):
    r = q('select v from kv where k=?', (k,), one=True)
    return json.loads(r['v']) if r else d


def kv_set(k, v):
    x('insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v', (k, J(v)))


# ---- audit ------------------------------------------------------------------
def audit(actor, action, app=None, detail=None, ok=True):
    x('insert into audit(ts,actor,action,app,detail,ok) values(?,?,?,?,?,?)',
      (time.time(), actor, action, app, redact(J(redact_obj(detail or {}))), int(bool(ok))))


def audit_list(limit=200, app=None):
    rows = q('select * from audit ' + ('where app=? ' if app else '') + 'order by id desc limit ?', ((app, limit) if app else (limit,)))
    return [_load(r, 'detail') for r in rows]


# ---- apps -------------------------------------------------------------------
def upsert_app(name, role='monitored', url=None, health_path='/', port=8080, meta=None):
    x("""insert into apps(name,role,url,health_path,port,added_at,meta) values(?,?,?,?,?,?,?)
         on conflict(name) do update set role=excluded.role, url=coalesce(excluded.url, apps.url), health_path=excluded.health_path,
         port=excluded.port, meta=excluded.meta""",
      (name, role, url or f'https://{name}.liara.run', health_path, port, time.time(), J(meta or {})))


def apps():
    return [_load(r, 'meta') for r in q('select * from apps order by added_at')]


def get_app(name):
    return _load(q('select * from apps where name=?', (name,), one=True), 'meta')


def remove_app(name):
    x('delete from apps where name=?', (name,))


# ---- samples ------------------------------------------------------------------
def add_sample(app, **kw):
    x('insert into samples(app,ts,cpu,mem,cpu_limit,mem_limit,latency_ms,status,ok) values(?,?,?,?,?,?,?,?,?)',
      (app, kw.get('ts', time.time()), kw.get('cpu'), kw.get('mem'), kw.get('cpu_limit'), kw.get('mem_limit'),
       kw.get('latency_ms'), kw.get('status'), int(bool(kw.get('ok')))))


def samples(app, since_s=3600):
    return q('select * from samples where app=? and ts>? order by ts', (app, time.time() - since_s))


def prune_samples(keep_s=3 * 86400):
    x('delete from samples where ts<?', (time.time() - keep_s,))


# ---- incidents -------------------------------------------------------------------
def open_incident(app, kind, severity, title, fingerprint, diagnosis=None):
    ex = q("select * from incidents where app=? and fingerprint=? and status!='resolved'", (app, fingerprint), one=True)
    if ex:
        return _load(ex, 'diagnosis', 'timeline'), False
    iid = uid('inc_')
    now = time.time()
    x('insert into incidents(id,app,kind,severity,title,status,opened_at,updated_at,diagnosis,fingerprint,timeline) values(?,?,?,?,?,?,?,?,?,?,?)',
      (iid, app, kind, severity, title, 'open', now, now, J(diagnosis or {}), fingerprint, J([{'ts': now, 'text': 'رخداد شناسایی شد: ' + title}])))
    return get_incident(iid), True


def get_incident(iid):
    return _load(q('select * from incidents where id=?', (iid,), one=True), 'diagnosis', 'timeline')


def incidents(limit=50, app=None, open_only=False):
    w, a = [], []
    if app:
        w.append('app=?'); a.append(app)
    if open_only:
        w.append("status!='resolved'")
    rows = q('select * from incidents ' + ('where ' + ' and '.join(w) if w else '') + ' order by opened_at desc limit ?', (*a, limit))
    return [_load(r, 'diagnosis', 'timeline') for r in rows]


def update_incident(iid, status=None, diagnosis=None, note=None, severity=None, title=None):
    inc = get_incident(iid)
    if not inc:
        return None
    tl = inc['timeline'] or []
    if note:
        tl.append({'ts': time.time(), 'text': note})
    now = time.time()
    x('update incidents set status=coalesce(?,status), diagnosis=coalesce(?,diagnosis), timeline=?, updated_at=?, severity=coalesce(?,severity), title=coalesce(?,title), resolved_at=case when ?=\'resolved\' then ? else resolved_at end where id=?',
      (status, J(diagnosis) if diagnosis is not None else None, J(tl[-60:]), now, severity, title, status, now, iid))
    return get_incident(iid)


# ---- proposals -------------------------------------------------------------------------
def add_proposal(p):
    x("""insert into proposals(id,app,tool,args,title,why,impact,rollback,status,created_at,expires_at,fingerprint,origin,incident_id,auto)
         values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (p['id'], p.get('app'), p['tool'], J(p.get('args', {})), p['title'], p.get('why', ''), J(p.get('impact', {})), p.get('rollback', ''),
       p.get('status', 'pending'), p['created_at'], p['expires_at'], p.get('fingerprint', ''), p.get('origin', 'chat'), p.get('incident_id'), int(p.get('auto', 0))))
    return get_proposal(p['id'])


def get_proposal(pid):
    return _load(q('select * from proposals where id=?', (pid,), one=True), 'args', 'impact', 'result', 'verification')


def proposals(status=None, limit=100, app=None):
    w, a = [], []
    if status:
        w.append('status=?'); a.append(status)
    if app:
        w.append('app=?'); a.append(app)
    rows = q('select * from proposals ' + ('where ' + ' and '.join(w) if w else '') + ' order by created_at desc limit ?', (*a, limit))
    return [_load(r, 'args', 'impact', 'result', 'verification') for r in rows]


def set_proposal(pid, **kw):
    sets, a = [], []
    for k, v in kw.items():
        sets.append(f'{k}=?'); a.append(J(v) if k in ('result', 'verification', 'impact', 'args') else v)
    x(f"update proposals set {', '.join(sets)} where id=?", (*a, pid))
    return get_proposal(pid)


def claim_proposal(pid, frm, to):
    """Atomic state transition (one-use approvals): returns True only for the caller that won the race."""
    with _lock, closing(_db()) as c:
        cur = c.execute('update proposals set status=? where id=? and status=?', (to, pid, frm))
        c.commit()
        return cur.rowcount == 1


# ---- policies ------------------------------------------------------------------------------
DEFAULT_POLICY = {'autopilot': False, 'resize': False, 'min_plan': 'small-g2', 'max_plan': 'medium-g2', 'max_toman_day': 60000,
                  'up_cpu': 0.8, 'up_mem': 0.8, 'up_minutes': 5, 'scale_down': False, 'down_cpu': 0.25, 'down_mem': 0.45, 'down_minutes': 30,
                  'restart_on_crash': False, 'auto_fix_code': False, 'auto_rollback': False, 'write_lock': False, 'env_lock': False}


def policy(app):
    r = q('select value from policies where app=?', (app,), one=True)
    return {**DEFAULT_POLICY, **(json.loads(r['value']) if r else {})}


def set_policy(app, value):
    x('insert into policies(app,value) values(?,?) on conflict(app) do update set value=excluded.value', (app, J({**DEFAULT_POLICY, **value})))


# ---- archive ------------------------------------------------------------------------------------
def add_archive(**kw):
    x('insert into archive(id,app,ts,release_id,sha256,path,message,commit_sha,origin,size,state) values(?,?,?,?,?,?,?,?,?,?,?)',
      (kw['id'], kw['app'], time.time(), kw.get('release_id'), kw.get('sha256'), kw['path'], kw.get('message'), kw.get('commit_sha'),
       kw.get('origin'), kw.get('size'), kw.get('state', 'BUILDING')))


def archives(app, limit=30):
    return q('select * from archive where app=? order by ts desc limit ?', (app, limit))


def set_archive_state(aid, state):
    x('update archive set state=? where id=?', (state, aid))


# ---- chat --------------------------------------------------------------------------------------
def chat_add(session, role, content, meta=None):
    return x('insert into chat(session,role,content,ts,meta) values(?,?,?,?,?)', (session, role, content, time.time(), J(meta or {})))


def chat_history(session, limit=40):
    rows = q('select * from chat where session=? order by id desc limit ?', (session, limit))
    return [_load(r, 'meta') for r in reversed(rows)]
