"""Dashboard API of the diagnostics toolkit (/api/diag): terminal (allowlisted exec runs now, anything else becomes an approval
card, blocked commands are refused), quick checks, runbooks and the log explorer. Every call goes through tools.run(), so
validation, redaction, approval routing and audit are the same as in the chat. GETs need a session; POSTs also CSRF."""
from fastapi import APIRouter, HTTPException, Request
from .. import store
from ..config import settings
from ..security import rate, require
from . import LABELS, all_specs
from .common import locked
from .exec import classify

router = APIRouter(prefix='/api/diag')
VERDICT_FA = {'read': 'فقط‌خواندنی؛ بی‌درنگ اجرا می‌شود', 'sensitive': 'نیاز به تأیید مالک دارد (کارت تأیید ساخته می‌شود)', 'blocked': 'ممنوع؛ هرگز اجرا نمی‌شود'}


async def _body(request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    if not isinstance(b, dict):
        raise HTTPException(400, 'بدنه‌ی درخواست باید یک شیء JSON باشد')
    return b


async def _run(name, args, request):
    from ..tools import TOOLS, run
    if name not in LABELS or name not in TOOLS:
        raise HTTPException(404, 'ابزار عیب‌یابی پیدا نشد')
    try:
        return await run(name, args, {'origin': 'dashboard', 'actor': 'owner', 'session': 'dashboard'})
    except Exception as e:  # validation errors are user errors
        raise HTTPException(400, str(e)[:300])


@router.get('/tools')
async def tools_(request: Request):
    require(request)
    return [{'name': s['name'], 'label': s['label_fa'], 'risk': s['risk'], 'desc': s['desc'], 'params': list(s['params'])} for s in all_specs()]


@router.get('/apps')
async def apps(request: Request):
    require(request)
    from .. import teams
    names = [a['name'] for a in store.apps() if teams.app_team(a) == teams.current()]  # the selected team's apps only
    try:
        from ..liara import liara
        names += [p['project_id'] for p in await liara.list_apps() if p['project_id'] not in names]
    except Exception:
        pass
    return [{'name': n, 'locked': locked(n), 'self': n == settings.self_app} for n in names]


@router.get('/classify')
async def classify_(request: Request, command: str = ''):
    require(request)
    v, why, argv = classify(command[:400])
    return {'verdict': v, 'why': why, 'label': VERDICT_FA[v]}


@router.post('/exec')
async def exec_(request: Request):
    require(request, csrf=True)
    rate('diag_exec', 30, 60)
    b = await _body(request)
    app, cmd = str(b.get('app') or ''), str(b.get('command') or '')[:400]
    v, why, _ = classify(cmd)
    if v == 'blocked':
        store.audit('owner', 'exec.blocked', app, {'command': cmd, 'why': why}, ok=False)
        raise HTTPException(400, f'این فرمان ممنوع است ({why}) و هرگز اجرا نمی‌شود')
    if v == 'sensitive' and locked(app):
        raise HTTPException(403, f'{app} فقط اجازه‌ی فرمان‌های فقط‌خواندنی دارد')
    res = await _run('container_exec' if v == 'read' else 'container_exec_any', {'app': app, 'command': cmd}, request)
    return {'verdict': v, 'label': VERDICT_FA[v], 'result': res}


@router.post('/run')
async def run_(request: Request):
    require(request, csrf=True)
    rate('diag_run', 40, 60)
    b = await _body(request)
    args = b.get('args') or {}
    if not isinstance(args, dict):
        raise HTTPException(400, 'args باید شیء باشد')
    return await _run(str(b.get('tool') or ''), args, request)


@router.get('/logs')
async def logs(request: Request, app: str, minutes: int = 60, search: str = None):
    require(request)
    rate('diag_logs', 30, 60)
    args = {'app': app, 'minutes': max(5, min(int(minutes), 1440))}
    if search and len(search.strip()) >= 3:
        args['search'] = search.strip()[:100]
    return await _run('log_insights', args, request)


@router.get('/trace')
async def trace(request: Request, app: str, request_id: str, minutes: int = 180):
    require(request)
    return await _run('log_trace', {'app': app, 'request_id': request_id[:100], 'minutes': max(5, min(int(minutes), 1440))}, request)


# ---------------------------------------------------------------- virtual servers (IaaS) + server templates
def _st():
    from . import server_templates
    return server_templates


async def _guarded(fn, *a, **kw):
    from .common import DiagError
    try:
        r = fn(*a, **kw)
        return await r if hasattr(r, '__await__') else r
    except DiagError as e:
        raise HTTPException(400, str(e))


@router.get('/servers')
async def servers(request: Request):
    require(request)
    return await _run('list_servers', {}, request)


@router.get('/servers/catalog')
async def servers_catalog(request: Request):
    require(request)
    from . import iaas
    plans_, imgs = await iaas.plan_list(), await iaas.images()
    return {'plans': plans_, 'images': imgs}


@router.get('/servers/{ref}')
async def server(ref: str, request: Request):
    require(request)
    return await _run('server_details', {'server': ref}, request)


@router.post('/servers/secret/{tok}')
async def server_secret(tok: str, request: Request):
    """One-time reveal of a reset root password (POST + CSRF so link previews cannot burn it)."""
    require(request, csrf=True)
    rate('diag_secret', 10, 60)
    from . import iaas
    s = iaas.reveal_secret(tok[:64])
    if not s:
        raise HTTPException(404, 'این رمز قبلاً نمایش داده شده یا منقضی شده است')
    store.audit('owner', 'iaas.secret_revealed', None, {'label': s['label']})
    return s


@router.post('/servers/approve-delete/{pid}')
async def server_approve_delete(pid: str, request: Request):
    require(request, csrf=True)
    rate('approve', 20, 60)
    b = await _body(request)
    p = store.get_proposal(pid)
    if not p or p['tool'] != 'delete_server':
        raise HTTPException(404, 'کارت حذف سرور پیدا نشد')
    if str(b.get('confirm_name') or '') != (p['args'] or {}).get('server'):
        raise HTTPException(400, 'برای حذف، نام سرور را دقیقاً تایپ کنید')
    from .. import approvals
    r = await approvals.approve(pid, 'owner', {'confirm_name': b['confirm_name']})
    if isinstance(r, dict) and r.get('error'):
        raise HTTPException(409, r['error'])
    return r


@router.post('/servers/protect')
async def server_protect(request: Request):
    require(request, csrf=True)
    b = await _body(request)
    name, on = str(b.get('server') or ''), bool(b.get('on'))
    pol = store.policy(f'vm:{name}')
    store.set_policy(f'vm:{name}', {**pol, 'write_lock': on})
    store.audit('owner', 'iaas.protect', None, {'server': name, 'on': on})
    return {'server': name, 'protected': on}


@router.get('/server-templates')
async def server_templates(request: Request):
    require(request)
    st = _st()
    return {'templates': await st.with_prices(st.list_all())}


@router.get('/server-templates/{tid}')
async def server_template(tid: str, request: Request):
    require(request)
    st = _st()
    t = await _guarded(st.must, tid)
    return {**(await st.with_prices([t]))[0], 'init_script': t.get('init_script'), 'versions': [] if t['builtin'] else st.versions(tid)}


@router.post('/server-templates')
async def server_template_create(request: Request):
    require(request, csrf=True)
    rate('templates_mut', 60, 60)
    return await _guarded(_st().create, await _body(request), 'owner', 'manual')


@router.post('/server-templates/{tid}')
async def server_template_update(tid: str, request: Request):
    require(request, csrf=True)
    rate('templates_mut', 60, 60)
    return await _guarded(_st().update, tid, await _body(request), 'owner')


@router.post('/server-templates/{tid}/{op}')
async def server_template_op(tid: str, op: str, request: Request):
    require(request, csrf=True)
    rate('templates_mut', 60, 60)
    b = await _body(request)
    st = _st()
    if op == 'clone':
        return await _guarded(st.clone, tid, b.get('name'), 'owner')
    if op == 'restore':
        return await _guarded(st.restore, tid, b.get('version'), 'owner')
    if op == 'delete':
        return await _guarded(st.delete, tid, b.get('confirm_name'), 'owner')
    raise HTTPException(404, 'عملیات نامعتبر')


# ---------------------------------------------------------------- one-time secrets + typed-name approvals (servers, databases, buckets)
@router.post('/secret/{tok}')
async def secret_once(tok: str, request: Request):
    """One-time reveal of a secret produced by an approved card (DB/VM password, storage secret key, backup/file link)."""
    require(request, csrf=True)
    rate('diag_secret', 10, 60)
    from . import iaas
    s = iaas.reveal_secret(tok[:64])
    if not s:
        raise HTTPException(404, 'این مورد قبلاً نمایش داده شده یا منقضی شده است')
    store.audit('owner', 'secret_revealed', None, {'label': s['label']})
    return s


@router.post('/approve-typed/{pid}')
async def approve_typed(pid: str, request: Request):
    """Approve a destructive card only when the owner typed the exact resource name (the LLM can never supply it)."""
    require(request, csrf=True)
    rate('approve', 20, 60)
    from . import TYPED_CONFIRM
    b = await _body(request)
    p = store.get_proposal(pid)
    if not p or p['tool'] not in TYPED_CONFIRM:
        raise HTTPException(404, 'کارت پیدا نشد')
    want = (p['args'] or {}).get(TYPED_CONFIRM[p['tool']])
    if not want or str(b.get('confirm_name') or '') != want:
        raise HTTPException(400, 'برای تأیید، نام را دقیقاً تایپ کنید')
    from .. import approvals
    r = await approvals.approve(pid, 'owner', {'confirm_name': want})
    if isinstance(r, dict) and r.get('error'):
        raise HTTPException(409, r['error'])
    return r


@router.get('/limitations')
async def diag_limitations(request: Request):
    require(request)
    from . import limitations
    return [{'title': t, 'text': x} for t, x in limitations()]


# ---------------------------------------------------------------- databases (DBaaS)
@router.get('/databases')
async def databases_(request: Request):
    require(request)
    return await _run('list_databases', {}, request)


@router.get('/databases/catalog')
async def databases_catalog(request: Request, engine: str = None):
    require(request)
    return await _run('database_plans', {'engine': engine} if engine else {}, request)


@router.get('/databases/{ref}')
async def database_(ref: str, request: Request):
    require(request)
    return await _run('database_details', {'database': ref}, request)


# ---------------------------------------------------------------- object storage
@router.get('/storage')
async def storage_(request: Request):
    require(request)
    return await _run('list_buckets', {}, request)


@router.get('/storage/catalog')
async def storage_catalog(request: Request):
    require(request)
    return await _run('storage_plans', {}, request)


@router.get('/storage/keys')
async def storage_keys(request: Request):
    require(request)
    return await _run('storage_keys', {}, request)


@router.get('/storage/{bucket}/objects')
async def storage_objects(bucket: str, request: Request, prefix: str = '', page: int = 1):
    require(request)
    return await _run('bucket_objects', {'bucket': bucket, 'prefix': prefix[:300], 'limit': 100, 'page': max(1, min(page, 1000))}, request)
