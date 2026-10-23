"""Liara managed databases (DBaaS) through the shared core client (https://api.iran.liara.ir, same token, ?teamID=).
Spec: /opt/liara-spec/dbaas.yaml; extra console-only calls verified in the console bundle (2026-10-23):
  create body {hostname, type, version, planID, bundlePlanID, network, publicNetwork} -> {databaseID}  (g2 plans need a network)
  PATCH /v2/databases/{id}/reset-password (rate-limited, 429), POST /v1/databases/{id}/{type}-bak {url} (restore from a backup file URL),
  POST /v1/databases/{id}/resize-bundle-plan {bundlePlanID}, GET /v1/databases/{id}/check-availability.
Prices: /v1/me plans.databases[plan].price + plans.databaseBundlePlans[plan][bundle].price (toman per hour); versions:
/v1/me databaseVersions. There is no restart endpoint (restart = scale 0 then 1) and no rename.
SECRETS: GET /v1/databases/{id} returns root_password. It is stripped from every output (tools, UI, LLM, audit, logs); a reset
password or a backup download link is shown once in the dashboard (one-time token, 15 min). Connecting a database to an app
writes the credentials straight into the app's env vars (merged with the existing ones); cards and results list names only."""
import asyncio, re, time
from urllib.parse import quote
from ..config import settings
from ..liara import LiaraError, liara
from ..redact import redact
from .common import DiagError, audit, locked, spec
from .iaas import stash_secret
from .liara_data import week_precheck

ENGINES = ('postgres', 'mysql', 'mariadb', 'mongodb', 'redis', 'mssql', 'elasticsearch', 'rabbitmq', 'clickhouse')
NAME_RX = re.compile(r'^[a-z][a-z0-9-]{2,30}$')
_SECRET_KEY = re.compile(r'(?i)(pass(word)?|secret|root_?password|connection(_?string)?|dsn|uri$|_url$|^url$|credential|token)')
_DSN = re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)([^:/\s@]*):([^@/\s]+)@')
_cache = {'me': (0, None)}
STATUS_FA = {'RUNNING': 'در حال اجرا', 'STOPPED': 'خاموش', 'CREATING': 'در حال ساخت', 'DELETING': 'در حال حذف', 'FROZEN': 'مسدود (اعتبار)', 'RESTARTING': 'در حال راه‌اندازی'}


def strip_secrets(o):
    """Remove password/credential/connection-string fields anywhere in a Liara response, and mask user:pass@ in strings."""
    if isinstance(o, dict):
        return {k: strip_secrets(v) for k, v in o.items() if not (_SECRET_KEY.search(str(k)) and isinstance(v, (str, int, float)) and v not in (None, ''))}
    if isinstance(o, (list, tuple)):
        return [strip_secrets(x) for x in o]
    if isinstance(o, str):
        return redact(_DSN.sub(r'\1\2:***@', o))
    return o


# ---------------------------------------------------------------- catalogue
async def _me(force=False):
    t, v = _cache['me']
    if v is None or force or time.time() - t > 600:
        v = await liara.me()
        _cache['me'] = (time.time(), v)
    return v


async def plans(engine=None):
    me = await _me()
    pl = me.get('plans') or {}
    dbp, bundles = pl.get('databases') or {}, pl.get('databaseBundlePlans') or {}
    rows = []
    for pid, p in dbp.items():
        if not p.get('available') or (engine and engine not in (p.get('supports') or [])):
            continue
        b = {k: round(float(x.get('price') or 0), 2) for k, x in (bundles.get(pid) or {}).items() if x.get('available')}
        backups = {k: (x.get('backupRetention') or {}) for k, x in (bundles.get(pid) or {}).items() if x.get('available')}
        rows.append({'plan': pid, 'cpu': (p.get('CPU') or {}).get('amount'), 'ram_gb': (p.get('RAM') or {}).get('amount'), 'disk_gb': p.get('volume'),
                     'engines': p.get('supports'), 'plan_toman_per_hour': p.get('price'), 'bundles': b, 'backup_retention': backups, 'g2': pid.endswith('-g2')})
    return sorted(rows, key=lambda r: (not r['g2'], r['plan_toman_per_hour'] or 0))


def price(row, bundle='basic'):
    h = (row['plan_toman_per_hour'] or 0) + (row['bundles'].get(bundle) or 0)
    return {'toman_per_hour': round(h, 2), 'toman_per_month': round(h * 720)}


async def plan_row(plan):
    r = next((p for p in await plans() if p['plan'] == plan), None)
    if not r:
        raise DiagError(f'database plan {plan} is not available')
    return r


async def versions(engine=None):
    v = (await _me()).get('databaseVersions') or {}
    return {k: [x.get('value') for x in vs] for k, vs in v.items() if not engine or k == engine}


# ---------------------------------------------------------------- read
async def raw_list():
    return (await liara.get('/v1/databases')).get('databases') or []


def _id(d):
    return d.get('_id') or d.get('id') or d.get('DBId')


async def find(ref):
    ref = (ref or '').strip()
    for d in await raw_list():
        if ref in (_id(d), d.get('hostname'), d.get('DBId')):
            return d
    raise DiagError(f'database "{ref}" not found')


async def raw_detail(db_id):
    """Full detail INCLUDING credentials: internal use only (connect/reset); never returned."""
    d = await liara.get(f'/v1/databases/{db_id}')
    return d.get('database') if isinstance(d, dict) and isinstance(d.get('database'), dict) else (d or {})


def summarize(d, prices=None):
    p = (prices or {}).get(d.get('planID'))
    pr = price(p, d.get('bundlePlanID') or 'basic') if p else {}
    return strip_secrets({'id': _id(d), 'name': d.get('hostname'), 'engine': d.get('type'), 'version': d.get('version'), 'status': d.get('status'),
                          'status_fa': STATUS_FA.get(d.get('status')), 'scale': d.get('scale'), 'plan': d.get('planID'), 'bundle': d.get('bundlePlanID'),
                          'public_network': d.get('publicNetwork'), 'created_at': d.get('created_at') or d.get('createdAt'),
                          # total = plan + bundle (Liara's hourlyPrice alone is the plan part only)
                          'toman_per_hour': pr.get('toman_per_hour') or round((d.get('hourlyPrice') or 0) + (d.get('bundleHourlyPrice') or 0), 2) or None,
                          'toman_per_month': pr.get('toman_per_month')})


async def list_databases():
    dbs, ps = await asyncio.gather(raw_list(), plans())
    pm = {p['plan']: p for p in ps}
    rows = [summarize(d, pm) for d in dbs]
    return {'count': len(rows), 'databases': rows, 'total_toman_per_hour': round(sum(r['toman_per_hour'] or 0 for r in rows), 1)}


async def details(ref):
    d = await find(ref)
    full, ps = await asyncio.gather(raw_detail(_id(d)), plans())
    out = summarize({**d, **full}, {p['plan']: p for p in ps})
    host, port = endpoint(full)
    out.update({'host': host, 'port': port, 'internal_host': full.get('hostname'), 'internal_port': full.get('internalPort'), 'public_host': (full.get('node') or {}).get('host'),
                'public_port': full.get('port'), 'db_name': full.get('dbName'), 'username': full.get('username'), 'disk_gb': full.get('volumeSize'),
                'private_network': (full.get('metaData') or {}).get('privateNetwork'), 'password': 'hidden (use connect_database or reset_database_password)'})
    for k, path in (('metrics', f'/v1/databases/{_id(d)}/metrics/summary'),):
        try:
            out[k] = strip_secrets(await liara.get(path))
        except LiaraError as e:
            out[k + '_error'] = e.status
    try:
        out['backups'] = [{'name': b.get('name'), 'at': b.get('lastModified'), 'mb': round((b.get('size') or 0) / 2 ** 20, 2)}
                          for b in (await liara.get(f'/v1/databases/{_id(d)}/backups')).get('backups') or []][-10:]
    except LiaraError as e:
        out['backups_error'] = e.status
    return strip_secrets(out)


def endpoint(full):
    """Host/port an app should use: the private hostname inside the network, else the public node host/port."""
    if full.get('publicNetwork') and not (full.get('metaData') or {}).get('privateNetwork'):
        return (full.get('node') or {}).get('host') or full.get('hostname'), full.get('port') or full.get('internalPort')
    return full.get('hostname'), full.get('internalPort') or full.get('port')


# ---------------------------------------------------------------- connection env (values only ever go to Liara update-envs)
DEFAULT_DB = {'postgres': 'postgres', 'mongodb': 'admin', 'clickhouse': 'default'}
DEFAULT_PORTS = {'postgres': 5432, 'mysql': 3306, 'mariadb': 3306, 'mongodb': 27017, 'redis': 6379, 'mssql': 1433, 'elasticsearch': 9200, 'rabbitmq': 5672, 'clickhouse': 9000}
URL_VAR = {'postgres': 'DATABASE_URL', 'mysql': 'DATABASE_URL', 'mariadb': 'DATABASE_URL', 'mssql': 'DATABASE_URL', 'mongodb': 'MONGODB_URI',
           'redis': 'REDIS_URL', 'elasticsearch': 'ELASTICSEARCH_URL', 'rabbitmq': 'AMQP_URL', 'clickhouse': 'CLICKHOUSE_URL'}
SCHEME = {'postgres': 'postgresql', 'mysql': 'mysql', 'mariadb': 'mysql', 'mssql': 'mssql', 'mongodb': 'mongodb', 'redis': 'redis', 'elasticsearch': 'http',
          'rabbitmq': 'amqp', 'clickhouse': 'clickhouse'}


def env_names(engine, prefix='DB'):
    if engine == 'redis':
        return [URL_VAR['redis'], 'REDIS_HOST', 'REDIS_PORT', 'REDIS_PASSWORD']
    return [URL_VAR.get(engine, 'DATABASE_URL'), f'{prefix}_HOST', f'{prefix}_PORT', f'{prefix}_USER', f'{prefix}_PASSWORD', f'{prefix}_NAME']


def connection_env(engine, full, prefix='DB'):
    host, port = endpoint(full)
    port = port or DEFAULT_PORTS.get(engine)
    # Liara returns no dbName for a fresh database; the server's default database is used (postgres: 'postgres', mongodb: 'admin')
    user, pw = full.get('username') or ('default' if engine == 'redis' else 'root'), full.get('root_password') or full.get('password') or ''
    name = full.get('dbName') or DEFAULT_DB.get(engine, '')
    if not pw:
        raise DiagError('Liara returned no credentials for this database')
    u, p = quote(str(user), safe=''), quote(str(pw), safe='')
    if engine == 'redis':
        return {URL_VAR['redis']: f'redis://:{p}@{host}:{port}/0', 'REDIS_HOST': host, 'REDIS_PORT': str(port), 'REDIS_PASSWORD': pw}
    q = '?authSource=admin' if engine == 'mongodb' else ''
    return {URL_VAR.get(engine, 'DATABASE_URL'): f"{SCHEME.get(engine, engine)}://{u}:{p}@{host}:{port}/{name}{q}", f'{prefix}_HOST': host,
            f'{prefix}_PORT': str(port), f'{prefix}_USER': user, f'{prefix}_PASSWORD': pw, f'{prefix}_NAME': name}


async def _check_app(app):
    from .. import store, teams
    if locked(app):
        raise DiagError(f'{app} is locked (monitor-only); its env vars are not changed')
    if store.policy(app).get('env_lock'):
        raise DiagError(f'env vars of {app} are locked')
    try:  # cross-team guard: resources of this team are only wired into apps of the same team
        await teams.require_app_in_team(app, liara)
    except ValueError as e:
        raise DiagError(str(e))


async def connect(ref, app, prefix='DB', ctx=None):
    await _check_app(app)
    d = await find(ref)
    full = await raw_detail(_id(d))
    env = connection_env(full.get('type') or d.get('type'), full, prefix)
    cur = await liara.envs(app)
    overwritten = sorted(k for k in env if k in cur)
    await liara.update_envs(app, {**cur, **env})  # full replacement => merge (Liara semantics); restarts the app
    audit(ctx, 'dbaas.connect', app, {'database': d.get('hostname'), 'env_names': sorted(env), 'overwritten': overwritten})
    return {'app': app, 'database': d.get('hostname'), 'env_names': sorted(env), 'overwritten': overwritten, 'values': 'hidden',
            'note': 'the app restarts with the new variables (ENV_UPDATE release)'}


# ---------------------------------------------------------------- actions
async def wait_status(db_id, want=('RUNNING',), timeout=300, every=8):
    t0, last = time.time(), {}
    while time.time() - t0 < timeout:
        try:
            last = await raw_detail(db_id)
        except LiaraError as e:
            if e.status != 404:
                raise
            last = {'status': 'NOT_FOUND'}
        if last.get('status') in want:
            return True, strip_secrets(last)
        await asyncio.sleep(every)
    return False, strip_secrets(last)


async def create(name, engine, version=None, plan=None, bundle='basic', public_network=False, connect_app=None, ctx=None):
    if connect_app:
        await _check_app(connect_app)
    if not NAME_RX.match(name or ''):
        raise DiagError('database name: 3-31 chars, lowercase letters, digits and -, starting with a letter')
    if engine not in ENGINES:
        raise DiagError(f'engine must be one of {ENGINES}')
    row = await plan_row(plan or (await plans(engine))[0]['plan'])
    if engine not in (row['engines'] or []):
        raise DiagError(f"plan {row['plan']} does not support {engine}")
    if bundle not in row['bundles']:
        raise DiagError(f"bundle must be one of {list(row['bundles'])}")
    vs = (await versions(engine)).get(engine) or []
    version = version or (vs[0] if vs else None)
    if vs and version not in vs:
        raise DiagError(f'{engine} versions: {vs}')
    if any(d.get('hostname') == name for d in await raw_list()):
        raise DiagError(f'a database named {name} already exists')
    pre = await week_precheck(price(row, bundle)['toman_per_hour'])
    if pre['ok'] is False:
        raise DiagError(pre['message_fa'])
    body = {'hostname': name, 'type': engine, 'version': version, 'planID': row['plan'], 'bundlePlanID': bundle, 'publicNetwork': bool(public_network)}
    if row['g2']:
        body['network'] = await liara.network_id()
    r = await liara.post('/v1/databases', body)
    db_id = (r or {}).get('databaseID') or (r or {}).get('id')
    audit(ctx, 'dbaas.create', connect_app, {'database': name, 'engine': engine, 'version': version, 'plan': row['plan'], 'bundle': bundle})
    ok, st = await wait_status(db_id) if db_id else (False, {})
    out = {'database': name, 'id': db_id, 'engine': engine, 'version': version, 'plan': row['plan'], 'bundle': bundle, 'running': ok, 'status': st.get('status'),
           **price(row, bundle)}
    if connect_app and ok:
        out['connected'] = await connect(db_id, connect_app, ctx=ctx)
    elif connect_app:
        out['connect_pending'] = 'the database is not RUNNING yet; run connect_database when it is'
    return out


async def scale(ref, on):
    d = await find(ref)
    await liara.post(f"/v1/databases/{_id(d)}/actions/scale", {'scale': 1 if on else 0})
    ok, st = await wait_status(_id(d), ('RUNNING',) if on else ('STOPPED', 'SCALED_DOWN'), timeout=180)
    return {'database': d.get('hostname'), 'on': bool(on), 'status': st.get('status'), 'reached': ok}


async def restart(ref):
    off = await scale(ref, False)
    on = await scale(ref, True)
    return {'database': on['database'], 'restarted': on['reached'], 'status': on['status'], 'stop_status': off['status'], 'note': 'Liara has no restart endpoint: scale 0 then 1'}


async def resize(ref, plan, bundle=None):
    d = await find(ref)
    row = await plan_row(plan)
    if d.get('type') not in (row['engines'] or []):
        raise DiagError(f"plan {plan} does not support {d.get('type')}")
    await liara.post(f"/v1/databases/{_id(d)}/resize", {'planID': plan, 'disk': True})
    if bundle and bundle != d.get('bundlePlanID'):
        await liara.post(f"/v1/databases/{_id(d)}/resize-bundle-plan", {'bundlePlanID': bundle})
    return {'database': d.get('hostname'), 'from': d.get('planID'), 'to': plan, 'bundle': bundle or d.get('bundlePlanID')}


async def backups(ref):
    d = await find(ref)
    bs = (await liara.get(f"/v1/databases/{_id(d)}/backups")).get('backups') or []
    return {'database': d.get('hostname'), 'backups': [{'name': b.get('name'), 'at': b.get('lastModified'), 'mb': round((b.get('size') or 0) / 2 ** 20, 2)} for b in bs]}


async def create_backup(ref):
    d = await find(ref)
    await liara.post(f"/v1/databases/{_id(d)}/backups")
    return {'database': d.get('hostname'), 'backup': 'requested', 'note': 'it appears in database_backups when finished'}


async def backup_link(ref, name, ctx=None):
    d = await find(ref)
    r = await liara.post(f"/v1/databases/{_id(d)}/backups/{quote(name, safe='')}/download")
    link = (r or {}).get('link')
    if not link:
        raise DiagError('Liara returned no download link')
    tok = stash_secret(link, f"لینک دانلود پشتیبان {name} از {d.get('hostname')}")
    audit(ctx, 'dbaas.backup_link', None, {'database': d.get('hostname'), 'backup': name})
    return {'database': d.get('hostname'), 'backup': name, 'link': 'hidden', 'secret_once': f'/api/diag/secret/{tok}',
            'note': 'the download link is shown once in the dashboard card (it grants access to the data)'}


async def restore(ref, name, confirm_name, ctx=None):
    d = await find(ref)
    if confirm_name != d.get('hostname'):
        raise DiagError('confirmation name does not match the database name')
    r = await liara.post(f"/v1/databases/{_id(d)}/backups/{quote(name, safe='')}/download")
    link = (r or {}).get('link')
    if not link:
        raise DiagError('Liara returned no download link for this backup')
    await liara.post(f"/v1/databases/{_id(d)}/{d.get('type')}-bak", {'url': link})
    audit(ctx, 'dbaas.restore', None, {'database': d.get('hostname'), 'backup': name})
    return {'database': d.get('hostname'), 'restoring_from': name, 'status': 'started'}


async def reset_password(ref, ctx=None):
    d = await find(ref)
    try:
        await liara.req('PATCH', f"/v2/databases/{_id(d)}/reset-password")
    except LiaraError as e:
        if e.status == 429:
            raise DiagError('Liara limits password resets (429); try again later')
        raise
    full = await raw_detail(_id(d))
    pw = full.get('root_password') or full.get('password')
    out = {'database': d.get('hostname'), 'password': 'hidden', 'note': 'apps connected to this database keep the OLD password: run connect_database again for each'}
    if pw:
        out['secret_once'] = f"/api/diag/secret/{stash_secret(pw, 'رمز جدید دیتابیس ' + str(d.get('hostname')))}"
    audit(ctx, 'dbaas.reset_password', None, {'database': d.get('hostname')})
    return out


async def public_network(ref, enable):
    d = await find(ref)
    await liara.req('PATCH', f"/v1/databases/{_id(d)}/public-network/{'enable' if enable else 'disable'}")
    return {'database': d.get('hostname'), 'public_network': bool(enable)}


async def delete(ref, confirm_name, ctx=None):
    d = await find(ref)
    if confirm_name != d.get('hostname'):
        raise DiagError('confirmation name does not match the database name')
    await liara.delete(f"/v1/databases/{_id(d)}")
    audit(ctx, 'dbaas.delete', None, {'database': d.get('hostname'), 'id': _id(d)})
    return {'deleted': d.get('hostname'), 'id': _id(d)}


# ---------------------------------------------------------------- cards
def _cost(p):
    return f"{p['toman_per_hour']:,.0f} تومان در ساعت (~{p['toman_per_month']:,.0f} تومان در ماه)"


async def _create_impact(a):
    eng = a.get('engine')
    if eng not in ENGINES:
        raise DiagError(f'engine must be one of {ENGINES}')
    row = await plan_row(a.get('plan') or (await plans(eng))[0]['plan'])
    b = a.get('bundle') or 'basic'
    if b not in row['bundles']:
        raise DiagError(f"bundle must be one of {list(row['bundles'])}")
    pr = price(row, b)
    pre = await week_precheck(pr['toman_per_hour'])
    if pre['ok'] is False:
        raise DiagError(pre['message_fa'])
    imp = {'downtime': 'ندارد (سرویس جدید)', 'cost': '+' + _cost(pr), 'risk': 'کم؛ هزینه تا حذف دیتابیس ادامه دارد', 'engine': eng, 'version': a.get('version') or 'آخرین نسخه',
           'resources': f"{row['plan']} · {row['cpu']} هسته · {row['ram_gb']} گیگ RAM · {row['disk_gb']} گیگ دیسک · باندل {b}", 'toman_per_hour': pr['toman_per_hour'],
           'backups': 'ندارد (باندل basic)' if not any((row['backup_retention'].get(b) or {}).values()) else row['backup_retention'].get(b),
           'balance_check': f"{pre.get('team') or ''}: موجودی {pre.get('balance') or 0:,.0f} ≥ هزینه‌ی یک هفته {pre.get('need') or 0:,.0f}" if pre['ok'] else pre.get('message_fa')}
    if a.get('connect_app'):
        await _check_app(a['connect_app'])
        imp['connect_app'] = a['connect_app']
        imp['env_names'] = env_names(eng, a.get('prefix') or 'DB')
        imp['downtime'] = f"برنامه‌ی {a['connect_app']} پس از اتصال یک بار ری‌استارت می‌شود"
    return imp, 'حذف دیتابیس با کارت «حذف» (تایپ نام)؛ اتصال برنامه با حذف همین متغیرها برمی‌گردد.'


async def _connect_impact(a):
    await _check_app(a['app'])
    d = await find(a['database'])
    names = env_names(d.get('type'), a.get('prefix') or 'DB')
    try:
        cur = set((await liara.envs(a['app'])).keys())
    except LiaraError:
        cur = set()
    return ({'downtime': 'برنامه یک بار ری‌استارت می‌شود (ENV_UPDATE)', 'cost': 'بدون هزینه', 'risk': 'کم؛ مقادیر محرمانه فقط به لیارا ارسال می‌شود',
             'env_names': names, 'overwrites': sorted(set(names) & cur), 'values': 'نمایش داده نمی‌شود'},
            'بازگشت: حذف همین متغیرها با unset_env یا بازگردانی نسخه‌ی قبلی (ENV_UPDATE قابل rollback است).')


async def _simple_impact(a, text, risk='کم', downtime='ندارد'):
    d = await find(a['database'])
    return {'downtime': downtime, 'cost': 'بدون تغییر', 'risk': risk, 'database': d.get('hostname'), 'engine': d.get('type')}, text


async def _backup_impact(a):
    """Refuse up front when the bundle has no manual backups (verified live: basic -> 'Manual backup not available.')."""
    d = await find(a['database'])
    b = d.get('bundlePlanID') or 'basic'
    try:
        manual = ((await plan_row(d.get('planID'))).get('backup_retention') or {}).get(b, {}).get('manual')
    except DiagError:
        manual = None
    if manual == 0:
        raise DiagError(f"باندل {b} پشتیبان‌گیری دستی ندارد؛ برای پشتیبان‌گیری باندل را به standard یا pro ارتقا دهید (resize_database با bundle).")
    imp, rb = await _simple_impact(a, 'نیازی به بازگشت نیست.')
    if manual:
        imp['limit'] = f'باندل {b}: حداکثر {manual} پشتیبان دستی نگه داشته می‌شود'
    return imp, rb


async def _resize_impact(a):
    d = await find(a['database'])
    old, new = await plan_row(d.get('planID')), await plan_row(a['plan'])
    b = a.get('bundle') or d.get('bundlePlanID') or 'basic'
    po, pn = price(old, d.get('bundlePlanID') or 'basic'), price(new, b)
    pre = await week_precheck(max(0, pn['toman_per_hour'] - po['toman_per_hour']))
    return ({'downtime': 'چند دقیقه قطعی هنگام تغییر پلن', 'cost': f"{po['toman_per_hour']:,.0f} → {pn['toman_per_hour']:,.0f} تومان در ساعت", 'risk': 'متوسط',
             'from': d.get('planID'), 'to': a['plan'], 'bundle': b, **({'balance_warning': pre['message_fa']} if pre['ok'] is False else {})},
            f"بازگشت با تغییر پلن به {d.get('planID')} (کاهش دیسک ممکن نیست).")


async def _typed_impact(a, what):
    d = await find(a['database'])
    try:  # the list row has no price (live: card showed "- تومان"); use the catalogue: plan + bundle
        per_hour = price(await plan_row(d.get('planID')), d.get('bundlePlanID') or 'basic')['toman_per_hour']
    except Exception:
        per_hour = (d.get('hourlyPrice') or 0) + (d.get('bundleHourlyPrice') or 0)
    saved = f"صرفه‌جویی {per_hour:,.0f} تومان در ساعت" if per_hour else 'هزینه‌ی دیتابیس قطع می‌شود'
    return ({'downtime': what, 'cost': 'بدون تغییر' if 'بازیابی' in what else saved, 'risk': 'بسیار زیاد (برگشت‌ناپذیر)',
             'database': d.get('hostname'), 'confirm': f"برای تأیید باید نام «{d.get('hostname')}» را تایپ کنید"}, 'برگشت‌ناپذیر است؛ پیش از آن از دیتابیس پشتیبان بگیرید.')


# ---------------------------------------------------------------- tools
DB = {'database': {'type': 'string', 'description': 'database name (hostname) or id'}}


@spec('list_databases', 'در حال فهرست کردن پایگاه‌داده‌ها…', 'read',
      'Managed databases of the team: engine, version, status, plan, bundle, public network, toman per hour/month. Never returns passwords.')
async def list_databases_tool(a, ctx):
    return await list_databases()


@spec('database_details', 'در حال خواندن جزئیات پایگاه‌داده…', 'read',
      'One managed database: status, engine/version, host/port to use (private network or public), db name, user, disk, metrics, recent backups. Password is never returned.',
      DB, ['database'])
async def database_details_tool(a, ctx):
    return await details(a['database'])


@spec('database_plans', 'در حال خواندن پلن‌ها و قیمت پایگاه‌داده…', 'read',
      'Database plans with live prices (plan + bundle, toman per hour/month), resources, supported engines, backup retention per bundle, and available versions.',
      {'engine': {'type': 'string', 'enum': list(ENGINES)}})
async def database_plans_tool(a, ctx):
    rows = await plans(a.get('engine'))
    return {'plans': [{**r, 'prices': {b: price(r, b) for b in r['bundles']}} for r in rows], 'versions': await versions(a.get('engine')),
            'note': 'cost = plan + bundle; the basic bundle has no backups'}


@spec('database_backups', 'در حال خواندن پشتیبان‌های پایگاه‌داده…', 'read', 'List backups of a managed database (name, time, size). Use database_backup_link / restore_database_backup with a name.', DB, ['database'])
async def database_backups_tool(a, ctx):
    return await backups(a['database'])


CREATE = {'name': {'type': 'string', 'pattern': NAME_RX.pattern, 'description': 'database hostname (lowercase)'}, 'engine': {'type': 'string', 'enum': list(ENGINES)},
          'version': {'type': 'string'}, 'plan': {'type': 'string', 'description': 'see database_plans; default = cheapest g2 plan for the engine'},
          'bundle': {'type': 'string', 'enum': ['basic', 'standard', 'pro']}, 'public_network': {'type': 'boolean'},
          'connect_app': {'type': 'string', 'description': 'optional: Liara app to connect right after creation (env vars injected, merged)'},
          'prefix': {'type': 'string', 'pattern': '^[A-Z][A-Z0-9_]{0,15}$'}, 'reason': {'type': 'string'}}


@spec('create_database', 'ساخت کارت تأیید برای پایگاه‌داده‌ی جدید…', 'sensitive',
      'Create a Liara managed database (postgres, mysql, mariadb, mongodb, redis, mssql, …) on the private network; optionally connect it to an app in the same card '
      '(DATABASE_URL + DB_HOST/PORT/USER/PASSWORD/NAME injected, merged with existing env; the app restarts). Checks that the credit covers one week first.',
      CREATE, ['name', 'engine'], title=lambda a: f"ساخت پایگاه‌داده‌ی {a.get('engine')} «{a.get('name')}»" + (f" و اتصال به {a['connect_app']}" if a.get('connect_app') else ''),
      impact=_create_impact, timeout=420)
async def create_database_tool(a, ctx):
    return await create(a['name'], a['engine'], a.get('version'), a.get('plan'), a.get('bundle') or 'basic', a.get('public_network', False), a.get('connect_app'), ctx)


@spec('connect_database', 'ساخت کارت تأیید برای اتصال پایگاه‌داده به برنامه…', 'sensitive',
      'Connect a managed database to an app: write the connection env vars (URL + host/port/user/password/name) into the app, merged with its existing env. '
      'The card lists variable NAMES only; values go straight to Liara. The app restarts.',
      {**DB, 'app': {'type': 'string'}, 'prefix': {'type': 'string', 'pattern': '^[A-Z][A-Z0-9_]{0,15}$'}, 'reason': {'type': 'string'}}, ['database', 'app'],
      title=lambda a: f"اتصال پایگاه‌داده‌ی {a.get('database')} به {a.get('app')}", impact=_connect_impact)
async def connect_database_tool(a, ctx):
    return await connect(a['database'], a['app'], a.get('prefix') or 'DB', ctx)


@spec('database_power', 'ساخت کارت تأیید برای روشن/خاموش/ری‌استارت پایگاه‌داده…', 'sensitive',
      'Start, stop or restart a managed database (restart = stop then start; Liara has no restart endpoint).',
      {**DB, 'action': {'type': 'string', 'enum': ['start', 'stop', 'restart']}, 'reason': {'type': 'string'}}, ['database', 'action'],
      title=lambda a: f"{ {'start': 'روشن کردن', 'stop': 'خاموش کردن', 'restart': 'ری‌استارت'}.get(a.get('action'), '')} پایگاه‌داده‌ی {a.get('database')}",
      impact=lambda a: _simple_impact(a, 'با عمل مخالف برمی‌گردد.', 'متوسط', 'برنامه‌های وابسته تا روشن شدن دوباره خطا می‌گیرند' if a.get('action') != 'start' else 'ندارد'),
      timeout=420)
async def database_power_tool(a, ctx):
    return await (restart(a['database']) if a['action'] == 'restart' else scale(a['database'], a['action'] == 'start'))


@spec('resize_database', 'ساخت کارت تأیید برای تغییر پلن پایگاه‌داده…', 'sensitive', 'Change the plan (and optionally the bundle) of a managed database; price delta on the card.',
      {**DB, 'plan': {'type': 'string'}, 'bundle': {'type': 'string', 'enum': ['basic', 'standard', 'pro']}, 'reason': {'type': 'string'}}, ['database', 'plan'],
      title=lambda a: f"تغییر پلن پایگاه‌داده‌ی {a.get('database')} به {a.get('plan')}", impact=_resize_impact)
async def resize_database_tool(a, ctx):
    return await resize(a['database'], a['plan'], a.get('bundle'))


@spec('backup_database', 'ساخت کارت تأیید برای پشتیبان‌گیری…', 'sensitive', 'Take a manual backup now (limited per hour by the bundle; the basic bundle has none).',
      DB, ['database'], title=lambda a: f"پشتیبان‌گیری از {a.get('database')}", impact=lambda a: _backup_impact(a))
async def backup_database_tool(a, ctx):
    return await create_backup(a['database'])


@spec('database_backup_link', 'ساخت کارت تأیید برای لینک دانلود پشتیبان…', 'sensitive',
      'Create a download link for a backup. The link gives access to the data, so it is shown once in the dashboard card only.',
      {**DB, 'backup': {'type': 'string'}}, ['database', 'backup'], title=lambda a: f"لینک دانلود پشتیبان {a.get('backup')}",
      impact=lambda a: _simple_impact(a, 'لینک موقت است و خودبه‌خود منقضی می‌شود.', 'متوسط: هر کس لینک را داشته باشد داده را دارد'))
async def database_backup_link_tool(a, ctx):
    return await backup_link(a['database'], a['backup'], ctx)


@spec('restore_database_backup', 'ساخت کارت تأیید برای بازیابی پشتیبان…', 'sensitive',
      'Restore a managed database from one of its backups (overwrites current data). The owner types the database name on the card.',
      {**DB, 'backup': {'type': 'string'}, 'reason': {'type': 'string'}}, ['database', 'backup'],
      title=lambda a: f"بازیابی {a.get('database')} از پشتیبان {a.get('backup')}", impact=lambda a: _typed_impact(a, 'بازیابی داده‌های فعلی را جایگزین می‌کند'))
async def restore_database_backup_tool(a, ctx):
    return await restore(a['database'], a['backup'], a.get('confirm_name'), ctx)


@spec('reset_database_password', 'ساخت کارت تأیید برای بازنشانی رمز پایگاه‌داده…', 'sensitive',
      'Reset the root password (rate-limited by Liara). The new password is shown once in the dashboard; connected apps must be reconnected with connect_database.',
      DB, ['database'], title=lambda a: f"بازنشانی رمز پایگاه‌داده‌ی {a.get('database')}",
      impact=lambda a: _simple_impact(a, 'رمز قبلی قابل بازگشت نیست.', 'زیاد: برنامه‌های متصل تا اتصال دوباره خطا می‌گیرند', 'برنامه‌های متصل تا اتصال دوباره قطع می‌شوند'))
async def reset_database_password_tool(a, ctx):
    return await reset_password(a['database'], ctx)


@spec('database_public_network', 'ساخت کارت تأیید برای دسترسی عمومی پایگاه‌داده…', 'sensitive',
      'Enable or disable public (internet) access to a managed database. Keep it disabled unless needed.',
      {**DB, 'enable': {'type': 'boolean'}}, ['database', 'enable'], title=lambda a: f"{'فعال' if a.get('enable') else 'غیرفعال'} کردن دسترسی عمومی {a.get('database')}",
      impact=lambda a: _simple_impact(a, 'با عمل مخالف برمی‌گردد.', 'زیاد: دیتابیس از اینترنت در دسترس می‌شود' if a.get('enable') else 'کم'))
async def database_public_network_tool(a, ctx):
    return await public_network(a['database'], a['enable'])


@spec('delete_database', 'ساخت کارت تأیید برای حذف پایگاه‌داده…', 'sensitive',
      'DELETE a managed database permanently (irreversible). The owner types the database name on the card.',
      {**DB, 'reason': {'type': 'string'}}, ['database'], title=lambda a: f"حذف دائمی پایگاه‌داده‌ی {a.get('database')}",
      impact=lambda a: _typed_impact(a, 'دیتابیس و همه‌ی داده‌هایش حذف می‌شود'))
async def delete_database_tool(a, ctx):
    return await delete(a['database'], a.get('confirm_name'), ctx)


LIMITATIONS = [
    ('پایگاه‌داده: ری‌استارت', 'API لیارا ری‌استارت ندارد؛ نگهبان دیتابیس را خاموش و دوباره روشن می‌کند (scale ۰ و ۱).'),
    ('پایگاه‌داده: پشتیبان', 'باندل basic پشتیبان ندارد؛ برای پشتیبان‌گیری باندل standard یا pro لازم است. بازیابی از طریق API فقط با لینک فایل پشتیبان ممکن است.'),
    ('پایگاه‌داده: رمز', 'بازنشانی رمز محدودیت نرخ دارد (۴۲۹) و برنامه‌های متصل باید دوباره متصل شوند؛ تغییر نام کاربری ممکن نیست.'),
    ('ساخت منابع جدید', 'لیارا ساخت دیتابیس/باکت/برنامه را وقتی موجودی کمتر از هزینه‌ی یک هفته‌ی همه‌ی سرویس‌هاست با ۴۲۹ رد می‌کند؛ نگهبان پیش از کارت تأیید بررسی می‌کند.'),
]
