"""Chat/MCP tools for the template library: list_templates, show_template (read), save_template (safe, local DB only) and
deploy_template (sensitive: approval card with cost, files and env schema; the executor creates the app/disk, deploys the
rendered files, registers monitoring and the autopilot bounds). Imported automatically at the end of tools.py."""
import os, tempfile, time
from pathlib import Path
from . import approvals, bus, plans, store
from . import templates as T
from . import workspace as ws
from .liara import LiaraError, liara, make_tarball
from .tools import ToolError, tool

# post-deploy verification (health, logs, release state) exactly like deploy_project
approvals.VERIFY.add('deploy_template')
approvals.LABEL['deploy_template'] = 'استقرار از قالب'
SECRET_KV = 'tplsecret:'  # typed secrets waiting for an approval (private kv, popped by the executor)

TID = {'template_id': {'type': 'string', 'description': 'template id from list_templates, e.g. fastapi, node-express, django-postgres'}}
PARAMS = {'type': 'object', 'additionalProperties': {'type': 'string'}, 'description': 'optional {{param}} overrides, e.g. {"port": "8000"}'}


def _tpl(tid):
    try:
        return T.must(tid)
    except T.TemplateError as e:
        raise ToolError(str(e))


def check_envs(t, envs):
    """Only NON-secret values may travel in tool args / proposal rows; secrets are typed on the card or set later with set_env."""
    if envs in (None, {}):
        return {}
    if not isinstance(envs, dict) or len(envs) > 60:
        raise ToolError('envs must be an object {NAME: value} (at most 60)')
    schema = {e['name']: e for e in t['env']}
    out = {}
    for k, v in envs.items():
        if not T.ENV_RX.match(str(k)) or not isinstance(v, (str, int, float)) or isinstance(v, bool) or len(str(v)) > 4000:
            raise ToolError(f'invalid env var {str(k)[:40]}')
        if (schema.get(k) or {}).get('secret') or (k not in schema and T.SECRET_NAME.search(k)):
            raise ToolError(f'{k} is a secret: never pass secret values in chat/tool args. The owner types it on the approval card or sets it later with set_env.')
        out[k] = str(v)
    return out


def _resolve(a):
    t = _tpl(a['template_id'])
    plan, bundle = a.get('plan') or t['plan'], a.get('bundle') or t['bundle']
    try:
        vals = T.values(t, a.get('params'), a['app'])
    except T.TemplateError as e:
        raise ToolError(str(e))
    return t, plan, bundle, vals, check_envs(t, a.get('envs'))


async def _exists(app):
    try:
        return bool(await liara.app(app))
    except LiaraError as e:
        if e.status == 404:
            return False
        raise


def _title(a):
    return f"استقرار {a.get('app')} از قالب {a.get('template_id')}" + (f" روی پلن {a['plan']}" if a.get('plan') else '')


async def _impact(a):
    t, plan, bundle, vals, envs = _resolve(a)
    await plans.catalogue()
    try:
        exists = await _exists(a['app'])
    except LiaraError as e:
        raise ToolError(f'could not read the app from Liara: {e.fa()}')
    pr, p = T.price(t, plan, bundle), plans.plan_sync(plan)
    files = T.render(t, a.get('params'), a['app'])
    diff = '\n'.join(f'+++ {path}\n' + '\n'.join('+' + l for l in c.splitlines()[:60]) + ('\n+…' if c.count('\n') > 60 else '') for path, c in files.items())
    defaults = {**T.env_defaults(t, vals), **envs}
    diff += '\n+++ متغیرهای محیطی (env)\n' + '\n'.join(
        f"+{e['name']}=" + ('<محرمانه: روی همین کارت یا بعداً با set_env وارد شود>' if e['secret'] else defaults.get(e['name'], '<خالی>'))
        + ('   # لازم' if e['required'] else '') for e in t['env'])
    disk = t.get('disk')
    imp = {'downtime': 'چند ثانیه هنگام جابه‌جایی نسخه' if exists else 'ندارد (برنامه‌ی جدید)',
           'cost': ('بدون تغییر پلن برنامه‌ی موجود' if exists else f"+{plans.fmt_toman(pr['hour'])} در ساعت (≈{plans.fmt_toman(pr['month'])} در ماه)"),
           'risk': 'متوسط — سورس فعلی برنامه با فایل‌های قالب جایگزین می‌شود' if exists else 'کم',
           'resources': f"{plan} ({p['cpu']} هسته، {p['ram']} گیگ رم) · باندل {bundle}" + (f" · دیسک {disk['name']} {disk['size_gb']}GB در {disk['mountTo']}" if disk else ''),
           'deploys': 'یکی از سهمیه‌ی استقرار روزانه مصرف می‌شود', 'diff': diff[:16000],
           'template': t['id'], 'template_version': t['version'], 'hourly': pr['hour'], 'monthly': pr['month'], 'app_exists': exists,
           'env_schema': [{k: e[k] for k in ('name', 'required', 'secret', 'description')} | {'value': '' if e['secret'] else defaults.get(e['name'], '')} for e in t['env']],
           'secrets_needed': [e['name'] for e in t['env'] if e['secret']], 'autoscale': t['autoscale']}
    rb = ('سورس فعلی در آرشیو نگهبان ذخیره است؛ با «بازگشت به نسخه‌ی قبلی» برمی‌گردد.' if exists else
          'برنامه‌ی تازه‌ساخته را می‌توان با «حذف برنامه» (با تأیید جداگانه) پاک کرد تا هزینه‌ای ادامه نیابد.')
    return imp, rb


@tool('list_templates', 'read', 'List the deploy-ready Liara templates (docker platform, no Kubernetes): builtin FastAPI, Node/Express, Next.js static, Django+Postgres, '
      'Laravel, Go, static nginx site, Python worker with cron, generic Docker, plus templates saved from earlier deploys. Shows stack, recommended plan and '
      'toman per hour/month. Use it before creating a new app so you reuse a template instead of writing a Dockerfile.',
      {'stack': {'type': 'string', 'description': 'optional filter, e.g. fastapi, node, django, php, go, static, worker, docker'}},
      label_fa='در حال خواندن کتابخانه‌ی قالب‌ها…')
async def list_templates(a, ctx):
    return [{'id': t['id'], 'name': t['name'], 'stack': t['stack'], 'builtin': t['builtin'], 'plan': t['plan'], 'bundle': t['bundle'], 'port': t['port'],
             'toman_per_hour': t['price']['hour'], 'toman_per_month': t['price']['month'], 'env': t['env'], 'secrets': t['secrets'],
             'disk': bool(t['disk']), 'description_fa': (t['description_fa'] or '')[:200]} for t in T.list_templates(a.get('stack'))]


@tool('show_template', 'read', 'Show one template: its rendered files (Dockerfile, liara.json, starter source; truncated), env-var schema, recommended plan/bundle, '
      'autopilot bounds, disk and version history. Pass app to render {{app_name}} for a concrete app name.',
      {**TID, 'app': {'type': 'string', 'description': 'optional app name used for {{app_name}}'}, 'params': PARAMS}, ['template_id'],
      label_fa='در حال خواندن فایل‌های قالب…')
async def show_template(a, ctx):
    t = _tpl(a['template_id'])
    try:
        files = T.render(t, a.get('params'), a.get('app'))
    except T.TemplateError as e:
        raise ToolError(str(e))
    budget, out = 9000, []
    for path, c in files.items():
        cut = c[:min(1800, max(0, budget))]
        budget -= len(cut)
        out.append({'path': path, 'content': cut + ('\n…[truncated]' if len(cut) < len(c) else ''), 'bytes': len(c)})
    return {'id': t['id'], 'name': t['name'], 'stack': t['stack'], 'builtin': t['builtin'], 'version': t['version'], 'description_fa': t['description_fa'],
            'plan': t['plan'], 'bundle': t['bundle'], 'price': T.price(t), 'port': t['port'], 'health_path': t['health_path'], 'health_check': t['health_check'],
            'autoscale': t['autoscale'], 'disk': t['disk'], 'cron': t['cron'], 'params': t['params'],
            'env_schema': [{k: e[k] for k in ('name', 'required', 'secret', 'description')} | {'default': e['default']} for e in t['env']],
            'versions': len(T.versions(t['id'])), 'files': out}


@tool('save_template', 'safe', 'Save a reusable template in the library (local only, nothing changes on Liara). Either clone an existing one with from_id plus '
      'overrides (name, plan, bundle, port, autopilot bounds), or pass a full template object (name, stack, port, health_path, health_check, cron, params, '
      'env, plan, bundle, autoscale, disk, files with a Dockerfile). Builtins are read-only, so customise them by cloning.',
      {'from_id': {'type': 'string', 'description': 'template to clone, e.g. fastapi'}, 'name': {'type': 'string'}, 'description_fa': {'type': 'string'},
       'plan': {'type': 'string', 'enum': plans.ORDER}, 'bundle': {'type': 'string', 'enum': list(T.BUNDLES)},
       'port': {'type': 'integer', 'minimum': 1, 'maximum': 65535}, 'min_plan': {'type': 'string', 'enum': plans.ORDER},
       'max_plan': {'type': 'string', 'enum': plans.ORDER}, 'max_toman_day': {'type': 'integer', 'minimum': 1000, 'maximum': 10000000},
       'template': {'type': 'object', 'description': 'full template object when not cloning'}}, ['name'],
      label_fa='در حال ذخیره‌ی قالب…')
async def save_template(a, ctx):
    ov = {k: a[k] for k in ('name', 'description_fa', 'plan', 'bundle', 'port') if a.get(k) not in (None, '')}
    auto = {k: a[k] for k in ('min_plan', 'max_plan', 'max_toman_day') if a.get(k) not in (None, '')}
    actor = ctx.get('actor', 'agent')
    try:
        if a.get('from_id'):
            src = _tpl(a['from_id'])
            if auto or 'plan' in ov:
                ov['autoscale'] = {**src['autoscale'], **auto}
                if 'plan' in ov and not auto:  # keep the bounds consistent with a new recommended plan
                    lo, hi = plans.ORDER.index(src['autoscale']['min_plan']), plans.ORDER.index(src['autoscale']['max_plan'])
                    i = plans.ORDER.index(ov['plan'])
                    ov['autoscale'].update(min_plan=plans.ORDER[min(lo, i)], max_plan=plans.ORDER[max(hi, i)])
            t = T.clone(src['id'], ov.get('name'), actor, ov)
        elif isinstance(a.get('template'), dict):
            data = {**a['template'], **ov}
            if auto:
                data['autoscale'] = {**(data.get('autoscale') or {}), **auto}
            t = T.create(data, actor, origin=f"{ctx.get('origin', 'chat')}")
        else:
            raise ToolError('pass from_id (clone) or a full template object')
    except T.TemplateError as e:
        raise ToolError(str(e))
    return {'saved': t['id'], 'name': t['name'], 'version': t['version'], 'price': T.price(t)}


@tool('deploy_template', 'sensitive', 'Create (if missing) and deploy a Liara app from a template, e.g. "deploy a FastAPI app from template fastapi called my-api on '
      'small-g2". Creates the app on the docker platform, creates the template disk, uploads the rendered Dockerfile + liara.json + starter source, waits '
      'for the release, registers monitoring and sets the autopilot bounds from the template. envs may contain NON-secret values only; secret values are '
      'typed by the owner on the approval card or set later with set_env.',
      {**TID, 'app': {'type': 'string', 'pattern': '^[a-z0-9][a-z0-9-]{2,30}$', 'description': 'Liara app name (new or existing)'},
       'plan': {'type': 'string', 'enum': plans.ORDER, 'description': 'defaults to the template recommendation'},
       'bundle': {'type': 'string', 'enum': list(T.BUNDLES)},
       'envs': {'type': 'object', 'additionalProperties': {'type': 'string'}, 'description': 'NON-secret env values {NAME: value}'},
       'params': PARAMS, 'reason': {'type': 'string'}}, ['template_id', 'app'],
      title=_title, impact=_impact, label_fa='در حال آماده‌سازی استقرار از قالب…')
async def deploy_template(a, ctx):
    t, plan, bundle, vals, envs = _resolve(a)
    app, pid = a['app'], ctx.get('proposal_id')
    secrets = {}
    if pid:
        secrets = store.kv_get(SECRET_KV + pid, {}) or {}
        store.x('delete from kv where k=?', (SECRET_KV + pid,))

    def op(text):
        bus.publish('op', proposal_id=pid, app=app, status='running', text=text)
    exists = await _exists(app)
    if not exists:
        op(f'ساخت برنامه‌ی {app} روی لیارا (پلن {plan}، باندل {bundle})…')
        await liara.create_app(app, plan, bundle, 'docker')
    disks = None
    if t.get('disk'):
        d = t['disk']
        try:
            op(f"ساخت دیسک {d['name']} ({d['size_gb']}GB)…")
            await liara.create_disk(app, d['name'], d['size_gb'])
        except LiaraError as e:
            if e.status != 409 and 'exist' not in str(e.message).lower():
                raise
        disks = [{'name': d['name'], 'mountTo': d['mountTo']}]
    files = T.render(t, a.get('params'), app)
    with tempfile.TemporaryDirectory(prefix='ngh-tpl-') as td:
        root = Path(td).resolve()
        for rel, content in files.items():
            p = (root / rel).resolve()
            if not str(p).startswith(str(root) + os.sep):
                raise ToolError(f'unsafe path {rel}')
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
        tb = make_tarball(td)
    msg = f"template {t['id']} v{t['version']} via Negahban"
    aid = ws.archive_bytes(app, tb, msg, 'template')
    env_all = {**T.env_defaults(t, vals), **((await liara.envs(app)) if exists else {}), **envs, **secrets}
    missing = [e['name'] for e in t['env'] if e['required'] and not env_all.get(e['name'])]
    port = int(vals['port'])
    op('بارگذاری فایل‌های قالب در لیارا…')
    rid = await liara.deploy_tar(app, tb, port, env_all or None, disks, msg, T.health_check(t, vals))
    store.x('update archive set release_id=? where id=?', (rid, aid))

    async def on_state(st):
        op(f'وضعیت ریلیز: {st}')
    st, rel = await liara.wait_release(app, rid, timeout=600, on_state=on_state)
    store.set_archive_state(aid, st)
    store.upsert_app(app, 'monitored', None, vals['health_path'], port)
    store.set_policy(app, {**store.policy(app), **t['autoscale']})
    store.audit(ctx.get('actor', 'owner'), 'template_deploy', app, {'template': t['id'], 'version': t['version'], 'release': rid, 'state': st,
                                                                     'created_app': not exists, 'missing_env': missing})
    return {'release_id': rid, 'release_state': st, 'url': f'https://{app}.liara.run', 'template': t['id'], 'template_version': t['version'],
            'created_app': not exists, 'archive_id': aid, 'missing_env': missing, 'failReason': (rel or {}).get('failReason'),
            **({'note': f"متغیرهای لازم هنوز مقدار ندارند: {', '.join(missing)} — با set_env تنظیم کنید."} if missing else {})}
