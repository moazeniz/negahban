"""REST API of the template library (/api/templates). GETs need a session; mutations also need the CSRF header.
Validation errors come back as 400 with one Persian message per line in `detail`."""
from fastapi import APIRouter, HTTPException, Request
from . import plans, store
from . import templates as T
from .security import rate, require

router = APIRouter(prefix='/api/templates')


async def _body(request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    if not isinstance(b, dict):
        raise HTTPException(400, 'بدنه‌ی درخواست باید یک شیء JSON باشد')
    return b


def _mut(request):
    require(request, csrf=True)
    rate('templates_mut', 60, 60)


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except T.TemplateError as e:
        raise HTTPException(404 if 'پیدا نشد' in str(e) else 400, str(e))


@router.get('')
async def list_(request: Request, stack: str = None):
    require(request)
    await plans.catalogue()
    return {'templates': T.list_templates(stack),
            'plans': [{'id': p, 'name_fa': plans.plan_sync(p)['name_fa'], 'cpu': plans.plan_sync(p)['cpu'], 'ram': plans.plan_sync(p)['ram'],
                       'hour': plans.hourly(p, 'basic'), 'month': round(plans.hourly(p, 'basic') * 720),
                       'bundles': {b: plans.hourly(p, b) for b in T.BUNDLES}} for p in plans.ORDER]}


@router.post('/validate')
async def validate(request: Request):
    require(request, csrf=True)
    try:
        T.validate(await _body(request))
        return {'ok': True, 'errors': []}
    except T.TemplateError as e:
        return {'ok': False, 'errors': e.errors}


@router.get('/{tid}')
async def get_(tid: str, request: Request, app: str = None):
    require(request)
    await plans.catalogue()
    return _call(T.view, tid, None, app)


@router.post('')
async def create(request: Request):
    _mut(request)
    b = await _body(request)
    return _call(T.create, b, 'owner', 'dashboard', (b.get('note') or 'ساخت قالب')[:200])


@router.put('/{tid}')
async def update(tid: str, request: Request):
    _mut(request)
    b = await _body(request)
    return _call(T.update, tid, b, 'owner', (b.get('note') or 'ویرایش از داشبورد')[:200])


@router.post('/{tid}/clone')
async def clone(tid: str, request: Request):
    _mut(request)
    b = await _body(request)
    return _call(T.clone, tid, (b.get('name') or '').strip() or None, 'owner')


@router.post('/{tid}/restore/{version}')
async def restore(tid: str, version: int, request: Request):
    _mut(request)
    return _call(T.restore, tid, version, 'owner')


@router.delete('/{tid}')
async def delete(tid: str, request: Request):
    _mut(request)
    b = await _body(request)
    return _call(T.delete, tid, b.get('confirm_name'), 'owner')


@router.post('/{tid}/deploy')
async def deploy(tid: str, request: Request):
    """Creates the approval card (nothing runs until the owner approves it). Secret values never travel here."""
    _mut(request)
    from .tools import ToolError, run
    b = await _body(request)
    args = {'template_id': tid, **{k: b[k] for k in ('app', 'plan', 'bundle', 'envs', 'params', 'reason') if b.get(k) not in (None, '', {})}}
    try:
        r = await run('deploy_template', args, {'origin': 'dashboard', 'actor': 'owner', 'why': b.get('reason') or 'استقرار از کتابخانه‌ی قالب‌ها'})
    except (ToolError, T.TemplateError) as e:
        raise HTTPException(400, str(e))
    if r.get('error'):
        raise HTTPException(400, r['error'])
    store.audit('owner', 'template_deploy_request', args.get('app'), {'template': tid, 'proposal': r['proposal']['id']})
    return r


@router.post('/proposals/{pid}/secrets')
async def secrets(pid: str, request: Request):
    """Secret env values typed next to a pending deploy_template card. Kept on the private kv until the executor pops them;
    never written to the proposal row, the template or the audit log (names only)."""
    _mut(request)
    from .tools_templates import SECRET_KV
    p = store.get_proposal(pid)
    if not p or p['tool'] != 'deploy_template' or p['status'] != 'pending':
        raise HTTPException(409, 'این کارت دیگر منتظر تأیید نیست')
    t = _call(T.must, (p['args'] or {}).get('template_id'))
    allowed = {e['name'] for e in t['env'] if e['secret']}
    vals = (await _body(request)).get('values') or {}
    if not isinstance(vals, dict) or set(vals) - allowed:
        raise HTTPException(400, 'فقط متغیرهای محرمانه‌ی همین قالب پذیرفته می‌شوند')
    clean = {k: str(v)[:4000] for k, v in vals.items() if isinstance(v, (str, int, float)) and str(v) != ''}
    for r in store.q('select k from kv where k like ?', (SECRET_KV + '%',)):  # drop values of cards that were rejected/expired meanwhile
        old = store.get_proposal(r['k'][len(SECRET_KV):])
        if not old or old['status'] not in ('pending', 'executing'):
            store.x('delete from kv where k=?', (r['k'],))
    store.kv_set(SECRET_KV + pid, {**(store.kv_get(SECRET_KV + pid, {}) or {}), **clean})
    store.audit('owner', 'template_secrets', p['app'], {'proposal': pid, 'names': sorted(clean)})
    return {'stored': sorted(clean)}
