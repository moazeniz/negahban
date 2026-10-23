"""Server templates (kind `server`) for Liara virtual servers: plan + OS image + optional extra disk, SSH port and first-boot
shell script. Builtins are read-only; custom templates live in SQLite with a full version history (every edit = new
version, restore = new version with the old content), like the app templates. Every server Negahban creates from explicit
specs is saved here automatically. Prices are always live (iaas.plans)."""
import json, re, time
from .. import store
from ..redact import redact
from .common import DiagError, spec

KIND = 'server'
ID_RX = re.compile(r'^[a-z0-9][a-z0-9_-]{1,40}$')
OS_RX = re.compile(r'^[a-z][a-z0-9]*-[0-9][\w.-]{0,20}$')
EDITABLE = ('name', 'description_fa', 'plan', 'os', 'extra_disk_gb', 'ssh_port', 'init_script', 'tags')
SECRETISH = re.compile(r'(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S{6,}|-----BEGIN [A-Z ]*PRIVATE KEY-----')

PG_SCRIPT = """#!/bin/bash
# PostgreSQL 16 bound to localhost; firewall allows SSH only. Create users/databases after first login.
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y postgresql ufw unattended-upgrades
sed -i "s/^#\\?listen_addresses.*/listen_addresses = 'localhost'/" /etc/postgresql/*/main/postgresql.conf
systemctl enable --now postgresql
ufw allow OpenSSH
ufw --force enable
"""
BASE_SCRIPT = """#!/bin/bash
# security baseline: updates, firewall (SSH + HTTP/HTTPS), fail2ban
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ufw fail2ban unattended-upgrades
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
"""
BUILTINS = {t['id']: {**t, 'kind': KIND, 'builtin': True, 'version': 1, 'origin': 'builtin'} for t in [
    dict(id='ubuntu-mini', name='Ubuntu 24 · ۱ هسته · ۲ گیگ · ۲۰ گیگ (ارزان‌ترین)', plan='standard-base-g2', os='ubuntu-24.04', extra_disk_gb=0,
         description_fa='کوچک‌ترین و ارزان‌ترین سرور مجازی لیارا؛ برای آزمایش، ربات‌ها و سرویس‌های سبک.', tags=['ubuntu', 'ارزان']),
    dict(id='ubuntu-2c4g', name='Ubuntu 24 · ۲ هسته · ۴ گیگ · ۴۰ گیگ', plan='standard-plus-g2', os='ubuntu-24.04', extra_disk_gb=0, init_script=BASE_SCRIPT,
         description_fa='سرور همه‌منظوره با دیوار آتش (SSH/HTTP/HTTPS)، fail2ban و به‌روزرسانی خودکار امنیتی.', tags=['ubuntu', 'web']),
    dict(id='docker-host', name='میزبان Docker · ۲ هسته · ۴ گیگ', plan='standard-plus-g2', os='docker-29.1.2', extra_disk_gb=0,
         description_fa='تصویر آماده‌ی Docker 29 لیارا (Ubuntu + Docker Engine + Compose)؛ برای اجرای کانتینرها و docker compose.', tags=['docker']),
    dict(id='db-server', name='سرور پایگاه‌داده · PostgreSQL 16 · ۴ هسته · ۸ گیگ · ۸۰ گیگ', plan='pro-g2', os='ubuntu-24.04', extra_disk_gb=0, init_script=PG_SCRIPT,
         description_fa='PostgreSQL روی localhost با دیوار آتش فقط برای SSH؛ دسترسی از راه دور را آگاهانه و با تونل SSH باز کنید.', tags=['database', 'postgres']),
    dict(id='debian-mini', name='Debian 12 · ۱ هسته · ۲ گیگ · ۲۰ گیگ', plan='standard-base-g2', os='debian-12.9', extra_disk_gb=0,
         description_fa='Debian پایدار و سبک روی ارزان‌ترین پلن.', tags=['debian', 'ارزان']),
]}

_ready = set()


def _ensure():
    from ..config import settings
    k = str(settings.data_dir)
    if k in _ready:
        return
    store.x('create table if not exists server_templates(id text primary key, name text, version int, data text, origin text, created_at real, updated_at real)')
    store.x('create table if not exists server_template_versions(template_id text, version int, data text, ts real, actor text, note text, primary key(template_id, version))')
    _ready.add(k)


def validate(d):
    from .iaas import PLAN_ORDER
    errs = []
    if not isinstance(d, dict):
        raise DiagError('template must be an object')
    name = str(d.get('name') or '').strip()
    if not 2 <= len(name) <= 90:
        errs.append('نام قالب باید ۲ تا ۹۰ نویسه باشد')
    if d.get('plan') not in PLAN_ORDER:
        errs.append(f"پلن نامعتبر است ({', '.join(PLAN_ORDER)})")
    if not OS_RX.match(str(d.get('os') or '')):
        errs.append('سیستم‌عامل باید مثل ubuntu-24.04 یا docker-29.1.2 باشد')
    try:
        ed = int(d.get('extra_disk_gb') or 0)
        if not 0 <= ed <= 500:
            raise ValueError
    except (TypeError, ValueError):
        errs.append('دیسک اضافه باید بین ۰ تا ۵۰۰ گیگ باشد')
    if d.get('ssh_port') not in (None, '') and not (str(d['ssh_port']).isdigit() and 22 <= int(d['ssh_port']) <= 65535):
        errs.append('پورت SSH نامعتبر است')
    sc = d.get('init_script') or ''
    if len(sc) > 16000:
        errs.append('اسکریپت راه‌اندازی حداکثر ۱۶ هزار نویسه است')
    if SECRETISH.search(sc):
        errs.append('اسکریپت شامل رمز/کلید است؛ اسرار را در قالب ذخیره نکنید')
    if errs:
        raise DiagError('\n'.join(errs))
    return {'name': name, 'description_fa': str(d.get('description_fa') or '')[:400], 'plan': d['plan'], 'os': d['os'], 'extra_disk_gb': int(d.get('extra_disk_gb') or 0),
            'ssh_port': int(d['ssh_port']) if str(d.get('ssh_port') or '').isdigit() else None, 'init_script': sc or None,
            'tags': [str(t)[:30] for t in (d.get('tags') or [])][:8]}


def _row(r):
    d = json.loads(r['data'])
    return {**d, 'id': r['id'], 'kind': KIND, 'builtin': False, 'version': r['version'], 'origin': r['origin'], 'created_at': r['created_at'], 'updated_at': r['updated_at']}


def get(tid):
    if tid in BUILTINS:
        return dict(BUILTINS[tid])
    _ensure()
    r = store.q('select * from server_templates where id=?', (tid,), one=True)
    return _row(r) if r else None


def must(tid):
    t = get(tid)
    if not t:
        raise DiagError(f'server template {tid} not found')
    return t


def list_all():
    _ensure()
    return [dict(t) for t in BUILTINS.values()] + [_row(r) for r in store.q('select * from server_templates order by updated_at desc')]


async def with_prices(items):
    from .iaas import plan_list
    try:
        rows = {r['plan']: r for r in await plan_list()}
    except Exception:
        rows = {}
    out = []
    for t in items:
        p = rows.get(t['plan']) or {}
        out.append({**{k: v for k, v in t.items() if k != 'init_script'}, 'has_init_script': bool(t.get('init_script')),
                    'cpu': p.get('cpu'), 'ram_gb': p.get('ram_gb'), 'disk_gb': p.get('disk_gb'), 'toman_per_hour': p.get('toman_per_hour'),
                    'toman_per_month': p.get('toman_per_month'), 'price_live': bool(p)})
    return out


def _slug(name):
    s = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    return s[:30] or 'server'


def _new_id(base):
    _ensure()
    base = _slug(base)
    tid, i = base, 2
    while get(tid):
        tid, i = f'{base}-{i}', i + 1
    return tid


def _version(tid, v, data, actor, note):
    store.x('insert into server_template_versions(template_id,version,data,ts,actor,note) values(?,?,?,?,?,?)', (tid, v, json.dumps(data, ensure_ascii=False), time.time(), actor, note))


def create(data, actor='owner', origin='manual', want_id=None, note='ساخت قالب سرور'):
    d = validate(data)
    tid = want_id if want_id and ID_RX.match(want_id) and not get(want_id) else _new_id(want_id or d['name'])
    now = time.time()
    store.x('insert into server_templates(id,name,version,data,origin,created_at,updated_at) values(?,?,?,?,?,?,?)', (tid, d['name'], 1, json.dumps(d, ensure_ascii=False), origin, now, now))
    _version(tid, 1, d, actor, note)
    store.audit(actor, 'server_template.create', None, {'id': tid, 'name': d['name'], 'origin': origin})
    return get(tid)


def update(tid, data, actor='owner', note='ویرایش'):
    t = must(tid)
    if t['builtin']:
        raise DiagError('قالب‌های پیش‌فرض فقط‌خواندنی‌اند؛ از آن یک کپی بسازید')
    d = validate({**{k: t.get(k) for k in EDITABLE}, **{k: v for k, v in data.items() if k in EDITABLE}})
    v = t['version'] + 1
    store.x('update server_templates set name=?, version=?, data=?, updated_at=? where id=?', (d['name'], v, json.dumps(d, ensure_ascii=False), time.time(), tid))
    _version(tid, v, d, actor, note)
    store.audit(actor, 'server_template.update', None, {'id': tid, 'version': v})
    return get(tid)


def versions(tid):
    _ensure()
    return [{**{k: r[k] for k in ('version', 'ts', 'actor', 'note')}, 'data': json.loads(r['data'])}
            for r in store.q('select * from server_template_versions where template_id=? order by version desc', (tid,))]


def restore(tid, version, actor='owner'):
    old = next((v for v in versions(tid) if v['version'] == int(version)), None)
    if not old:
        raise DiagError('نسخه پیدا نشد')
    return update(tid, old['data'], actor, note=f'بازگردانی نسخه‌ی {version}')


def clone(tid, name=None, actor='owner'):
    t = must(tid)
    return create({**{k: t.get(k) for k in EDITABLE}, 'name': name or f"{t['name']} (کپی)"}, actor, origin=f'clone:{tid}')


def delete(tid, confirm_name, actor='owner'):
    t = must(tid)
    if t['builtin']:
        raise DiagError('قالب‌های پیش‌فرض حذف نمی‌شوند')
    if confirm_name != t['name']:
        raise DiagError('برای حذف، نام قالب را دقیقاً تایپ کنید')
    store.x('delete from server_templates where id=?', (tid,))
    store.audit(actor, 'server_template.delete', None, {'id': tid})
    return {'deleted': tid}


def save_from_server(server_name, c, actor='negahban'):
    """Auto-save the concrete spec of a server Negahban created (scripts are redacted defensively)."""
    from .iaas import PLAN_ORDER
    d = {'name': f"سرور {server_name} ({c['os']}، {c['plan']})",
         'description_fa': 'به‌طور خودکار از سروری که نگهبان ساخت ذخیره شد.', 'plan': c['plan'] if c['plan'] in PLAN_ORDER else 'standard-base-g2',
         'os': c['os'], 'extra_disk_gb': c.get('extra_disk_gb') or 0, 'ssh_port': c.get('ssh_port'),
         'init_script': redact(c['init_script']) if c.get('init_script') else None, 'tags': ['auto']}
    return create(d, actor, origin=f'server:{server_name}', want_id=f'srv-{server_name}', note='ذخیره‌ی خودکار از سرور ساخته‌شده')


# ---------------------------------------------------------------- tools
@spec('server_templates', 'در حال خواندن قالب‌های سرور…', 'read',
      'Server templates (plan + OS image + optional first-boot script) with live price per hour/month; use the id with create_server.')
async def server_templates_tool(a, ctx):
    return {'templates': await with_prices(list_all())}


@spec('save_server_template', 'در حال ذخیره‌ی قالب سرور…', 'safe',
      'Create (or, with id, edit -> new version) a custom server template: name, plan, os, optional extra_disk_gb, ssh_port, init_script (no secrets). '
      'Saving a template costs nothing; creating a server from it needs approval.',
      {'id': {'type': 'string'}, 'name': {'type': 'string', 'maxLength': 90}, 'description_fa': {'type': 'string', 'maxLength': 400},
       'plan': {'type': 'string'}, 'os': {'type': 'string'}, 'extra_disk_gb': {'type': 'integer', 'minimum': 0, 'maximum': 500},
       'ssh_port': {'type': 'integer', 'minimum': 22, 'maximum': 65535}, 'init_script': {'type': 'string', 'maxLength': 16000}}, ['name', 'plan', 'os'])
async def save_server_template_tool(a, ctx):
    data = {k: v for k, v in a.items() if k != 'id'}
    actor = (ctx or {}).get('actor') or 'agent'
    t = update(a['id'], data, actor) if a.get('id') and get(a['id']) and not get(a['id'])['builtin'] else create(data, actor, origin='chat')
    return (await with_prices([t]))[0]
