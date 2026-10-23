"""Liara IaaS (virtual servers) through the shared core client: https://iaas-api.liara.ir, same bearer token, ?teamID= on
every call (verified live 2026-10-22 from Hetzner and from inside negahban-sre; first call from Liara can take ~4 s).
Endpoints: /vm, /vm/{id}, /vm/power/{id} {action: start|stop|reboot|shutdown}, /vm/resize/{id} {plan}, /vm/disk/{id},
/vm/disk/{vm}/{disk}, /vm/reset-password/{id} (needs power off), /vm/reset-network/{id}, /vm/unfreeze/{id},
/vm/operation/{id}, /vm/traffic-volume|traffic-graph/{id}?startTimeUnix=, /plans, /oss, /init-scripts (console-only:
{name, description, type: shell, content}; a VM takes config.InitScriptID). Create body (console): {name, plan, OS, config:
{SSHKeys?, sshPort?, InitScriptID?}} -> {VMID, taskID}. One-click images are OS ids like docker-29.1.2.
Root passwords are never returned to the LLM, chat, audit or logs: a reset password is kept for 15 minutes behind a one-time
reveal token that only the owner's dashboard can open."""
import asyncio, os, re, secrets, time
from .. import store
from ..liara import LiaraError, liara
from .common import DiagError, audit, spec

IAAS = os.environ.get('LIARA_IAAS_BASE', 'https://iaas-api.liara.ir').rstrip('/')
PLAN_ORDER = ['standard-base-g2', 'standard-plus-g2', 'pro-g2', 'pro-plus-g2', 'express-g2', 'express-plus-g2']
NAME_RX = re.compile(r'^[a-z0-9][a-z0-9-]{2,30}$')
POWER_FA = {'start': 'روشن کردن', 'stop': 'خاموش کردن', 'reboot': 'راه‌اندازی مجدد', 'shutdown': 'قطع برق (خاموشی ناگهانی)'}
STATE_FA = {'CREATED': 'ساخته شده', 'CREATING': 'در حال ساخت', 'FROZEN': 'مسدود (اعتبار)', 'DELETING': 'در حال حذف', 'DELETED': 'حذف شده',
            'POWERED_ON': 'روشن', 'POWERED_OFF': 'خاموش', 'SUSPENDED': 'معلق'}
_cache = {'plans': (0, None), 'oss': (0, None)}


def get(path, **kw):
    return liara.get(path, base=IAAS, timeout=30, **kw)


def call(method, path, body=None, **kw):
    return liara.req(method, path, json_body=body, base=IAAS, timeout=45, **kw)


# ---------------------------------------------------------------- catalogue
async def plans(force=False):
    t, v = _cache['plans']
    if v is None or force or time.time() - t > 600:
        v = (await get('/plans')).get('plans') or {}
        _cache['plans'] = (time.time(), v)
    return v


def plan_row(pid, p):
    ip = (p.get('IPv4HourlyPrice') or [0])[0] or 0
    return {'plan': pid, 'cpu': (p.get('CPU') or {}).get('amount'), 'ram_gb': (p.get('RAM') or {}).get('amount'), 'disk_gb': p.get('volume'),
            'available': p.get('available'), 'toman_per_hour': round((p.get('hourlyPrice') or 0) + ip, 2), 'plan_toman_per_hour': p.get('hourlyPrice'),
            'ipv4_toman_per_hour': ip, 'toman_per_month': round((p.get('monthlyPrice') or 0) + ((p.get('IPv4MonthlyPrice') or [0])[0] or 0))}


async def plan_list():
    ps = await plans()
    rows = [plan_row(k, v) for k, v in ps.items() if v.get('available') and (v.get('hourlyPrice') or 0) > 0]
    return sorted(rows, key=lambda r: r['toman_per_hour'])


async def pick_plan(cpu=None, ram_gb=None, disk_gb=None):
    """Cheapest available plan with at least the requested resources."""
    for r in await plan_list():
        if (r['cpu'] or 0) >= (cpu or 0) and (r['ram_gb'] or 0) >= (ram_gb or 0) and (r['disk_gb'] or 0) >= (disk_gb or 0):
            return r
    raise DiagError(f'no available plan with ≥{cpu} cores, ≥{ram_gb} GB RAM, ≥{disk_gb} GB disk')


async def images(force=False):
    t, v = _cache['oss']
    if v is None or force or time.time() - t > 600:
        v = await get('/oss')
        _cache['oss'] = (time.time(), v)
    out = []
    for fam, vers in (v or {}).items():
        if fam == 'one-click-apps':
            out += [{'os': f'{app}-{ver}', 'kind': 'one-click', 'name': app, 'version': ver} for app, vs in (vers or {}).items() for ver in vs]
        else:
            out += [{'os': f'{fam}-{ver}', 'kind': 'os', 'name': fam, 'version': ver} for ver in vers or []]
    return out


async def check_os(os_id):
    ids = [i['os'] for i in await images()]
    if os_id not in ids:
        raise DiagError(f'unknown image {os_id}; available: {", ".join(ids)}')
    return os_id


# ---------------------------------------------------------------- servers
async def vms():
    return [_clean_detail(v) for v in ((await get('/vm')).get('vms') or [])]  # list items may also carry config.rootPassword


async def find(ref):
    ref = (ref or '').strip()
    for v in await vms():
        if ref in (v.get('_id'), v.get('name')):
            return v
    raise DiagError(f'server "{ref}" not found')


def _clean_detail(d):
    d = dict(d or {})
    cfg = dict(d.get('config') or {})
    cfg.pop('rootPassword', None)  # never leaves this module
    cfg['SSHKeys'] = [k[:24] + '…' for k in cfg.get('SSHKeys') or []]
    d['config'] = cfg
    return d


async def detail(vm_id):
    d = await get(f'/vm/{vm_id}')
    return _clean_detail(d.get('vm') if isinstance(d, dict) and isinstance(d.get('vm'), dict) else d)


def summarize(v, d=None, prices=None):
    d = d or {}
    pd = d.get('planDetails') or (prices or {}).get(v.get('plan')) or {}
    row = plan_row(v.get('plan'), pd) if pd else {}
    ips = [ip.get('address') for ip in d.get('IPs') or [] if ip.get('address')]
    return {'id': v.get('_id'), 'name': v.get('name'), 'state': v.get('state'), 'power': d.get('power') or v.get('power'), 'guest': d.get('guestState'),
            'state_fa': STATE_FA.get(d.get('power') or v.get('power')) or STATE_FA.get(v.get('state')), 'plan': v.get('plan'), 'os': v.get('OS'),
            'cpu': row.get('cpu'), 'ram_gb': row.get('ram_gb'), 'disk_gb': row.get('disk_gb'), 'ips': ips, 'ssh_port': (d.get('config') or {}).get('sshPort') or '22',
            'toman_per_hour': row.get('toman_per_hour'), 'toman_per_month': row.get('toman_per_month'), 'created_at': v.get('createdAt'), 'protected': protected(v.get('name')),
            'deletable_from': _when(earliest_delete(v))}


async def list_servers():
    vs, ps = await asyncio.gather(vms(), plans())
    ds = await asyncio.gather(*[detail(v['_id']) for v in vs[:20]], return_exceptions=True)
    rows = [summarize(v, d if isinstance(d, dict) else None, ps) for v, d in zip(vs, ds)]
    on = [r for r in rows if r['power'] == 'POWERED_ON' or r['state'] in ('CREATED', 'CREATING')]
    return {'count': len(rows), 'servers': rows, 'total_toman_per_hour': round(sum(r['toman_per_hour'] or 0 for r in rows), 1),
            'note': 'سرورها تا وقتی وجود دارند (حتی خاموش) ساعتی هزینه دارند؛ سرورهای بی‌استفاده را حذف کنید (لیارا تا ۲۴ ساعت پس از ساخت اجازه‌ی حذف نمی‌دهد)' if rows else 'هیچ سرور مجازی‌ای در تیم نیست',
            'running': len(on)}


async def server_info(ref):
    v = await find(ref)
    d, ops, dk = await asyncio.gather(detail(v['_id']), get(f"/vm/operation/{v['_id']}"), get(f"/vm/disk/{v['_id']}"), return_exceptions=True)
    out = summarize(v, d if isinstance(d, dict) else None, await plans())
    out['hostname'] = (d.get('config') or {}).get('hostname') if isinstance(d, dict) else None
    out['operations'] = [{k: o.get(k) for k in ('name', 'state', 'createdAt')} for o in (ops.get('operations') or [])][-15:] if isinstance(ops, dict) else []
    out['disks'] = dk.get('disks') if isinstance(dk, dict) else []
    out['disk_space_available_gb'] = dk.get('availableSize') if isinstance(dk, dict) else None
    try:
        tv = await get(f"/vm/traffic-volume/{v['_id']}", params={'startTimeUnix': int(time.time() - 86400)})
        out['traffic_24h_mb'] = round(sum(float(x.get('sumbytes') or 0) for x in (tv if isinstance(tv, list) else tv.get('data') or [])) / 2 ** 20, 2)
    except (LiaraError, AttributeError, TypeError, ValueError):
        out['traffic_24h_mb'] = None
    return out


async def traffic(ref, hours=24):
    v = await find(ref)
    start = int(time.time() - hours * 3600)
    g = await get(f"/vm/traffic-graph/{v['_id']}", params={'startTimeUnix': start})
    rows = g if isinstance(g, list) else (g or {}).get('data') or []
    per_min, per_dst = {}, {}
    for x in rows:
        b = float(x.get('sumbytes') or 0)
        m = int(x.get('minute') or 0)
        per_min[m] = per_min.get(m, 0) + b
        per_dst[x.get('dstip')] = per_dst.get(x.get('dstip'), 0) + b
    pts = sorted(per_min.items())
    ts = lambda m: m * 60 if m < 10 ** 10 / 60 else m
    return {'server': v['name'], 'hours': hours, 'total_mb': round(sum(per_min.values()) / 2 ** 20, 2),
            'top_destinations': [{'ip': k, 'mb': round(b / 2 ** 20, 2)} for k, b in sorted(per_dst.items(), key=lambda x: -x[1])[:8]],
            '_attach': {'kind': 'chart', 'type': 'area', 'title': f"ترافیک سرور {v['name']}", 'unit': 'MB', 'series': [{'name': 'ترافیک', 'points': [[ts(m), round(b / 2 ** 20, 3)] for m, b in pts]}]}}


async def status(vm_id):
    """Detail merged with the list row: right after creation /vm/{id} can omit `power` for minutes while /vm already has it."""
    d, rows = await asyncio.gather(detail(vm_id), vms())
    row = next((v for v in rows if v.get('_id') == vm_id), {})
    return {**d, **{k: row[k] for k in ('power', 'guestState', 'state') if row.get(k) and not d.get(k)}}


async def wait(vm_id, want=lambda d: d.get('power') == 'POWERED_ON', timeout=150, every=6):
    t0, last = time.time(), {}
    while time.time() - t0 < timeout:
        try:
            last = await status(vm_id)
            if want(last):
                return True, last
        except LiaraError as e:
            if e.status != 404:
                raise
            last = {'state': 'NOT_FOUND'}
            if want(last):
                return True, last
        await asyncio.sleep(every)
    return False, last


# ---------------------------------------------------------------- Liara rule: a VM can only be deleted 24 h after creation
MIN_LIFETIME_H = 24  # DELETE /vm/{id} answers 409 before that (console text; not in the OpenAPI spec). Verified live 2026-10-22.


def _created_ts(v):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(v.get('createdAt')).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def earliest_delete(v):
    t = _created_ts(v)
    return t + MIN_LIFETIME_H * 3600 if t else None


def _when(ts):
    return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ts)) if ts else None


# ---------------------------------------------------------------- protection
def protected(name):
    if not name:
        return False
    if name in {x.strip() for x in os.environ.get('PROTECTED_SERVERS', '').split(',') if x.strip()}:
        return True
    try:
        return bool(store.policy(f'vm:{name}').get('write_lock'))
    except Exception:
        return False


async def guard(ref, need_unprotected=True):
    v = await find(ref)
    if need_unprotected and protected(v['name']):
        raise DiagError(f"server {v['name']} is protected (write-locked); unlock it in the dashboard first")
    return v


# ---------------------------------------------------------------- one-time password reveal
def stash_secret(value, label):
    tok = secrets.token_urlsafe(18)
    store.kv_set(f'vmsecret:{tok}', {'v': value, 'label': label, 'exp': time.time() + 900})
    return tok


def reveal_secret(tok):
    k = f'vmsecret:{tok}'
    s = store.kv_get(k)
    store.x('delete from kv where k=?', (k,))
    if not s or s.get('exp', 0) < time.time():
        return None
    return {'label': s['label'], 'value': s['v']}


# ---------------------------------------------------------------- actions
async def create(name, plan, os_id, ssh_keys=None, ssh_port=None, init_script=None, extra_disk_gb=0, template_id=None, ctx=None):
    if not NAME_RX.match(name or ''):
        raise DiagError('server name: 3-31 chars, lowercase letters, digits and -')
    if any(v['name'] == name for v in await vms()):
        raise DiagError(f'a server named {name} already exists')
    ps = await plans()
    if plan not in ps or not ps[plan].get('available'):
        raise DiagError(f'plan {plan} is not available')
    await check_os(os_id)
    cfg = {}
    if ssh_keys:
        cfg['SSHKeys'] = [k.strip() for k in ssh_keys if k.strip()][:5]
    if ssh_port:
        cfg['sshPort'] = str(int(ssh_port))
    script_id = None
    if init_script:
        r = await call('POST', '/init-scripts', {'name': f'negahban-{name}'[:40], 'description': 'created by Negahban', 'type': 'shell', 'content': init_script})
        script_id = _find_id(r)
        if not script_id:  # fall back to the list (response shape is undocumented)
            lst = (await get('/init-scripts')).get('initScripts') or []
            script_id = next((s.get('_id') for s in lst if s.get('name') == f'negahban-{name}'[:40]), None)
        if script_id:
            cfg['InitScriptID'] = script_id
    body = {'name': name, 'plan': plan, 'OS': os_id, **({'config': cfg} if cfg else {})}
    r = await call('POST', '/vm', body)
    vm_id = (r or {}).get('VMID') or _find_id(r)
    if not vm_id:
        vm_id = next((v['_id'] for v in await vms() if v['name'] == name), None)
    audit(ctx, 'iaas.create', None, {'server': name, 'plan': plan, 'os': os_id, 'vm_id': vm_id, 'template': template_id, 'init_script': bool(script_id)})
    ok, d = await wait(vm_id, lambda d: d.get('power') == 'POWERED_ON' and bool(d.get('IPs')), timeout=150) if vm_id else (False, {})
    out = {'server': name, 'id': vm_id, 'task_id': (r or {}).get('taskID'), 'plan': plan, 'os': os_id, 'booted': ok, 'power': d.get('power'),
           'ips': [ip.get('address') for ip in d.get('IPs') or []], 'init_script_id': script_id, 'toman_per_hour': plan_row(plan, ps[plan])['toman_per_hour'],
           'access': 'use the SSH key you gave, or server_reset_password (one-time reveal in the dashboard)'}
    if extra_disk_gb and vm_id and ok:
        try:
            await call('POST', f'/vm/disk/{vm_id}', {'size': int(extra_disk_gb)})
            out['extra_disk_gb'] = int(extra_disk_gb)
        except LiaraError as e:
            out['extra_disk_error'] = e.fa()
    return out


def _find_id(o):
    if isinstance(o, dict):
        for k in ('VMID', '_id', 'id', 'ID'):
            if isinstance(o.get(k), str):
                return o[k]
        for v in o.values():
            r = _find_id(v)
            if r:
                return r
    return None


async def power(ref, action, ctx=None):
    if action not in POWER_FA:
        raise DiagError('action must be start, stop, reboot or shutdown')
    v = await guard(ref)
    await call('PATCH', f"/vm/power/{v['_id']}", {'action': action})
    want = {'start': 'POWERED_ON', 'reboot': 'POWERED_ON', 'stop': 'POWERED_OFF', 'shutdown': 'POWERED_OFF'}[action]
    ok, d = await wait(v['_id'], lambda d: d.get('power') == want, timeout=90, every=5)
    return {'server': v['name'], 'action': action, 'power': d.get('power'), 'reached_expected_state': ok}


async def resize(ref, plan):
    v = await guard(ref)
    if plan not in PLAN_ORDER:
        raise DiagError(f'plan must be one of {PLAN_ORDER}')
    r = await call('PATCH', f"/vm/resize/{v['_id']}", {'plan': plan})
    return {'server': v['name'], 'from': v.get('plan'), 'to': plan, 'task_id': (r or {}).get('taskID')}


async def add_disk(ref, size_gb):
    v = await guard(ref)
    await call('POST', f"/vm/disk/{v['_id']}", {'size': int(size_gb)})
    return {'server': v['name'], 'added_gb': int(size_gb), 'disks': (await get(f"/vm/disk/{v['_id']}")).get('disks')}


async def extend_disk(ref, disk_id, new_size_gb):
    v = await guard(ref)
    r = await call('POST', f"/vm/disk/{v['_id']}/{disk_id}", {'newSize': int(new_size_gb)})
    return {'server': v['name'], 'disk': disk_id, 'new_size_gb': int(new_size_gb), 'task_id': (r or {}).get('taskID')}


async def reset_password(ref, ctx=None):
    v = await guard(ref)
    r = await call('POST', f"/vm/reset-password/{v['_id']}")
    pw = (r or {}).get('password')
    if not pw:
        raise DiagError('Liara returned no password (the server must be powered off first)')
    tok = stash_secret(pw, f"رمز root سرور {v['name']}")
    audit(ctx, 'iaas.reset_password', None, {'server': v['name']})
    return {'server': v['name'], 'password': 'hidden', 'secret_once': f'/api/diag/servers/secret/{tok}',
            'note': 'the new root password is shown ONCE in the dashboard card (valid 15 minutes); it is not stored in chat or logs'}


async def reset_network(ref):
    v = await guard(ref)
    await call('POST', f"/vm/reset-network/{v['_id']}")
    return {'server': v['name'], 'network_reset': True}


async def delete(ref, confirm_name, ctx=None):
    v = await guard(ref)
    if confirm_name != v['name']:
        raise DiagError('confirmation name does not match the server name')
    ed = earliest_delete(v)
    if ed and ed > time.time():
        raise DiagError(f"Liara allows deleting a server only {MIN_LIFETIME_H} h after creation; {v['name']} can be deleted from {_when(ed)}")
    try:
        await call('DELETE', f"/vm/{v['_id']}")
    except LiaraError as e:
        if e.status == 409:
            raise DiagError(f"Liara refused (409): a server can be deleted only {MIN_LIFETIME_H} h after creation (earliest {_when(ed)})")
        raise
    gone, d = await wait(v['_id'], lambda d: d.get('state') in ('NOT_FOUND', 'DELETED', 'DELETING'), timeout=60, every=5)
    audit(ctx, 'iaas.delete', None, {'server': v['name'], 'id': v['_id']})
    return {'deleted': v['name'], 'id': v['_id'], 'state': d.get('state'), 'confirmed_gone': gone}


# ---------------------------------------------------------------- approval-card helpers
async def _price_of(plan):
    ps = await plans()
    if plan not in ps:
        raise DiagError(f'unknown plan {plan}')
    return plan_row(plan, ps[plan])


async def resolve_create(a):
    """Template / explicit / cpu+ram args -> concrete (plan, os, extra_disk, init_script, template_id)."""
    from . import server_templates as ST
    t = ST.must(a['template']) if a.get('template') else None
    plan = a.get('plan') or (t or {}).get('plan')
    if not plan and (a.get('cpu') or a.get('ram_gb')):
        plan = (await pick_plan(a.get('cpu'), a.get('ram_gb'), a.get('disk_gb')))['plan']
    plan = plan or 'standard-base-g2'
    os_id = a.get('os') or (t or {}).get('os') or 'ubuntu-24.04'
    return {'plan': plan, 'os': os_id, 'extra_disk_gb': int(a.get('extra_disk_gb') or (t or {}).get('extra_disk_gb') or 0),
            'init_script': a.get('init_script') or (t or {}).get('init_script') or None, 'ssh_port': a.get('ssh_port') or (t or {}).get('ssh_port'),
            'template_id': (t or {}).get('id'), 'template_name': (t or {}).get('name')}


def _cost_text(row):
    return f"{row['toman_per_hour']:,.0f} تومان در ساعت (~{row['toman_per_month']:,.0f} تومان در ماه، با IPv4)"


async def _create_impact(a):
    c = await resolve_create(a)
    row = await _price_of(c['plan'])
    if not NAME_RX.match(a.get('name') or ''):
        raise DiagError('server name: 3-31 chars, lowercase letters, digits and -')
    await check_os(c['os'])
    return ({'downtime': 'ندارد (سرور جدید)', 'cost': '+' + _cost_text(row), 'plan': c['plan'],
             'risk': f"لیارا تا {MIN_LIFETIME_H} ساعت پس از ساخت اجازه‌ی حذف نمی‌دهد؛ حداقل هزینه ≈ {row['toman_per_hour'] * MIN_LIFETIME_H:,.0f} تومان",
             'min_cost_toman': round(row['toman_per_hour'] * MIN_LIFETIME_H),
             'specs': f"{row['cpu']} هسته · {row['ram_gb']} گیگ RAM · {row['disk_gb']} گیگ دیسک", 'os': c['os'], 'template': c['template_name'],
             'init_script': bool(c['init_script']), 'toman_per_hour': row['toman_per_hour']},
            f'بازگشت: حذف سرور با «حذف سرور» (تایپ نام)، ولی تنها {MIN_LIFETIME_H} ساعت پس از ساخت؛ هزینه ساعتی است.')


def _power_title(a):
    return f"{POWER_FA.get(a.get('action'), a.get('action'))} سرور {a.get('server')}"


async def _power_impact(a):
    v = await guard(a['server'])
    down = {'start': 'ندارد', 'reboot': 'حدود ۱ دقیقه قطعی', 'stop': 'سرور تا روشن شدن دوباره در دسترس نیست', 'shutdown': 'قطع ناگهانی؛ داده‌ی ذخیره‌نشده از دست می‌رود'}
    return ({'downtime': down.get(a.get('action'), '?'), 'cost': 'بدون تغییر (سرور خاموش هم هزینه دارد)', 'risk': 'زیاد' if a.get('action') == 'shutdown' else 'کم',
             'server': v['name'], 'power_now': v.get('power')}, 'با عمل مخالف (روشن/خاموش) برمی‌گردد.')


async def _resize_impact(a):
    v = await guard(a['server'])
    old, new = await _price_of(v['plan']), await _price_of(a['plan'])
    return ({'downtime': 'سرور برای تغییر پلن خاموش/روشن می‌شود', 'cost': f"{old['toman_per_hour']:,.0f} → {new['toman_per_hour']:,.0f} تومان در ساعت",
             'risk': 'متوسط', 'from': v['plan'], 'to': a['plan'], 'specs': f"{new['cpu']} هسته · {new['ram_gb']} گیگ RAM · {new['disk_gb']} گیگ دیسک"},
            f"بازگشت با تغییر پلن به {v['plan']} (کوچک کردن دیسک ممکن نیست).")


async def _disk_impact(a):
    v = await guard(a['server'])
    return ({'downtime': 'ندارد', 'cost': 'از سهم دیسک پلن سرور برداشته می‌شود', 'risk': 'کم', 'server': v['name'],
             'size_gb': a.get('size_gb') or a.get('new_size_gb')}, 'دیسک را نمی‌توان کوچک کرد؛ حذف دیسک داده‌هایش را پاک می‌کند.')


async def _pw_impact(a):
    v = await guard(a['server'])
    return ({'downtime': 'ندارد (سرور باید خاموش باشد)', 'cost': 'بدون هزینه', 'risk': 'متوسط: رمز قبلی root باطل می‌شود', 'server': v['name'], 'power_now': v.get('power'),
             'secret': 'رمز جدید فقط یک بار در همین کارت نمایش داده می‌شود'}, 'رمز قبلی قابل بازگشت نیست؛ در صورت نیاز دوباره بازنشانی کنید.')


async def _net_impact(a):
    v = await guard(a['server'])
    return ({'downtime': 'اتصال شبکه چند ثانیه قطع می‌شود', 'cost': 'بدون هزینه', 'risk': 'کم', 'server': v['name']}, 'نیازی به بازگشت نیست.')


async def _del_impact(a):
    v = await guard(a['server'])
    ed = earliest_delete(v)
    if ed and ed > time.time():
        raise DiagError(f"Liara allows deleting a server only {MIN_LIFETIME_H} h after creation; {v['name']} can be deleted from {_when(ed)}")
    row = await _price_of(v['plan'])
    return ({'downtime': 'سرور و همه‌ی داده‌هایش برای همیشه حذف می‌شود', 'cost': '-' + _cost_text(row), 'risk': 'بسیار زیاد (برگشت‌ناپذیر)',
             'server': v['name'], 'confirm': f"برای تأیید باید نام «{v['name']}» را تایپ کنید"}, 'برگشت‌ناپذیر است؛ فقط با ساخت سرور جدید از قالب جایگزین می‌شود.')


# ---------------------------------------------------------------- tools
SRV = {'server': {'type': 'string', 'description': 'server name or id'}}


@spec('list_servers', 'در حال فهرست کردن سرورهای مجازی…', 'read',
      'List the team\'s Liara virtual servers (IaaS): state, power, plan with cores/RAM/disk, OS, IPs, toman per hour/month, protection.', timeout=60)
async def list_servers_tool(a, ctx):
    return await list_servers()


@spec('server_details', 'در حال خواندن جزئیات سرور…', 'read',
      'One virtual server: specs, power/guest state, IPs, SSH port, disks and free disk space, recent operations, 24 h traffic. Never returns passwords.',
      SRV, ['server'], timeout=60)
async def server_details_tool(a, ctx):
    return await server_info(a['server'])


@spec('server_traffic', 'در حال خواندن ترافیک سرور…', 'read', 'Network traffic of a virtual server over the last N hours (chart + top destinations).',
      {**SRV, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 168}}, ['server'])
async def server_traffic_tool(a, ctx):
    return await traffic(a['server'], int(a.get('hours') or 24))


@spec('server_plans', 'در حال خواندن پلن‌ها و قیمت سرورها…', 'read',
      'Virtual server (IaaS) plans with live prices (toman per hour and month incl. IPv4), cores, RAM, disk; cheapest first.')
async def server_plans_tool(a, ctx):
    return {'plans': await plan_list(), 'note': 'billing is hourly from creation until deletion (powered-off servers are still billed)'}


@spec('server_images', 'در حال خواندن سیستم‌عامل‌های قابل نصب…', 'read', 'Operating systems and one-click images (e.g. docker-29.1.2) available for new virtual servers.')
async def server_images_tool(a, ctx):
    return {'images': await images()}


CREATE_PARAMS = {'name': {'type': 'string', 'pattern': NAME_RX.pattern, 'description': 'lowercase server name'},
                 'template': {'type': 'string', 'description': 'server template id (see server_templates)'},
                 'plan': {'type': 'string', 'enum': PLAN_ORDER}, 'os': {'type': 'string', 'description': 'e.g. ubuntu-24.04, debian-12.9, docker-29.1.2'},
                 'cpu': {'type': 'integer', 'minimum': 1, 'maximum': 32}, 'ram_gb': {'type': 'integer', 'minimum': 1, 'maximum': 128},
                 'disk_gb': {'type': 'integer', 'minimum': 10, 'maximum': 640}, 'extra_disk_gb': {'type': 'integer', 'minimum': 0, 'maximum': 500},
                 'ssh_keys': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 5}, 'ssh_port': {'type': 'integer', 'minimum': 22, 'maximum': 65535},
                 'init_script': {'type': 'string', 'maxLength': 16000, 'description': 'optional shell script run on first boot'}, 'reason': {'type': 'string'}}


def _create_title(a):
    return f"ساخت سرور مجازی {a.get('name')}" + (f" از قالب {a['template']}" if a.get('template') else '')


@spec('create_server', 'ساخت کارت تأیید برای سرور جدید…', 'sensitive',
      'Create a Liara virtual server from a server template, or from plan+os, or from cpu+ram_gb (cheapest matching plan is chosen). '
      'Billing starts immediately (hourly) and Liara forbids deleting a server during its first 24 h (minimum cost = 24 x hourly price). Optional SSH keys, SSH port, first-boot shell script and extra disk. The created server is saved as a server template.',
      CREATE_PARAMS, ['name'], title=_create_title, impact=_create_impact, timeout=240)
async def create_server_tool(a, ctx):
    from . import server_templates as ST
    c = await resolve_create(a)
    r = await create(a['name'], c['plan'], c['os'], a.get('ssh_keys'), c['ssh_port'], c['init_script'], c['extra_disk_gb'], c['template_id'], ctx)
    if not c['template_id']:
        try:
            r['saved_template'] = ST.save_from_server(a['name'], c, actor=(ctx or {}).get('actor') or 'negahban')['id']
        except Exception as e:
            r['saved_template_error'] = str(e)[:120]
    else:
        r['template'] = c['template_id']
    return r


@spec('server_power', 'ساخت کارت تأیید برای روشن/خاموش کردن سرور…', 'sensitive',
      'Power action on a virtual server: start | stop (graceful) | reboot | shutdown (hard power cut).',
      {**SRV, 'action': {'type': 'string', 'enum': list(POWER_FA)}, 'reason': {'type': 'string'}}, ['server', 'action'],
      title=_power_title, impact=_power_impact, timeout=120)
async def server_power_tool(a, ctx):
    return await power(a['server'], a['action'], ctx)


@spec('resize_server', 'ساخت کارت تأیید برای تغییر پلن سرور…', 'sensitive', 'Change the plan (cores/RAM/disk) of a virtual server; price delta shown on the card.',
      {**SRV, 'plan': {'type': 'string', 'enum': PLAN_ORDER}, 'reason': {'type': 'string'}}, ['server', 'plan'],
      title=lambda a: f"تغییر پلن سرور {a.get('server')} به {a.get('plan')}", impact=_resize_impact)
async def resize_server_tool(a, ctx):
    return await resize(a['server'], a['plan'])


@spec('server_add_disk', 'ساخت کارت تأیید برای افزودن دیسک به سرور…', 'sensitive', 'Add a new disk (GB) to a virtual server (taken from the plan disk allowance).',
      {**SRV, 'size_gb': {'type': 'integer', 'minimum': 1, 'maximum': 640}}, ['server', 'size_gb'],
      title=lambda a: f"افزودن دیسک {a.get('size_gb')} گیگ به سرور {a.get('server')}", impact=_disk_impact)
async def server_add_disk_tool(a, ctx):
    return await add_disk(a['server'], a['size_gb'])


@spec('server_extend_disk', 'ساخت کارت تأیید برای بزرگ کردن دیسک سرور…', 'sensitive', 'Grow an existing disk of a virtual server to new_size_gb (cannot shrink).',
      {**SRV, 'disk_id': {'type': 'string'}, 'new_size_gb': {'type': 'integer', 'minimum': 1, 'maximum': 640}}, ['server', 'disk_id', 'new_size_gb'],
      title=lambda a: f"بزرگ کردن دیسک سرور {a.get('server')} به {a.get('new_size_gb')} گیگ", impact=_disk_impact)
async def server_extend_disk_tool(a, ctx):
    return await extend_disk(a['server'], a['disk_id'], a['new_size_gb'])


@spec('server_reset_password', 'ساخت کارت تأیید برای بازنشانی رمز root…', 'sensitive',
      'Reset the root password of a (powered-off) virtual server. The new password is shown once in the dashboard card only, never in chat.',
      SRV, ['server'], title=lambda a: f"بازنشانی رمز root سرور {a.get('server')}", impact=_pw_impact)
async def server_reset_password_tool(a, ctx):
    return await reset_password(a['server'], ctx)


@spec('server_reset_network', 'ساخت کارت تأیید برای بازنشانی شبکه‌ی سرور…', 'sensitive', 'Reset the network configuration of a virtual server.',
      SRV, ['server'], title=lambda a: f"بازنشانی شبکه‌ی سرور {a.get('server')}", impact=_net_impact)
async def server_reset_network_tool(a, ctx):
    return await reset_network(a['server'])


@spec('delete_server', 'ساخت کارت تأیید برای حذف سرور…', 'sensitive',
      'DELETE a virtual server permanently (irreversible). The owner must type the server name on the card to confirm.',
      {**SRV, 'reason': {'type': 'string'}}, ['server'],  # confirm_name is NOT a param: only the owner's typed value (approval extra) counts
      title=lambda a: f"حذف دائمی سرور {a.get('server')}", impact=_del_impact, timeout=120)
async def delete_server_tool(a, ctx):
    return await delete(a['server'], a.get('confirm_name'), ctx)
