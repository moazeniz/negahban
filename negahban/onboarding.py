"""Initial setup ("راه‌اندازی اولیه"): one guided path from a repo/zip to a verified live app.
analyse (analyzer.py) -> recommend plan+bundle with live price and reasoning -> collect env values (secrets kept out of the
card, stored privately by reference) -> ONE approval card -> create app + deploy (build logs streamed live) -> verify ->
default subdomain / custom-domain guide -> saved as a reusable template."""
import time
from . import bus, plans, store
from .liara import LiaraError, liara
from .tools import ToolError, tool

APP_RX = r'^[a-z0-9][a-z0-9-]{2,30}$'


def stash_secrets(values):
    """Secret env values never enter a proposal row: they are parked in the private kv and referenced by id."""
    vals = {str(k)[:64]: str(v)[:4000] for k, v in (values or {}).items() if v not in (None, '')}
    if not vals:
        return None
    ref = store.uid('sec_')
    store.kv_set(f'pending_envs:{ref}', vals)
    return ref


def _meta(pid):
    m = store.kv_get(f'project:{pid}')
    if not m:
        raise ToolError('project not found: analyse the repo first (analyze_project / wizard)')
    return m


async def _impact(a):
    await plans.catalogue()
    plan, bundle = a.get('plan') or 'small-g2', a.get('bundle') or 'basic'
    h = plans.hourly(plan, bundle)
    m = store.kv_get(f"project:{a.get('project_id')}", {}) or {}
    exists = True
    try:
        await liara.app(a['app'])
    except LiaraError:
        exists = False
    envs = sorted((a.get('envs') or {}).keys())
    secrets = store.kv_get(f"pending_envs:{a['secret_ref']}", {}) if a.get('secret_ref') else {}
    missing = [e for e in (m.get('env_schema') or []) if e['name'] not in envs and e['name'] not in secrets]
    warn = None
    if not exists:  # Liara refuses new apps unless the balance covers ~one week of the team's run-rate (incl. the new app)
        try:
            from .tools import team_usage
            u = await team_usage({}, {})
            need = ((u.get('total_toman_per_hour') or 0) + h) * 24 * 7
            if u.get('balance_toman') is not None and u['balance_toman'] < need:
                warn = f"موجودی ({plans.fmt_toman(u['balance_toman'])}) برای ساخت برنامه‌ی جدید کافی نیست؛ لیارا موجودی معادل یک هفته هزینه ({plans.fmt_toman(need)}) می‌خواهد."
        except Exception:
            pass
    steps = (['ساخت برنامه در لیارا'] if not exists else []) + ['بارگذاری سورس و بیلد (لاگ بیلد زنده نمایش داده می‌شود)', 'تنظیم متغیرهای محیطی',
                                                                'بررسی سلامت پس از استقرار', 'فعال بودن زیردامنه‌ی رایگان .liara.run', 'ذخیره به‌عنوان قالب']
    return ({'downtime': 'ندارد (برنامه‌ی جدید)' if not exists else 'چند ثانیه هنگام جابه‌جایی نسخه',
             'cost': f"+{plans.fmt_toman(h)} در ساعت (≈{plans.fmt_toman(h * 720)} در ماه)" if not exists else 'بدون تغییر پلن برنامه‌ی موجود',
             'risk': 'کم' if not exists else 'متوسط — سورس فعلی جایگزین می‌شود', 'resources': f"{plan} ({plans.plan_sync(plan)['cpu']} هسته، {plans.plan_sync(plan)['ram']} گیگ رم) · باندل {bundle}",
             'deploys': 'یک استقرار از سهمیه‌ی روزانه', 'steps': steps, 'env_names': envs, 'secrets_provided': sorted(secrets),
             'secrets_missing': [e['name'] for e in missing if e.get('secret')], 'env_missing': [e['name'] for e in missing if not e.get('secret')],
             'framework': m.get('framework'), 'url': f"https://{a['app']}.liara.run", 'hourly': h, 'monthly': round(h * 720), **({'balance_warning': warn} if warn else {})},
            'حذف برنامه با کارت «حذف» (نام را تایپ کنید)؛ سورس در آرشیو نگهبان می‌ماند.')


@tool('launch_project', 'sensitive',
      'Initial setup in ONE approval card: create the Liara app (if needed) and deploy an analysed project (from analyze_project / the wizard), '
      'set its env vars, stream build logs, verify health, keep the free .liara.run subdomain and save it as a template. '
      'Pass non-secret env values in envs; secret values are typed by the owner on the card (never ask for secrets in chat).',
      {'project_id': {'type': 'string'}, 'app': {'type': 'string', 'pattern': APP_RX}, 'plan': {'type': 'string', 'enum': plans.ORDER},
       'bundle': {'type': 'string', 'enum': ['basic', 'standard', 'pro']}, 'envs': {'type': 'object'}, 'secret_ref': {'type': 'string'}},
      ['project_id', 'app'], title=lambda a: f"راه‌اندازی {a.get('app')} روی لیارا ({a.get('plan') or 'small-g2'})", impact=_impact,
      label_fa='آماده‌سازی کارت راه‌اندازی…')
async def launch_project(a, ctx):
    from .analyzer import project_dir
    from .deployer import _wait
    from .liara import make_tarball
    from . import workspace as ws
    pid, app = a['project_id'], a['app']
    m = _meta(pid)
    plan, bundle = a.get('plan') or 'small-g2', a.get('bundle') or 'basic'
    say = lambda t: bus.publish('op', proposal_id=ctx.get('proposal_id'), app=app, status='running', text=t)
    created = False
    try:
        await liara.app(app)
    except LiaraError as e:
        if e.status != 404:
            raise
        say(f'ساخت برنامه‌ی {app} با پلن {plan}…')
        await liara.create_app(app, plan, bundle, 'docker')
        created = True
    store.upsert_app(app, 'monitored', None, m.get('health_path') or '/', int(m.get('port') or 8080), {'origin': 'onboarding', 'project': pid})
    store.set_policy(app, {})
    envs = {str(k): str(v) for k, v in (a.get('envs') or {}).items()}
    for ref in (a.get('secret_ref'), ctx.get('env_ref')):
        if ref:
            envs.update(store.kv_get(f'pending_envs:{ref}', {}) or {})
            store.kv_set(f'pending_envs:{ref}', {})  # one-use
    say('بارگذاری سورس…')
    tb = make_tarball(project_dir(pid))
    aid = ws.archive_bytes(app, tb, f'initial setup {pid}', 'onboarding')
    rid = await liara.deploy_tar(app, tb, int(m.get('port') or 8080), envs or None, None, f'initial setup via Negahban ({pid})')
    store.x('update archive set release_id=? where id=?', (rid, aid))
    st, rel = await _wait(app, rid, ctx)
    store.set_archive_state(aid, st)
    tpl = None
    if st == 'READY':
        try:
            from .templates import save_from_project
            t = save_from_project(pid, app)
            tpl = t and {'id': t['id'], 'name': t['name']}
        except Exception:
            tpl = None
    store.kv_set(f'project:{pid}', {**m, 'app': app, 'launched_at': time.time(), 'release': rid})
    return {'app': app, 'created_app': created, 'release_id': rid, 'release_state': st, 'failReason': (rel or {}).get('failReason'),
            'url': f'https://{app}.liara.run', 'env_names': sorted(envs), 'template': tpl,
            'next_steps': ['آدرس رایگان https://%s.liara.run فعال است' % app,
                           'برای دامنه‌ی اختصاصی: صفحه‌ی «دامنه» یا در گفتگو بگویید «دامنه‌ی example.ir را به %s وصل کن» — رکوردهای DNS و مراحل ایرنیک نمایش داده می‌شود' % app,
                           'خودران و بودجه‌ی این برنامه را در «خودران و بودجه» تنظیم کنید']}
