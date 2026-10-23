"""Liara object storage (S3-compatible buckets) through the shared core client: https://storage-service.liara.ir/api/v1, same
token, ?teamID=. Spec: /opt/liara-spec/object-storage.yaml + console bundle (2026-10-23): create {name, plan, permission:
private|public} (default plan 20g-g2); PATCH /buckets/{b}/type/{private|public}; PATCH /buckets/{b}/upgrade/{plan};
keys: POST /keys {buckets:[names], description, permissions:[read,put,list,delete]} -> {data:{accessKey, secretKey}},
PATCH /keys/{accessKey}/revoke -> new secret, DELETE /keys/{accessKey}; objects: GET /buckets/{b}/objects/?prefix…&number=&page=;
temporary link: GET /buckets/{b}/download/{key}?expiry=<s> -> {data:{url}}. Prices: /v1/me plans.objectStorage[plan].price
(toman per hour). S3 endpoint: the bucket's cluster.endPoint (live: https://storage.iran.liara.site; env LIARA_S3_ENDPOINT overrides the fallback).
Secret keys are never returned: a new secret is shown once in the dashboard; connect_bucket creates a bucket-scoped key at
execution time and writes it straight into the app env (merged); cards/results list variable names only."""
import asyncio, os, re, time
from urllib.parse import quote
from ..liara import LiaraError, liara
from .common import DiagError, audit, spec
from .dbaas import _check_app
from .iaas import stash_secret
from .liara_data import week_precheck

BASE = os.environ.get('LIARA_STORAGE_BASE', 'https://storage-service.liara.ir/api/v1').rstrip('/')
S3_ENDPOINT = os.environ.get('LIARA_S3_ENDPOINT', 'https://storage.iran.liara.site')  # verified live: bucket.cluster.endPoint (.space does not answer)
NAME_RX = re.compile(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$')
PERMS = ('private', 'public')
KEY_PERMS = ('read', 'put', 'list', 'delete')
_cache = {'me': (0, None)}


def get(path, **kw):
    return liara.get(path, base=BASE, timeout=30, **kw)


def call(method, path, body=None, **kw):
    return liara.req(method, path, json_body=body, base=BASE, timeout=45, **kw)


def mask_key(k):
    k = str(k or '')
    return (k[:4] + '…' + k[-2:]) if len(k) > 8 else '…'


def strip_keys(o):
    if isinstance(o, dict):
        return {k: (mask_key(v) if k == 'accessKey' else strip_keys(v)) for k, v in o.items() if not re.search(r'(?i)secret', str(k))}
    if isinstance(o, list):
        return [strip_keys(x) for x in o]
    return o


# ---------------------------------------------------------------- catalogue
async def plans():
    t, v = _cache['me']
    if v is None or time.time() - t > 600:
        v = await liara.me()
        _cache['me'] = (time.time(), v)
    rows = [{'plan': k, 'space_gb': p.get('space'), 'toman_per_hour': round(float(p.get('price') or 0), 2), 'toman_per_month': round(float(p.get('price') or 0) * 720),
             'g2': k.endswith('-g2')} for k, p in ((v.get('plans') or {}).get('objectStorage') or {}).items() if p.get('available')]
    return sorted(rows, key=lambda r: (not r['g2'], r['toman_per_hour']))


async def plan_row(plan):
    r = next((p for p in await plans() if p['plan'] == plan), None)
    if not r:
        raise DiagError(f'storage plan {plan} is not available')
    return r


# ---------------------------------------------------------------- read
async def raw_buckets():
    return (await get('/buckets')).get('buckets') or []


async def find(name):
    for b in await raw_buckets():
        if name in (b.get('name'), b.get('id')):
            return b
    raise DiagError(f'bucket "{name}" not found')


async def usage(name):
    try:
        m = ((await get(f'/buckets/{name}/metrics/summary')).get('data') or {}).get('metrics') or {}
        val = lambda x: float(x[-1]) if isinstance(x, list) and x else (float(x) if isinstance(x, (int, float, str)) and str(x).replace('.', '', 1).isdigit() else None)
        b, n = val(m.get('total_bytes')), val(m.get('totol_objects') or m.get('total_objects'))
        return {'used_mb': round(b / 2 ** 20, 2) if b is not None else None, 'objects': int(n) if n is not None else None}
    except (LiaraError, TypeError, ValueError):
        return {'used_mb': None, 'objects': None}


async def bucket_endpoint(name):
    """S3 endpoint of a bucket from GET /buckets/{name} -> bucket.cluster.endPoint (verified live), else the default."""
    try:
        ep = (((await get(f'/buckets/{name}')).get('bucket') or {}).get('cluster') or {}).get('endPoint')
    except LiaraError:
        ep = None
    return ('https://' + ep if ep and not ep.startswith('http') else ep) or S3_ENDPOINT


async def list_buckets():
    bs, ps = await asyncio.gather(raw_buckets(), plans())
    pm = {p['plan']: p for p in ps}
    us = await asyncio.gather(*[usage(b['name']) for b in bs[:20]])
    rows = [{'name': b.get('name'), 'plan': b.get('plan'), 'space_gb': (pm.get(b.get('plan')) or {}).get('space_gb'), 'permission': b.get('permission'), 'status': b.get('status'),
             'created_at': b.get('createdAt'), **u, 'toman_per_hour': (pm.get(b.get('plan')) or {}).get('toman_per_hour'), 'endpoint': S3_ENDPOINT} for b, u in zip(bs, us)]
    return {'count': len(rows), 'buckets': rows, 'total_toman_per_hour': round(sum(r['toman_per_hour'] or 0 for r in rows), 1), 'endpoint': S3_ENDPOINT}


async def list_objects(bucket, prefix='', limit=50, page=1):
    await find(bucket)
    path = f"/buckets/{bucket}/objects/{quote(prefix.lstrip('/'), safe='/')}" if prefix else f'/buckets/{bucket}/objects/'
    params = {'number': max(1, min(int(limit), 50))}
    if int(page) > 1:  # verified live: sending page=1 makes storage-service answer 500
        params['page'] = int(page)
    d = ((await get(path, params=params)).get('data') or {}).get('objects') or {}
    files = [{'key': o.get('Key'), 'kb': round((o.get('Size') or 0) / 1024, 1), 'modified': o.get('LastModified')} for o in d.get('Contents') or []]
    return {'bucket': bucket, 'prefix': prefix, 'folders': [p.get('Prefix') for p in d.get('CommonPrefixes') or []], 'objects': files[:limit],
            'truncated': bool(d.get('IsTruncated')), 'count': d.get('KeyCount', len(files))}


async def temp_link(bucket, key, expiry=3600):
    await find(bucket)
    expiry = max(60, min(int(expiry), 7 * 86400))
    d = await get(f"/buckets/{bucket}/download/{quote(key, safe='')}", params={'expiry': expiry})
    url = ((d or {}).get('data') or {}).get('url')
    if not url:
        raise DiagError('Liara returned no link')
    return url, expiry


async def keys():
    ks = (await get('/keys')).get('keys') or []
    return {'count': len(ks), 'keys': [{'access_key': mask_key(k.get('accessKey')), 'id': k.get('id'), 'description': k.get('description'), 'status': k.get('status'),
                                        'buckets': [b.get('name') for b in k.get('buckets') or []], 'permissions': k.get('permissions'), 'created_at': k.get('createdAt')} for k in ks]}


async def _key_by_ref(ref):
    for k in (await get('/keys')).get('keys') or []:
        if ref in (k.get('id'), k.get('accessKey'), k.get('description')) or (len(ref) >= 5 and mask_key(k.get('accessKey')) == ref):
            return k
    raise DiagError(f'access key "{ref}" not found')


# ---------------------------------------------------------------- actions
async def create_bucket(name, plan='20g-g2', permission='private', connect_app=None, ctx=None):
    if connect_app:
        _check_app(connect_app)
    if not NAME_RX.match(name or ''):
        raise DiagError('bucket name: 3-63 chars, lowercase letters, digits and -')
    if permission not in PERMS:
        raise DiagError('permission must be private or public')
    row = await plan_row(plan)
    if any(b.get('name') == name for b in await raw_buckets()):
        raise DiagError(f'a bucket named {name} already exists')
    try:
        await get(f'/buckets/{name}/check-availability')
    except LiaraError as e:
        if e.status == 409:
            raise DiagError(f'bucket name {name} is taken (bucket names are global)')
    pre = await week_precheck(row['toman_per_hour'])
    if pre['ok'] is False:
        raise DiagError(pre['message_fa'])
    await call('POST', '/buckets', {'name': name, 'plan': plan, 'permission': permission})
    audit(ctx, 'storage.create', connect_app, {'bucket': name, 'plan': plan, 'permission': permission})
    out = {'bucket': name, 'plan': plan, 'permission': permission, 'endpoint': S3_ENDPOINT, 'toman_per_hour': row['toman_per_hour'], 'toman_per_month': row['toman_per_month']}
    if connect_app:
        out['connected'] = await connect(name, connect_app, ctx=ctx)
    return out


async def set_permission(name, permission):
    if permission not in PERMS:
        raise DiagError('permission must be private or public')
    await find(name)
    await call('PATCH', f'/buckets/{name}/type/{permission}')
    return {'bucket': name, 'permission': permission}


async def resize_bucket(name, plan):
    b = await find(name)
    old, new = await plan_row(b.get('plan')) if b.get('plan') else None, await plan_row(plan)
    if old and new['space_gb'] < old['space_gb']:
        raise DiagError('a bucket plan can only be upgraded')
    await call('PATCH', f'/buckets/{name}/upgrade/{plan}')
    return {'bucket': name, 'from': b.get('plan'), 'to': plan}


async def delete_bucket(name, confirm_name, ctx=None):
    await find(name)
    if confirm_name != name:
        raise DiagError('confirmation name does not match the bucket name')
    objs = await list_objects(name, limit=1)
    if objs['objects'] or objs['folders']:
        raise DiagError(f'bucket {name} is not empty; empty it first (Negahban does not bulk-delete objects)')
    await call('DELETE', f'/buckets/{name}')
    audit(ctx, 'storage.delete', None, {'bucket': name})
    return {'deleted': name}


async def create_key(buckets, description, permissions=KEY_PERMS, ctx=None, reveal=True):
    names = [b['name'] for b in await raw_buckets()]
    for b in buckets:
        if b not in names:
            raise DiagError(f'bucket {b} not found')
    bad = [p for p in permissions if p not in KEY_PERMS]
    if bad or not permissions:
        raise DiagError(f'permissions must be from {KEY_PERMS}')
    r = await call('POST', '/keys', {'buckets': list(buckets), 'description': description[:120], 'permissions': list(permissions)})
    d = (r or {}).get('data') or {}
    ak, sk = d.get('accessKey'), d.get('secretKey')
    if not (ak and sk):
        raise DiagError('Liara returned no key pair')
    audit(ctx, 'storage.key_create', None, {'buckets': list(buckets), 'access_key': mask_key(ak), 'permissions': list(permissions)})
    out = {'access_key': mask_key(ak), 'buckets': list(buckets), 'permissions': list(permissions), 'secret_key': 'hidden'}
    if reveal:
        tok = stash_secret('Access key: ' + ak + '\nSecret key: ' + sk, 'کلید دسترسی فضای ذخیره‌سازی')
        out['secret_once'] = f'/api/diag/secret/{tok}'
    return out, ak, sk


async def revoke_key(ref, ctx=None):
    k = await _key_by_ref(ref)
    r = await call('PATCH', f"/keys/{k['accessKey']}/revoke")
    d = (r or {}).get('data') or r or {}
    out = {'access_key': mask_key(k['accessKey']), 'revoked': True, 'secret_key': 'hidden', 'note': 'apps using the old secret stop working until reconnected'}
    if d.get('secretKey'):
        tok = stash_secret('Access key: ' + str(d.get('accessKey') or k['accessKey']) + '\nSecret key: ' + str(d['secretKey']), 'کلید جدید فضای ذخیره‌سازی')
        out['secret_once'] = f'/api/diag/secret/{tok}'
    audit(ctx, 'storage.key_revoke', None, {'access_key': mask_key(k['accessKey'])})
    return out


async def delete_key(ref, ctx=None):
    k = await _key_by_ref(ref)
    await call('DELETE', f"/keys/{k['accessKey']}")
    audit(ctx, 'storage.key_delete', None, {'access_key': mask_key(k['accessKey'])})
    return {'access_key': mask_key(k['accessKey']), 'deleted': True}


ENV_STYLES = {'liara': ('LIARA_ENDPOINT', 'LIARA_BUCKET_NAME', 'LIARA_ACCESS_KEY', 'LIARA_SECRET_KEY'),
              'aws': ('AWS_ENDPOINT_URL_S3', 'S3_BUCKET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY')}


def env_names(style='liara'):
    return list(ENV_STYLES[style]) + (['AWS_DEFAULT_REGION'] if style == 'aws' else [])


async def connect(bucket, app, style='liara', ctx=None):
    _check_app(app)
    if style not in ENV_STYLES:
        raise DiagError('style must be liara or aws')
    await find(bucket)
    ep = await bucket_endpoint(bucket)
    _, ak, sk = await create_key([bucket], f'negahban: {app} -> {bucket}', ctx=ctx, reveal=False)
    e, b, a, s = ENV_STYLES[style]
    env = {e: ep, b: bucket, a: ak, s: sk, **({'AWS_DEFAULT_REGION': 'us-east-1'} if style == 'aws' else {})}
    cur = await liara.envs(app)
    await liara.update_envs(app, {**cur, **env})
    audit(ctx, 'storage.connect', app, {'bucket': bucket, 'env_names': sorted(env), 'access_key': mask_key(ak)})
    return {'app': app, 'bucket': bucket, 'env_names': sorted(env), 'overwritten': sorted(k for k in env if k in cur), 'access_key': mask_key(ak), 'values': 'hidden',
            'note': 'a dedicated key (read/put/list/delete on this bucket only) was created; the app restarts'}


# ---------------------------------------------------------------- cards
async def _create_impact(a):
    row = await plan_row(a.get('plan') or '20g-g2')
    if not NAME_RX.match(a.get('name') or ''):
        raise DiagError('bucket name: 3-63 chars, lowercase letters, digits and -')
    pre = await week_precheck(row['toman_per_hour'])
    if pre['ok'] is False:
        raise DiagError(pre['message_fa'])
    imp = {'downtime': 'ندارد (سرویس جدید)', 'cost': f"+{row['toman_per_hour']:,.0f} تومان در ساعت (~{row['toman_per_month']:,.0f} تومان در ماه)",
           'risk': 'کم' if (a.get('permission') or 'private') == 'private' else 'متوسط: فایل‌ها بدون احراز هویت قابل خواندن‌اند', 'space': f"{row['space_gb']} گیگ",
           'permission': a.get('permission') or 'private', 'toman_per_hour': row['toman_per_hour'],
           'balance_check': f"موجودی {pre.get('balance') or 0:,.0f} ≥ هزینه‌ی یک هفته {pre.get('need') or 0:,.0f}" if pre['ok'] else pre.get('message_fa')}
    if a.get('connect_app'):
        _check_app(a['connect_app'])
        imp.update(connect_app=a['connect_app'], env_names=env_names(a.get('style') or 'liara'), downtime=f"برنامه‌ی {a['connect_app']} پس از اتصال یک بار ری‌استارت می‌شود")
    return imp, 'حذف باکت با کارت «حذف» (تایپ نام، باکت باید خالی باشد).'


async def _connect_impact(a):
    _check_app(a['app'])
    await find(a['bucket'])
    names = env_names(a.get('style') or 'liara')
    try:
        cur = set((await liara.envs(a['app'])).keys())
    except LiaraError:
        cur = set()
    return ({'downtime': 'برنامه یک بار ری‌استارت می‌شود (ENV_UPDATE)', 'cost': 'بدون هزینه', 'risk': 'کم؛ یک کلید اختصاصی فقط برای همین باکت ساخته می‌شود',
             'env_names': names, 'overwrites': sorted(set(names) & cur), 'values': 'نمایش داده نمی‌شود'}, 'بازگشت: حذف کلید و همین متغیرها.')


async def _b_impact(a, text, risk='کم'):
    b = await find(a['bucket'])
    return {'downtime': 'ندارد', 'cost': 'بدون تغییر', 'risk': risk, 'bucket': b.get('name'), 'permission_now': b.get('permission')}, text


async def _resize_impact(a):
    b = await find(a['bucket'])
    old, new = (await plan_row(b['plan'])) if b.get('plan') else None, await plan_row(a['plan'])
    pre = await week_precheck(max(0, new['toman_per_hour'] - (old or {}).get('toman_per_hour', 0)))
    return ({'downtime': 'ندارد', 'cost': f"{(old or {}).get('toman_per_hour', 0):,.0f} → {new['toman_per_hour']:,.0f} تومان در ساعت", 'risk': 'کم', 'space': f"{new['space_gb']} گیگ",
             **({'balance_warning': pre['message_fa']} if pre['ok'] is False else {})}, 'کاهش پلن باکت ممکن نیست.')


async def _del_impact(a):
    b = await find(a['bucket'])
    objs = await list_objects(b['name'], limit=1)
    if objs['objects'] or objs['folders']:
        raise DiagError(f"bucket {b['name']} is not empty; empty it first")
    return ({'downtime': 'باکت برای همیشه حذف می‌شود', 'cost': 'هزینه‌ی باکت قطع می‌شود', 'risk': 'زیاد (برگشت‌ناپذیر)', 'bucket': b['name'], 'empty': True,
             'confirm': f"برای تأیید باید نام «{b['name']}» را تایپ کنید"}, 'برگشت‌ناپذیر است.')


async def _key_impact(a):
    for b in a.get('buckets') or []:
        await find(b)
    return ({'downtime': 'ندارد', 'cost': 'بدون هزینه', 'risk': 'متوسط: هر کس کلید را داشته باشد به باکت دسترسی دارد', 'buckets': a.get('buckets'),
             'permissions': a.get('permissions') or list(KEY_PERMS), 'secret': 'کلید محرمانه فقط یک بار در همین کارت نمایش داده می‌شود'}, 'بازگشت: حذف کلید.')


async def _key_ref_impact(a, text):
    k = await _key_by_ref(a['key'])
    return {'downtime': 'ندارد', 'cost': 'بدون هزینه', 'risk': 'زیاد: برنامه‌هایی که از این کلید استفاده می‌کنند قطع می‌شوند', 'access_key': mask_key(k.get('accessKey')),
            'buckets': [b.get('name') for b in k.get('buckets') or []]}, text


# ---------------------------------------------------------------- tools
BK = {'bucket': {'type': 'string', 'description': 'bucket name'}}


@spec('list_buckets', 'در حال فهرست کردن باکت‌های ذخیره‌سازی…', 'read',
      'Object storage buckets: plan, space, used MB, object count, permission, status, toman per hour, S3 endpoint.')
async def list_buckets_tool(a, ctx):
    return await list_buckets()


@spec('storage_plans', 'در حال خواندن پلن‌ها و قیمت ذخیره‌سازی…', 'read', 'Object storage plans with live prices (toman per hour/month) and space.')
async def storage_plans_tool(a, ctx):
    return {'plans': await plans(), 'endpoint': S3_ENDPOINT}


@spec('bucket_objects', 'در حال فهرست کردن فایل‌های باکت…', 'read', 'List the first N objects (and folders) of a bucket, optionally under a prefix.',
      {**BK, 'prefix': {'type': 'string', 'maxLength': 300}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}, 'page': {'type': 'integer', 'minimum': 1, 'maximum': 1000}}, ['bucket'])
async def bucket_objects_tool(a, ctx):
    return await list_objects(a['bucket'], a.get('prefix') or '', a.get('limit') or 50, a.get('page') or 1)


@spec('storage_keys', 'در حال خواندن کلیدهای دسترسی…', 'read', 'Access keys of the object storage (access key masked, buckets, permissions). Secret keys are never shown.')
async def storage_keys_tool(a, ctx):
    return await keys()


@spec('bucket_file_link', 'ساخت کارت تأیید برای لینک موقت فایل…', 'sensitive',
      'Create a temporary download link for one object (expiry seconds). The link gives access to the file, so it is shown once in the dashboard card.',
      {**BK, 'key': {'type': 'string', 'maxLength': 500}, 'expiry': {'type': 'integer', 'minimum': 60, 'maximum': 604800}}, ['bucket', 'key'],
      title=lambda a: f"لینک موقت {a.get('key')} از {a.get('bucket')}", impact=lambda a: _b_impact(a, 'لینک خودبه‌خود منقضی می‌شود.', 'متوسط: هر کس لینک را داشته باشد فایل را دارد'))
async def bucket_file_link_tool(a, ctx):
    url, exp = await temp_link(a['bucket'], a['key'], a.get('expiry') or 3600)
    audit(ctx, 'storage.link', None, {'bucket': a['bucket'], 'key': a['key'][:200], 'expiry': exp})
    return {'bucket': a['bucket'], 'key': a['key'], 'expiry_s': exp, 'link': 'hidden', 'secret_once': f"/api/diag/secret/{stash_secret(url, 'لینک موقت ' + a['key'][:80])}"}


@spec('create_bucket', 'ساخت کارت تأیید برای باکت جدید…', 'sensitive',
      'Create an object storage bucket (name, plan e.g. 20g-g2, permission private|public); optionally connect it to an app in the same card '
      '(a bucket-scoped key is created and LIARA_ENDPOINT/BUCKET_NAME/ACCESS_KEY/SECRET_KEY or AWS_* vars are injected, merged). Checks the one-week credit rule first.',
      {'name': {'type': 'string', 'pattern': NAME_RX.pattern}, 'plan': {'type': 'string'}, 'permission': {'type': 'string', 'enum': list(PERMS)},
       'connect_app': {'type': 'string'}, 'style': {'type': 'string', 'enum': list(ENV_STYLES)}, 'reason': {'type': 'string'}}, ['name'],
      title=lambda a: f"ساخت باکت «{a.get('name')}»" + (f" و اتصال به {a['connect_app']}" if a.get('connect_app') else ''), impact=_create_impact, timeout=120)
async def create_bucket_tool(a, ctx):
    r = await create_bucket(a['name'], a.get('plan') or '20g-g2', a.get('permission') or 'private', None, ctx)
    if a.get('connect_app'):
        r['connected'] = await connect(a['name'], a['connect_app'], a.get('style') or 'liara', ctx)
    return r


@spec('connect_bucket', 'ساخت کارت تأیید برای اتصال باکت به برنامه…', 'sensitive',
      'Connect a bucket to an app: create a key limited to that bucket and write endpoint, bucket and key env vars into the app (merged). Names only on the card; the app restarts.',
      {**BK, 'app': {'type': 'string'}, 'style': {'type': 'string', 'enum': list(ENV_STYLES)}, 'reason': {'type': 'string'}}, ['bucket', 'app'],
      title=lambda a: f"اتصال باکت {a.get('bucket')} به {a.get('app')}", impact=_connect_impact)
async def connect_bucket_tool(a, ctx):
    return await connect(a['bucket'], a['app'], a.get('style') or 'liara', ctx)


@spec('bucket_permission', 'ساخت کارت تأیید برای تغییر دسترسی باکت…', 'sensitive', 'Make a bucket private or public (public = anyone can read the files).',
      {**BK, 'permission': {'type': 'string', 'enum': list(PERMS)}}, ['bucket', 'permission'], title=lambda a: f"تغییر دسترسی {a.get('bucket')} به {a.get('permission')}",
      impact=lambda a: _b_impact(a, 'با تغییر دوباره‌ی دسترسی برمی‌گردد.', 'زیاد: فایل‌ها عمومی می‌شوند' if a.get('permission') == 'public' else 'کم'))
async def bucket_permission_tool(a, ctx):
    return await set_permission(a['bucket'], a['permission'])


@spec('resize_bucket', 'ساخت کارت تأیید برای ارتقای پلن باکت…', 'sensitive', 'Upgrade the plan (space) of a bucket; price delta on the card. Downgrades are not possible.',
      {**BK, 'plan': {'type': 'string'}}, ['bucket', 'plan'], title=lambda a: f"ارتقای باکت {a.get('bucket')} به {a.get('plan')}", impact=_resize_impact)
async def resize_bucket_tool(a, ctx):
    return await resize_bucket(a['bucket'], a['plan'])


@spec('delete_bucket', 'ساخت کارت تأیید برای حذف باکت…', 'sensitive',
      'DELETE an (empty) bucket permanently. The owner types the bucket name on the card; non-empty buckets are refused.',
      {**BK, 'reason': {'type': 'string'}}, ['bucket'], title=lambda a: f"حذف دائمی باکت {a.get('bucket')}", impact=_del_impact)
async def delete_bucket_tool(a, ctx):
    return await delete_bucket(a['bucket'], a.get('confirm_name'), ctx)


@spec('create_storage_key', 'ساخت کارت تأیید برای کلید دسترسی…', 'sensitive',
      'Create an access key for some buckets with permissions (read, put, list, delete). The secret is shown once in the dashboard card, never in chat.',
      {'buckets': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 20}, 'description': {'type': 'string', 'maxLength': 120},
       'permissions': {'type': 'array', 'items': {'type': 'string', 'enum': list(KEY_PERMS)}}}, ['buckets'],
      title=lambda a: f"کلید دسترسی برای {', '.join(a.get('buckets') or [])}", impact=_key_impact)
async def create_storage_key_tool(a, ctx):
    out, _, _ = await create_key(a['buckets'], a.get('description') or 'negahban', a.get('permissions') or list(KEY_PERMS), ctx)
    return out


@spec('revoke_storage_key', 'ساخت کارت تأیید برای باطل کردن کلید…', 'sensitive',
      'Revoke (rotate) the secret of an access key; the new secret is shown once in the dashboard. Apps using the old secret stop working.',
      {'key': {'type': 'string', 'description': 'key id, description or masked access key'}}, ['key'],
      title=lambda a: f"چرخش کلید دسترسی {a.get('key')}", impact=lambda a: _key_ref_impact(a, 'کلید قبلی قابل بازگشت نیست.'))
async def revoke_storage_key_tool(a, ctx):
    return await revoke_key(a['key'], ctx)


@spec('delete_storage_key', 'ساخت کارت تأیید برای حذف کلید دسترسی…', 'sensitive', 'Delete an access key (apps using it lose access).',
      {'key': {'type': 'string'}}, ['key'], title=lambda a: f"حذف کلید دسترسی {a.get('key')}", impact=lambda a: _key_ref_impact(a, 'برگشت‌ناپذیر است؛ کلید جدید بسازید.'))
async def delete_storage_key_tool(a, ctx):
    return await delete_key(a['key'], ctx)


LIMITATIONS = [
    ('ذخیره‌سازی: حذف باکت', 'نگهبان فقط باکت خالی را حذف می‌کند و فایل‌ها را دسته‌جمعی پاک نمی‌کند؛ پلن باکت فقط ارتقا می‌یابد.'),
    ('ذخیره‌سازی: کلیدها', 'کلید محرمانه فقط یک بار (هنگام ساخت یا چرخش) از لیارا گرفته می‌شود؛ نگهبان آن را فقط یک بار در داشبورد نشان می‌دهد.'),
]
