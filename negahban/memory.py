"""Long-term memory: owner facts/preferences (remember/recall tools + dashboard editor), automatic incident memory
(root cause + fix + verification of every resolved incident, recalled in new diagnoses), and short summaries of past chat
sessions injected into the agent context."""
import re, time
from . import store
from .redact import redact
from .sanitize import clean

SCHEMA = """create table if not exists memory(id text primary key, kind text, app text, text text, meta text default '{}',
  created_at real, updated_at real, hits int default 0);"""


def ensure():
    from contextlib import closing
    with store._lock, closing(store._db()) as c:
        c.executescript(SCHEMA); c.commit()


def add(kind, text, app=None, meta=None, actor='owner'):
    ensure()
    text = redact(clean(text, 600)).strip()
    if not text:
        raise ValueError('empty')
    ex = store.q('select id from memory where kind=? and text=?', (kind, text), one=True)
    if ex:
        store.x('update memory set updated_at=? where id=?', (time.time(), ex['id']))
        return ex['id']
    mid = store.uid('mem_')
    store.x('insert into memory(id,kind,app,text,meta,created_at,updated_at) values(?,?,?,?,?,?,?)', (mid, kind, app, text, store.J(meta or {}), time.time(), time.time()))
    store.audit(actor, 'memory_add', app, {'id': mid, 'kind': kind})
    return mid


def update(mid, text, actor='owner'):
    ensure()
    store.x('update memory set text=?, updated_at=? where id=?', (redact(clean(text, 600)).strip(), time.time(), mid))
    store.audit(actor, 'memory_edit', None, {'id': mid})


def delete(mid, actor='owner'):
    ensure()
    store.x('delete from memory where id=?', (mid,))
    store.audit(actor, 'memory_delete', None, {'id': mid})


def listing(kind=None, limit=200):
    ensure()
    return store.q('select * from memory ' + ('where kind=? ' if kind else '') + 'order by updated_at desc limit ?', ((kind, limit) if kind else (limit,)))


def _words(s):
    return {w for w in re.findall(r'[\w؀-ۿ-]{2,}', (s or '').lower())}


def recall(query, kind=None, app=None, limit=8):
    """Tiny keyword search (token overlap) over memories; recent first on ties."""
    ensure()
    q = _words(query)
    rows = listing(kind, 500)
    scored = []
    for r in rows:
        if app and r['app'] and r['app'] != app:
            continue
        sc = len(q & _words(r['text'] + ' ' + (r['app'] or '')))
        if sc or not q:
            scored.append((sc, r['updated_at'], r))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out = [r for _, _, r in scored[:limit]]
    for r in out:
        store.x('update memory set hits=hits+1 where id=?', (r['id'],))
    return out


def remember_incident(inc, resolution):
    """Called when an incident resolves: keep app + kind + top hypothesis + what fixed it."""
    d = inc.get('diagnosis') or {}
    top = (d.get('hypotheses') or [{}])[0]
    text = f"{inc['app']}: «{inc['title']}» — علت: {top.get('title', '?')} — رفع: {resolution}"
    return add('incident', text, inc['app'], {'incident': inc['id'], 'hypothesis': top.get('id'), 'kind': inc.get('kind')}, actor='negahban')


def similar_incidents(app, hypothesis_id, limit=3):
    ensure()
    rows = store.q("select * from memory where kind='incident' and app=? order by updated_at desc limit 50", (app,))
    return [r for r in rows if f'"hypothesis": "{hypothesis_id}"' in (r['meta'] or '')][:limit]


def session_summary(session, question, answer):
    s = store.kv_get('session_summaries', [])
    s = [x for x in s if x['session'] != session]
    s.append({'session': session, 'ts': time.time(), 'q': clean(question, 160), 'a': clean(answer, 220)})
    store.kv_set('session_summaries', s[-12:])


def context_block(current_session):
    facts = listing('fact', 20) + listing('preference', 10)
    sums = [x for x in store.kv_get('session_summaries', []) if x['session'] != current_session][-5:]
    parts = []
    if facts:
        parts.append('Long-term memory about the owner (facts they asked you to remember):\n' + '\n'.join(f"- {f['text']}" for f in facts))
    if sums:
        parts.append('Recent past conversations (other sessions):\n' + '\n'.join(f"- Q: {x['q']} → A: {x['a']}" for x in sums))
    n = store.q("select count(*) n from memory where kind='incident'", one=True) if facts is not None else None
    if n and n['n']:
        parts.append(f"Incident memory: {n['n']} past incidents with root causes and fixes are searchable with the `recall` tool.")
    return ('\n\n' + '\n\n'.join(parts)) if parts else ''
