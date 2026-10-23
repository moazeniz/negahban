"""Template / wizard add-ons: "with database" and "with storage". A template (or the initial-setup wizard) may carry
  addons = {'database': {'engine': 'postgres', 'plan': None, 'bundle': 'basic', 'prefix': 'DB'}, 'storage': {'plan': '20g-g2', 'permission': 'private', 'style': 'liara'}}
propose_addons(app, addons, ctx) turns them into approval cards next to the app card: create_database / create_bucket, each
with connect_app=app (create + connect in one card). suggest(analysis) derives add-ons from analyze_project output
(e.g. psycopg / DATABASE_URL => postgres; boto3 / S3_* => storage). Nothing is created here; cards only."""
import re
from .common import DiagError

DB_DEFAULT = {'engine': 'postgres', 'plan': None, 'bundle': 'basic', 'prefix': 'DB'}
ST_DEFAULT = {'plan': '20g-g2', 'permission': 'private', 'style': 'liara'}
_ENGINE_HINT = {'PostgreSQL': 'postgres', 'MySQL': 'mysql', 'MongoDB': 'mongodb', 'Redis': 'redis'}
_STORAGE_HINT = re.compile(r'(?i)^(S3_|AWS_(ACCESS|SECRET)|LIARA_(ENDPOINT|BUCKET|ACCESS|SECRET)|MINIO_|BUCKET)')


def validate(addons):
    """Normalise an addons dict (raises DiagError with Persian messages)."""
    from .dbaas import ENGINES
    if not addons:
        return {}
    if not isinstance(addons, dict) or set(addons) - {'database', 'storage'}:
        raise DiagError('افزونه‌ها فقط database و storage هستند')
    out, errs = {}, []
    on = lambda k: addons.get(k) not in (None, False)
    if on('database'):
        d = {**DB_DEFAULT, **(addons['database'] if isinstance(addons['database'], dict) else {})}
        if d['engine'] not in ENGINES:
            errs.append(f"موتور دیتابیس نامعتبر است ({', '.join(ENGINES)})")
        if d['bundle'] not in ('basic', 'standard', 'pro'):
            errs.append('باندل دیتابیس باید basic/standard/pro باشد')
        if not re.match(r'^[A-Z][A-Z0-9_]{0,15}$', d['prefix'] or ''):
            errs.append('پیشوند متغیرها باید حروف بزرگ لاتین باشد')
        out['database'] = {k: d[k] for k in DB_DEFAULT}
    if on('storage'):
        s = {**ST_DEFAULT, **(addons['storage'] if isinstance(addons['storage'], dict) else {})}
        if s['permission'] not in ('private', 'public'):
            errs.append('دسترسی باکت باید private یا public باشد')
        if s['style'] not in ('liara', 'aws'):
            errs.append('سبک متغیرهای ذخیره‌سازی باید liara یا aws باشد')
        out['storage'] = {k: s[k] for k in ST_DEFAULT}
    if errs:
        raise DiagError('\n'.join(errs))
    return out


def suggest(analysis):
    """analyze_project output -> suggested addons (only engines Liara offers; SQLite stays on a disk)."""
    a = analysis or {}
    out = {}
    engines = [_ENGINE_HINT[x] for x in a.get('databases') or [] if x in _ENGINE_HINT]
    envs = [e if isinstance(e, str) else e.get('name', '') for e in a.get('env_vars') or []]
    if not engines and any(re.match(r'(?i)^(DATABASE_URL|POSTGRES|PG(HOST|USER|DATABASE))', e) for e in envs):
        engines = ['postgres']
    if engines:
        out['database'] = {**DB_DEFAULT, 'engine': engines[0]}
    if any(_STORAGE_HINT.match(e) for e in envs):
        out['storage'] = dict(ST_DEFAULT)
    return out


def _slug(app, suffix):
    base = re.sub(r'[^a-z0-9-]', '-', app.lower()).strip('-')[:24]
    return f'{base}-{suffix}'


async def propose_addons(app, addons, ctx, why=None):
    """Create the approval cards (never executes). Returns the proposal summaries."""
    from ..approvals import propose
    addons = validate(addons)
    cards = []
    if addons.get('database'):
        d = addons['database']
        args = {'name': _slug(app, 'db'), 'engine': d['engine'], 'bundle': d['bundle'], 'prefix': d['prefix'], 'connect_app': app}
        if d.get('plan'):
            args['plan'] = d['plan']
        cards.append(await _safe(propose, 'create_database', args, ctx, why or f'پایگاه‌داده‌ی {d["engine"]} برای {app}'))
    if addons.get('storage'):
        s = addons['storage']
        cards.append(await _safe(propose, 'create_bucket', {'name': _slug(app, 'files'), 'plan': s['plan'], 'permission': s['permission'], 'style': s['style'], 'connect_app': app},
                                 ctx, why or f'فضای ذخیره‌سازی برای {app}'))
    return cards


async def _safe(propose, tool, args, ctx, why):
    try:
        return await propose(tool, args, ctx, why=why)
    except Exception as e:  # e.g. the one-week credit rule refuses the card up front
        return {'tool': tool, 'refused': True, 'error': str(e)[:300]}


from .common import spec  # noqa: E402


@spec('propose_addons', 'در حال ساخت کارت‌های دیتابیس/ذخیره‌سازی برای برنامه…', 'safe',
      'For an app (new or existing): create approval cards for a managed database and/or an object-storage bucket, each created AND connected to the app '
      'in its own card (env vars merged; app restarts once). Use when a project needs Postgres/MySQL/MongoDB/Redis or S3 storage. Nothing is created until approved.',
      {'app': {'type': 'string'}, 'database': {'type': 'string', 'enum': ['none', 'postgres', 'mysql', 'mariadb', 'mongodb', 'redis', 'mssql']},
       'db_plan': {'type': 'string'}, 'db_bundle': {'type': 'string', 'enum': ['basic', 'standard', 'pro']}, 'storage': {'type': 'boolean'},
       'storage_plan': {'type': 'string'}}, ['app'])
async def propose_addons_tool(a, ctx):
    addons = {}
    if a.get('database') and a['database'] != 'none':
        addons['database'] = {'engine': a['database'], 'plan': a.get('db_plan'), 'bundle': a.get('db_bundle') or 'basic'}
    if a.get('storage'):
        addons['storage'] = {'plan': a.get('storage_plan') or '20g-g2'}
    if not addons:
        raise DiagError('nothing to propose: give database=<engine> and/or storage=true')
    return {'app': a['app'], 'cards': await propose_addons(a['app'], addons, ctx or {})}
