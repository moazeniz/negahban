"""Multi-team support. Negahban manages one PRODUCTION team (LIARA_TEAM_ID) and optionally more teams (LIARA_TEAMS), e.g. a
separate test team for create demos. The team of the current operation lives in a context variable: the Liara client reads it
on every call, the dashboard sets it per request (X-Ngh-Team header), approval cards carry it in their args and re-enter it on
approval/execution, and the orchestrator enters each app's team. Default = the production team.

LIARA_TEAMS = JSON list: [{"id": "...", "name": "دوخت"}, {"id": "...", "name": "لیارا", "token_env": "LIARA_API_TOKEN_2", "test": true}]
token_env is an optional per-team token override (default: LIARA_API_TOKEN). Names are refreshed from GET /v2/teams."""
import contextvars, json, os, re, time
from contextlib import contextmanager
from .config import settings

CUR = contextvars.ContextVar('negahban_team', default=None)
_names = {'t': 0, 'v': {}}
COLORS = {False: '#0f766e', True: '#c2410c'}  # production teal, test team orange (also used by the dashboard)


def _cfg():
    try:
        rows = json.loads(os.environ.get('LIARA_TEAMS') or '[]')
    except ValueError:
        rows = []
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and re.fullmatch(r'[0-9a-f]{24}', str(r.get('id', ''))) and r['id'] not in seen:
            seen.add(r['id'])
            out.append({'id': r['id'], 'name': str(r.get('name') or r['id'][-6:]), 'token_env': r.get('token_env') or '', 'test': bool(r.get('test'))})
    d = default_id()
    if d and d not in seen:
        out.insert(0, {'id': d, 'name': 'production', 'token_env': '', 'test': False})
    for r in out:
        r['default'] = r['id'] == d
        if r['default']:
            r['test'] = False
        r['name'] = _names['v'].get(r['id'], r['name'])
    out.sort(key=lambda r: not r['default'])
    return out


def default_id():
    return settings.liara_team


def all_teams():
    return _cfg()


def ids():
    return [t['id'] for t in _cfg()]


def get(ref):
    """Team by id or by name (case/space-insensitive, e.g. 'لیارا', 'دوخت', 'test', 'production'); None if unknown."""
    if not ref:
        return None
    r = str(ref).strip().lower()
    for t in _cfg():
        if r in (t['id'], t['name'].strip().lower()) or (r in ('test', 'آزمایشی', 'تست') and t['test']) or (r in ('production', 'prod', 'اصلی') and t['default']):
            return t
    return None


def resolve(ref):
    """Team id for a ref; raises ValueError (Persian message) for an unknown team."""
    if not ref:
        return current()
    t = get(ref)
    if not t:
        raise ValueError(f"تیم «{ref}» شناخته نیست؛ تیم‌های پیکربندی‌شده: {', '.join(x['name'] for x in _cfg())}")
    return t['id']


def current():
    v = CUR.get()
    return v if v in ids() else default_id()


def is_default(team_id=None):
    return (team_id or current()) == default_id()


@contextmanager
def use(team):
    """Run a block in a team (id or name). Unknown/empty => the production team."""
    t = get(team) if team else None
    tok = CUR.set(t['id'] if t else default_id())
    try:
        yield tok
    finally:
        CUR.reset(tok)


def token_for(team_id):
    """Per-team token override (token_env), else None = the client's own token (the same Liara user works for every team)."""
    for t in _cfg():
        if t['id'] == team_id and t['token_env'] and os.environ.get(t['token_env']):
            return os.environ[t['token_env']]
    return None


def secret_values():
    return [os.environ.get(t['token_env'], '') for t in _cfg() if t['token_env']]


def label(team_id=None):
    tid = team_id or current()
    t = next((x for x in _cfg() if x['id'] == tid), None) or {'id': tid, 'name': str(tid)[-6:], 'test': False, 'default': tid == default_id()}
    return {'id': t['id'], 'name': t['name'], 'test': t['test'], 'default': t.get('default', False), 'color': COLORS[bool(t['test'])],
            'label_fa': f"تیم {t['name']}" + (' (آزمایشی)' if t['test'] else ' (اصلی)' if t.get('default') else '')}


def app_team(app_row):
    """Team of a registered app row (meta.team), default = production."""
    m = (app_row or {}).get('meta') or {}
    return m.get('team') if m.get('team') in ids() else default_id()


def team_of_app(name):
    from . import store
    a = store.get_app(name) if name else None
    return app_team(a) if a else None


async def refresh_names(force=False):
    """Team names from GET /v2/teams (cached 1 h)."""
    if not force and time.time() - _names['t'] < 3600:
        return _names['v']
    from .liara import liara
    try:
        d = await liara.get('/v2/teams', team='')
        _names['v'] = {t['_id']: t.get('name') or t['_id'][-6:] for t in d.get('teams') or []}
    except Exception:
        pass
    _names['t'] = time.time()
    return _names['v']


def mentioned(text):
    """Teams named in a chat message («در تیم لیارا», «تیم دوخت», 'test team')."""
    s = (text or '').lower()
    out = []
    for t in _cfg():
        n = t['name'].strip().lower()
        if n and (f'تیم {n}' in s or f'team {n}' in s):  # bare «لیارا» is the platform name, so a team needs «تیم …»
            out.append(t['id'])
        elif t['test'] and re.search(r'تیم\s*(تست|آزمایشی)|test team', s):
            out.append(t['id'])
    return list(dict.fromkeys(out))


async def locate_app(name, client=None):
    """Which configured team owns a Liara app (app names are global on Liara). None if not found anywhere."""
    from .liara import LiaraError, liara
    liara = client or liara
    for t in _cfg():
        try:
            await liara.get(f'/v1/projects/{name}', team=t['id'], retries=1)
            return t['id']
        except LiaraError:
            continue
    return None


async def require_app_in_team(app, client=None):
    """Cross-team guard: the app must exist in the CURRENT team (e.g. a DB of team A is never wired into an app of team B)."""
    from .liara import LiaraError, liara
    liara = client or liara
    try:
        await liara.get(f'/v1/projects/{app}', retries=1)
        return
    except LiaraError as e:
        if e.status not in (403, 404):
            raise
    where = await locate_app(app, liara)
    here = label()['label_fa']
    if where and where != current():
        raise ValueError(f"برنامه‌ی {app} در {label(where)['label_fa']} است، نه در {here}؛ اتصال منابع بین دو تیم ممکن نیست (شبکه و صورتحساب جداست). "
                         f"منبع را در همان تیم برنامه بسازید.")
    raise ValueError(f'برنامه‌ی {app} در {here} پیدا نشد.')
