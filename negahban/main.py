"""FastAPI app: dashboard API, SSE stream, chat, approvals, policies, wizards, scenarios, MCP endpoint."""
import asyncio, json, logging, secrets, time
from pathlib import Path
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from . import agent, approvals, bus, orchestrator, plans, store
from .config import settings
from .liara import LiaraError, liara
from .redact import mask_envs, redact, redact_obj
from .security import COOKIE, CSRF_COOKIE, TTL, check_password, client_ip, make_session, rate, record_hit, require

from . import obs  # noqa: E402
obs.setup()
STATIC = Path(__file__).resolve().parent.parent / 'static'
VERSION = '1.10.0'
app = FastAPI(title='Negahban', docs_url=None, redoc_url=None, openapi_url=None)
STARTED = time.time()
obs.install(app)
from .templates_api import router as templates_router  # noqa: E402
app.include_router(templates_router)
try:
    from .diag.api import router as diag_router  # noqa: E402
    app.include_router(diag_router)
except Exception as _e:  # the diagnostics toolkit is optional; never block the core app
    logging.getLogger('negahban').error('diag api not loaded: %r', _e)

LIMITATIONS = [
    ('دسترسی گیت‌هاب محدود شده در کد', 'توکن گیت‌هاب داده‌شده دسترسی بیشتری از نیاز دارد؛ نگهبان در کد فقط خواندن (کامیت، مقایسه، تگ، ریلیز، اجراهای CI، چک‌ها) و اجرای workflow «Deploy to Liara» با تأیید شما را مجاز می‌کند. هیچ کامیت، push، شاخه یا PR ای در مخزن‌های عمومی ساخته نمی‌شود. ریلیزهایی که پیش از اتصال پایپ‌لاین مستقر شده‌اند اطلاعات git ندارند و «عقب‌تر از main» برایشان نامشخص نمایش داده می‌شود.'),
    ('مقیاس افقی وجود ندارد', 'API لیارا فقط روشن/خاموش (scale 0 یا 1) و تغییر پلن عمودی (resize) دارد؛ چند نسخه‌ی هم‌زمان یا autoscaling داخلی ندارد. نگهبان مقیاس خودکار را با تغییر پلن در بازه‌ی مجاز شما انجام می‌دهد.'),
    ('تغییر پلن قطعی کوتاه دارد', 'هر resize حدود ۲۰ ثانیه خطای 502 ایجاد می‌کند. با باندل نقره‌ای/طلایی و «استقرار بدون قطعی» کمتر می‌شود؛ باندل برنزی این قابلیت را ندارد.'),
    ('وب‌هوک و هشدار ندارد', 'لیارا برای خرابی، کمبود منابع یا شکست استقرار رویداد push نمی‌فرستد؛ نگهبان هر دقیقه وضعیت، لاگ، متریک و رویدادها را می‌خواند.'),
    ('بازگشت نسخه (rollback) رسمی نیست', 'endpoint بازگشت فقط در کنسول استفاده می‌شود و در مستندات عمومی نیست. نگهبان اول آن را امتحان می‌کند و اگر نشد، سورس آرشیوشده‌ی نسخه‌ی قبلی را دوباره مستقر می‌کند.'),
    ('DNS بیرون از لیارا', 'رکوردهای دامنه در ایرنیک (nic.ir)، آروان‌کلاد یا کلادفلر با API لیارا قابل ویرایش نیست؛ نگهبان دقیقاً می‌گوید چه رکوردی را کجا ثبت کنید و وضعیت را بررسی می‌کند.'),
    ('تغییر متغیر محیطی = ری‌استارت', 'API همه‌ی متغیرها را یکجا جایگزین می‌کند و برنامه را دوباره راه‌اندازی می‌کند (یک ریلیز ENV_UPDATE).'),
    ('سهمیه‌ی استقرار', 'باندل برنزی ۲۰، نقره‌ای ۵۰ استقرار در روز؛ نگهبان استقرارهای خودکار را با دوره‌ی خنک‌سازی محدود می‌کند.'),
    ('شِل کانتینر از ایران مسدود است', 'دروازه‌ی exec لیارا اتصال از شبکه‌های ایران (حتی خود لیارا) را رد می‌کند؛ ترمینال نگهبان از طریق رله‌ی تیم بیرون از ایران کار می‌کند. فرمان‌های فقط‌خواندنی خودکار اجرا می‌شوند، بقیه کارت تأیید می‌خواهند و فرمان‌های مخرب همیشه رد می‌شوند. اصلاح کد فقط برای برنامه‌هایی ممکن است که سورسشان در فضای کاری نگهبان است.'),
    ('سرور مجازی: حداقل ۲۴ ساعت', 'لیارا تا ۲۴ ساعت پس از ساخت سرور مجازی اجازه‌ی حذف نمی‌دهد؛ کارت ساخت، حداقل هزینه را نشان می‌دهد. رمز root هرگز در رابط یا مدل زبانی نمایش داده نمی‌شود و رمز جدید فقط یک بار نشان داده می‌شود.'),
    ('قالب بررسی سلامت (healthCheck)', 'مستند نشده؛ آزمایش‌شده: command باید آرایه باشد و زمان‌ها بر حسب ثانیه‌اند (startPeriod حداکثر ۳۰۰۰).'),
    ('توکن با دسترسی محدود وجود ندارد', 'توکن API لیارا به کل تیم دسترسی دارد؛ نگهبان آن را فقط سمت سرور نگه می‌دارد و قفل نوشتن برای هر برنامه را خودش اعمال می‌کند.'),
    ('واحد متریک‌ها', 'لیارا واحد متریک‌ها را مستند نکرده؛ CPU به هسته و حافظه به بایت تفسیر شده و با سقف پلن مقایسه می‌شود.'),
    ('هزینه‌ی تا این لحظه', 'API «هزینه‌ی مصرف‌شده‌ی ماه» ندارد؛ نگهبان نرخ ساعتی و موجودی را نشان می‌دهد.'),
]


@app.middleware('http')
async def headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'same-origin'
    resp.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'"
    return resp


@app.exception_handler(LiaraError)  # more specific than the global handler installed by obs
async def liara_err(request, e: LiaraError):
    return JSONResponse({'detail': e.fa()}, status_code=502 if e.status >= 500 or e.status == 0 else 400)


def bootstrap_registry():
    """Discover team apps once; owner decides what to monitor. Sensible defaults from the brief."""
    if store.kv_get('bootstrapped_v1'):
        return
    store.upsert_app(settings.demo_app, 'demo', None, '/', 8080)
    store.set_policy(settings.demo_app, {'autopilot': True, 'resize': True, 'min_plan': 'small-g2', 'max_plan': 'medium-g2', 'max_toman_day': 60000,
                                         'scale_down': True, 'restart_on_crash': True, 'auto_fix_code': False, 'auto_rollback': False})
    store.kv_set('bootstrapped_v1', True)


async def discover():
    try:
        team = await liara.list_apps()
    except LiaraError:
        return
    reg = {a['name'] for a in store.apps()}
    alive = {p['project_id'] for p in team}
    for name in reg - alive:  # app deleted on Liara: drop it from the registry (and its policy) so the name can be reused
        a = store.get_app(name)
        if a and a['role'] not in ('demo', 'self'):
            store.remove_app(name); store.x('delete from policies where app=?', (name,))
            store.audit('negahban', 'app_unregistered', name, {'reason': 'no longer exists on Liara'})
    reg &= alive
    for p in team:
        n = p['project_id']
        if n in reg:
            continue
        if __import__('re').fullmatch(r'demo-shop-r\d+', n):  # replicas belong to the deterministic replica controller (meow-lb)
            store.upsert_app(n, 'replica', None, '/healthz', 8080, {'owner': 'meow-lb'})
            store.set_policy(n, {'write_lock': True, 'env_lock': True})
        elif n == 'meow-lb':  # platform load balancer / replica controller: monitored, never remediated by Negahban
            store.upsert_app(n, 'monitored', None, '/lb/status', 8080, {'platform': True, 'note': 'deterministic replica controller (owned by the platform)'})
            store.set_policy(n, {'write_lock': True, 'env_lock': True})
        elif n == settings.self_app:
            store.upsert_app(n, 'self', None, '/healthz', 8080)
            store.set_policy(n, {'write_lock': True, 'env_lock': True})
        elif 'wallex' in n or 'trade' in n:
            store.upsert_app(n, 'monitored', None, '/', 8080, {'note': 'real-money trading: monitor only'})
            store.set_policy(n, {'write_lock': True, 'env_lock': True})
        elif n == 'bazargard-ai':
            store.upsert_app(n, 'monitored', None, '/api/ready', 8080)
            store.set_policy(n, {'autopilot': True, 'resize': True, 'min_plan': 'standard-base-g2', 'max_plan': 'standard-plus-g2', 'max_toman_day': 140000,
                                 'scale_down': True, 'down_minutes': 30, 'restart_on_crash': False, 'env_lock': True})
        else:
            store.upsert_app(n, 'off', None, '/', 8080)
            store.set_policy(n, {'write_lock': True})


@app.on_event('startup')
async def startup():
    store.init()
    bootstrap_registry()
    await discover()
    from . import control, jobs
    # every background loop is supervised: a crash is logged (app.crash) and the loop restarts with backoff (loop.restart)
    asyncio.create_task(control.supervise('jobs', jobs.worker))
    if settings.orchestrator_enabled:
        asyncio.create_task(control.supervise('orchestrator', orchestrator.loop))
    asyncio.create_task(control.supervise('discovery', _discover_loop))
    asyncio.create_task(control.supervise('self_check', control.self_check))
    asyncio.create_task(control.supervise('snapshots', control.snapshot_loop))


async def _discover_loop():
    while True:
        await asyncio.sleep(600)
        await discover()


# ------------------------------------------------------------------ public
@app.get('/healthz')
async def healthz():
    o = orchestrator.STATE
    from . import control
    if control.STATE['restart_requested']:  # unrecoverable resource pressure: let the external watchdog restart us
        return JSONResponse({'ok': False, 'reason': 'restart requested by self-check', 'version': VERSION}, status_code=503)
    return {'ok': True, 'version': VERSION, 'ai_enabled': control.ai_enabled(), 'uptime_s': int(time.time() - STARTED), 'orchestrator_last_cycle_s': int(time.time() - o['last_cycle']) if o['last_cycle'] else None}


def _asset_version():
    import hashlib
    h = hashlib.sha1()
    for f in ('app.js', 'style.css', 'charts.js', 'templates.js', 'diag.js', 'servers.js', 'data.js', 'extras.js'):
        p = STATIC / f
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


ASSET_V = _asset_version()


@app.get('/')
async def index():
    from fastapi.responses import HTMLResponse
    html = (STATIC / 'index.html').read_text(encoding='utf-8').replace('?v=1', f'?v={ASSET_V}')
    return HTMLResponse(html, headers={'Cache-Control': 'no-cache, no-store, must-revalidate'})


app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.post('/api/login')
async def login(request: Request):
    ip = client_ip(request)
    rate(f'login:{ip}', 6, 300, record=False)  # only FAILED attempts count towards the lock-out
    rate('login:all', 40, 300, record=False)
    body = await request.json()
    if not check_password(body.get('password')):
        record_hit(f'login:{ip}'); record_hit('login:all')
        store.audit(f'ip:{ip}', 'login_failed', None, {}, ok=False)
        await asyncio.sleep(0.8)
        raise HTTPException(401, 'رمز عبور اشتباه است')
    tok, sid = make_session()
    csrf = secrets.token_urlsafe(24)
    store.audit('owner', 'login', None, {'ip': ip})
    r = JSONResponse({'ok': True, 'csrf': csrf})
    r.set_cookie(COOKIE, tok, max_age=TTL, httponly=True, secure=settings.secure_cookies, samesite='strict')
    r.set_cookie(CSRF_COOKIE, csrf, max_age=TTL, httponly=False, secure=settings.secure_cookies, samesite='strict')
    return r


@app.post('/api/logout')
async def logout(request: Request):
    r = JSONResponse({'ok': True})
    r.delete_cookie(COOKIE); r.delete_cookie(CSRF_COOKIE)
    return r


@app.get('/api/me')
async def me(request: Request):
    sid = require(request)
    from .llm import llm
    i = llm.info()
    return {'ok': True, 'abuse': agent.abuse_state(sid), 'version': VERSION, 'provider': i['provider'], 'model': i['model'], 'demo_app': settings.demo_app}


# ------------------------------------------------------------------ overview
def _app_card(a):
    st = orchestrator.STATE['apps'].get(a['name'], {}).get('last') or {}
    rows = store.samples(a['name'], 3600)
    step = max(1, len(rows) // 60)
    pol = store.policy(a['name'])
    return {**a, 'policy': pol, 'last': st,
            'spark': {'cpu': [round(r['cpu'] / r['cpu_limit'], 3) if r['cpu_limit'] else 0 for r in rows[::step]],
                      'mem': [round(r['mem'] / r['mem_limit'], 3) if r['mem_limit'] else 0 for r in rows[::step]],
                      'lat': [r['latency_ms'] or 0 for r in rows[::step]], 'ts': [int(r['ts']) for r in rows[::step]]},
            'incidents_open': len(store.incidents(10, a['name'], open_only=True))}


@app.get('/api/overview')
async def overview(request: Request):
    require(request)
    await plans.catalogue()
    team = {}
    try:
        team = {p['project_id']: p for p in await liara.list_apps()}
    except LiaraError:
        pass
    cards = []
    for a in store.apps():
        c = _app_card(a)
        p = team.get(a['name'], {})
        c['liara'] = {'plan': p.get('planID'), 'bundle': p.get('bundlePlanID'), 'status': p.get('status'), 'scale': p.get('scale'),
                      'toman_h': plans.hourly(p.get('planID'), p.get('bundlePlanID') or 'basic') if p.get('planID') else None, 'exists': bool(p)}
        cards.append(c)
    unregistered = [n for n in team if not store.get_app(n)]
    burn = sum(c['liara']['toman_h'] or 0 for c in cards if c['liara'].get('scale'))
    return {'apps': cards, 'unregistered': unregistered, 'incidents': store.incidents(30), 'pending': [approvals.public(p) for p in store.proposals('pending', 30)],
            'orchestrator': {k: v for k, v in orchestrator.STATE.items() if k != 'apps'}, 'burn_toman_h': round(burn), 'watchdog': store.kv_get('watchdog_ping'),
            'llm': __import__('negahban.llm', fromlist=['llm']).llm.info()}


@app.get('/api/apps/{name}')
async def app_detail(name: str, request: Request):
    require(request)
    a = store.get_app(name)
    if not a:
        raise HTTPException(404, 'برنامه ثبت نشده است')
    info, rels = await asyncio.gather(liara.app(name), liara.releases(name, 10))
    from . import workspace as ws
    commits = []
    if a['role'] == 'demo':
        try:
            ws.ensure(name); commits = ws.log(name, 10)
        except Exception:
            pass
    return {'app': _app_card(a), 'liara': {k: info.get(k) for k in ('planID', 'bundlePlanID', 'status', 'scale', 'zeroDowntime', 'hourlyPrice', 'bundleHourlyPrice', 'created_at', 'defaultSubdomain')},
            'envs': mask_envs(info.get('envs')), 'releases': rels.get('releases', []), 'current_release': rels.get('currentRelease'),
            'archives': store.archives(name, 15), 'commits': commits, 'incidents': store.incidents(20, name), 'audit': store.audit_list(40, name)}


@app.get('/api/apps/{name}/logs')
async def app_logs(name: str, request: Request, minutes: int = 15, search: str = ''):
    require(request)
    if not store.get_app(name):
        raise HTTPException(404)
    lines = await liara.logs(name, start=time.time() - 60 * max(1, min(minutes, 1440)), search=search or None)
    from .sanitize import clean
    return [{'ts': l['ts'], 'type': l['type'], 'line': redact(clean(l['line'], 400))} for l in lines[-300:]]


@app.get('/api/apps/{name}/metrics')
async def app_metrics(name: str, request: Request, minutes: int = 60):
    require(request)
    since = time.time() - 60 * max(5, min(minutes, 10080))
    info, cpu, mem = await asyncio.gather(liara.app(name), liara.metrics(name, 'cpu', since), liara.metrics(name, 'memory', since))
    pl = plans.plan_sync(info.get('planID'))
    return {'plan': pl, 'cpu': cpu, 'mem': [(t, v / 1024 ** 3) for t, v in mem]}


@app.get('/api/apps/{name}/full')
async def app_full(name: str, request: Request, hours: int = 6):
    """Everything about one app for the detail page (events, network, disks, domains, what is running)."""
    require(request)
    from .tools import running_info
    since = time.time() - 3600 * max(1, min(hours, 168))
    rx, tx, cpu, mem, evs, run = await asyncio.gather(liara.metrics(name, 'network-receive', since), liara.metrics(name, 'network-transmit', since),
                                                      liara.metrics(name, 'cpu', since), liara.metrics(name, 'memory', since),
                                                      liara.events(name, start=time.time() - 3 * 86400), running_info({'app': name}, {}), return_exceptions=True)
    ok = lambda v, d: d if isinstance(v, Exception) else v
    info = await liara.app(name)
    pl = plans.plan_sync(info.get('planID'))
    return {'plan': pl, 'cpu': ok(cpu, []), 'mem': [(t, v / 1024 ** 3) for t, v in ok(mem, [])], 'rx': [(t, v / 1024) for t, v in ok(rx, [])], 'tx': [(t, v / 1024) for t, v in ok(tx, [])],
            'events': list(reversed(ok(evs, [])))[:80], 'running': redact_obj(ok(run, {}))}


@app.get('/api/releases/{rid}/build-logs')
async def release_build_logs(rid: str, request: Request):
    require(request)
    from .analysis import parse_build_logs
    from .sanitize import clean
    lines, rel = await liara.build_log_lines(rid)
    return {'release': {k: rel.get(k) for k in ('state', 'failReason')}, 'lines': [redact(clean(l, 600)) for l in lines][-400:], 'summary': parse_build_logs(lines)}


@app.get('/api/team')
async def team(request: Request):
    require(request)
    from .tools import team_usage
    return await team_usage({}, {})


@app.post('/api/apps/{name}/register')
async def register(name: str, request: Request):
    require(request, csrf=True)
    body = await request.json()
    role = body.get('role', 'monitored')
    if role not in ('monitored', 'off', 'demo'):
        raise HTTPException(400)
    a = store.get_app(name)
    store.upsert_app(name, role, body.get('url') or (a or {}).get('url'), body.get('health_path') or (a or {}).get('health_path') or '/', (a or {}).get('port') or 8080)
    store.audit('owner', 'register_app', name, {'role': role})
    return {'ok': True}


@app.post('/api/apps/{name}/diagnose')
async def run_diag(name: str, request: Request):
    require(request, csrf=True)
    rate('diag', 10, 60)
    from .diagnose import diagnose
    d = await diagnose(name, 20, with_llm=True)
    store.audit('owner', 'diagnose', name, {'top': d['hypotheses'][0]['title'] if d['hypotheses'] else 'healthy'})
    return redact_obj(d)


# ------------------------------------------------------------------ incidents / proposals / audit / policies
@app.get('/api/incidents')
async def incidents(request: Request):
    require(request)
    return store.incidents(60)


@app.get('/api/incidents/{iid}')
async def incident(iid: str, request: Request):
    require(request)
    i = store.get_incident(iid)
    if not i:
        raise HTTPException(404)
    i['proposals'] = [approvals.public(p) for p in store.q('select * from proposals where incident_id=? order by created_at', (iid,))]
    return i


@app.post('/api/incidents/{iid}/resolve')
async def resolve(iid: str, request: Request):
    require(request, csrf=True)
    store.audit('owner', 'incident_resolve', None, {'incident': iid})
    i = store.update_incident(iid, status='resolved', note='مالک رخداد را بست.')
    bus.publish('incident', incident=i)
    return i


@app.post('/api/incidents/{iid}/act')
async def incident_act(iid: str, request: Request):
    """Turn one of the diagnosis fixes into an approval card."""
    require(request, csrf=True)
    body = await request.json()
    i = store.get_incident(iid)
    h = next((h for h in (i or {}).get('diagnosis', {}).get('hypotheses', []) if h['id'] == body.get('hypothesis')), None)
    if not h or body.get('fix', 0) >= len(h['fixes']):
        raise HTTPException(404)
    f = h['fixes'][body.get('fix', 0)]
    from .tools import run
    return await run(f['tool'], dict(f['args']), {'origin': 'dashboard', 'actor': 'owner', 'incident_id': iid, 'why': h['title']})


@app.get('/api/proposals')
async def proposals(request: Request, status: str = None):
    require(request)
    return [approvals.public(p) for p in store.proposals(status, 80)]


@app.post('/api/proposals/{pid}/approve')
async def approve(pid: str, request: Request):
    require(request, csrf=True)
    rate('approve', 20, 60)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    p = store.get_proposal(pid)
    if p and p['tool'] in ('delete_app', 'delete_server', 'delete_database', 'restore_database_backup', 'delete_bucket') and body.get('confirm_name') != ((p['args'] or {}).get('app') or (p['args'] or {}).get('server') or (p['args'] or {}).get('database') or (p['args'] or {}).get('bucket')):
        raise HTTPException(400, 'برای حذف، نام را دقیقاً تایپ کنید')
    extra = {}
    if p and p['tool'] in ('delete_app', 'delete_server', 'delete_database', 'restore_database_backup', 'delete_bucket'):
        extra['confirm_name'] = body.get('confirm_name')
    if p and p['tool'] == 'launch_project' and body.get('envs'):
        from .onboarding import stash_secrets
        extra['env_ref'] = stash_secrets(body['envs'])
    if p and p['tool'] == 'set_env' and body.get('value'):
        extra['value'] = str(body['value'])[:4000]
    r = await approvals.approve(pid, 'owner', extra)
    if isinstance(r, dict) and r.get('error'):
        raise HTTPException(409, r['error'])
    return r


@app.post('/api/proposals/{pid}/reject')
async def reject(pid: str, request: Request):
    require(request, csrf=True)
    r = await approvals.reject(pid, 'owner')
    if r.get('error'):
        raise HTTPException(409, r['error'])
    return r


@app.get('/api/audit')
async def audit(request: Request, app_name: str = None):
    require(request)
    return store.audit_list(300, app_name)


@app.get('/api/policies')
async def policies(request: Request):
    require(request)
    return {a['name']: {**store.policy(a['name']), 'role': a['role']} for a in store.apps()}


@app.put('/api/policies/{name}')
async def set_policy(name: str, request: Request):
    require(request, csrf=True)
    body = await request.json()
    old = store.policy(name)
    new = {k: body[k] for k in store.DEFAULT_POLICY if k in body}
    for k in ('min_plan', 'max_plan'):
        if k in new and new[k] not in plans.ORDER:
            raise HTTPException(400, 'پلن نامعتبر')
    if 'min_plan' in new or 'max_plan' in new:
        lo, hi = new.get('min_plan', old['min_plan']), new.get('max_plan', old['max_plan'])
        if plans.ORDER.index(lo) > plans.ORDER.index(hi):
            raise HTTPException(400, 'حداقل پلن نباید از حداکثر بزرگ‌تر باشد')
    for k in ('max_toman_day', 'up_cpu', 'up_mem', 'up_minutes', 'down_cpu', 'down_mem', 'down_minutes'):
        if k in new:
            new[k] = float(new[k]) if k in ('up_cpu', 'up_mem', 'down_cpu', 'down_mem') else int(new[k])
    if name == settings.self_app:
        new['write_lock'] = True
    store.set_policy(name, {**old, **new})
    store.audit('owner', 'policy_update', name, {'changes': {k: v for k, v in new.items() if old.get(k) != v}})
    bus.publish('policy', app=name, policy=store.policy(name))
    return store.policy(name)


@app.get('/api/plans')
async def get_plans(request: Request):
    require(request)
    c = await plans.catalogue()
    return {'order': plans.ORDER, 'plans': c['plans'], 'bundles': c['bundles'], 'live': c.get('live'), 'limits': c.get('limits')}


@app.get('/api/llm')
async def llm_info(request: Request):
    require(request)
    from .llm import llm
    return llm.info()


@app.put('/api/llm')
async def llm_set(request: Request):
    """Runtime provider switch (stored in the DB, survives restarts; the env LLM_PROVIDER is only the default)."""
    require(request, csrf=True)
    from .llm import llm
    body = await request.json()
    p = body.get('provider')
    if p not in ('anthropic', 'boxapi'):
        raise HTTPException(400, 'provider must be anthropic or boxapi')
    store.kv_set('llm_provider', p)
    store.audit('owner', 'llm_provider', None, {'provider': p})
    bus.publish('llm', info=llm.info())
    return llm.info()


@app.get('/api/limitations')
async def limitations(request: Request):
    require(request)
    try:
        from .diag import limitations as _dl
        extra = list(_dl())
    except Exception:
        extra = []
    return [{'title': t, 'text': x} for t, x in LIMITATIONS + extra]


# ------------------------------------------------------------------ chat + stream
@app.post('/api/chat')
async def chat(request: Request):
    sid = require(request, csrf=True)
    rate(f'chat:{sid}', 12, 60)
    body = await request.json()
    session = str(body.get('session') or 'default')[:40]
    text = str(body.get('message') or '')[:4000]
    if agent.abuse_state(sid)['blocked']:
        raise HTTPException(423, 'این گفتگو به‌دلیل درخواست‌های خارج از وظیفه یا تلاش برای دور زدن قوانین بسته شده است.')
    from . import control
    if control.CHAT_WAITING['n'] >= control.MAX_CHAT_WAITING:  # back-pressure: chat can never starve monitoring
        raise HTTPException(429, 'نگهبان مشغول است؛ چند ثانیه‌ی دیگر دوباره بپرسید.')

    async def run():
        control.CHAT_WAITING['n'] += 1
        try:
            async with control.CHAT_SLOTS:
                await asyncio.wait_for(agent.chat(session, sid, text), 300)
        except asyncio.TimeoutError:
            bus.publish('chat_done', session=session, text='پاسخ بیش از حد طول کشید؛ دوباره امتحان کنید.')
        finally:
            control.CHAT_WAITING['n'] -= 1
    asyncio.create_task(run())
    return {'ok': True, 'session': session}


@app.get('/api/chat/{session}')
async def chat_hist(session: str, request: Request):
    require(request)
    return store.chat_history(session[:40], 80)


@app.post('/api/chat/unblock')
async def unblock(request: Request):
    sid = require(request, csrf=True)
    body = await request.json()
    if not check_password(body.get('password')):
        raise HTTPException(401, 'رمز عبور اشتباه است')
    store.kv_set(f'abuse:{sid}', {'score': 0, 'blocked': False, 'strikes': []})
    store.audit('owner', 'chat_unblock', None, {})
    return {'ok': True}


@app.get('/api/stream')
async def stream(request: Request):
    require(request)
    q = bus.subscribe()

    async def gen():
        try:
            yield 'retry: 3000\n:' + ' ' * 2048 + '\n\n'  # padding defeats proxy buffering
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), 12)
                    yield f'data: {msg}\n\n'
                except asyncio.TimeoutError:
                    yield ': ping\n\n'
        finally:
            bus.unsubscribe(q)
    return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ------------------------------------------------------------------ scenarios
@app.get('/api/scenarios')
async def scenarios(request: Request):
    require(request)
    from .scenarios import SCENARIOS, load_status
    return {'scenarios': SCENARIOS, 'load': load_status(), 'app': settings.demo_app}


@app.post('/api/scenarios/{name}')
async def scenario(name: str, request: Request):
    require(request, csrf=True)
    rate('scenario', 6, 60)
    from .scenarios import trigger
    try:
        return await trigger(name, 'owner')
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ new project wizard + domains
@app.post('/api/projects/analyze')
async def analyze(request: Request, file: UploadFile = File(None)):
    require(request, csrf=True)
    rate('analyze', 6, 60)
    from . import analyzer
    try:
        if file is not None:
            data = await file.read()
            return await analyzer.analyze_zip(data)
        raise HTTPException(400, 'فایل zip یا آدرس git لازم است')
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post('/api/projects/analyze-git')
async def analyze_git(request: Request):
    require(request, csrf=True)
    rate('analyze', 6, 60)
    from . import analyzer
    body = await request.json()
    try:
        return await analyzer.analyze_git(str(body.get('git_url', '')))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post('/api/projects/propose')
async def project_propose(request: Request):
    """Wizard step: create-app proposal (then a deploy proposal once the app exists)."""
    require(request, csrf=True)
    body = await request.json()
    from .tools import run
    if body.get('step') == 'deploy':
        return await run('deploy_project', {'app': body['name'], 'project_id': body['project_id']}, {'origin': 'wizard', 'actor': 'owner'})
    return await run('create_app', {'name': body['name'], 'plan': body['plan'], 'bundle': body.get('bundle') or 'basic',
                                    'platform': body.get('platform') or 'docker'}, {'origin': 'wizard', 'actor': 'owner'})


@app.post('/api/domains/propose')
async def domain_propose(request: Request):
    require(request, csrf=True)
    body = await request.json()
    from .tools import run
    return await run('add_domain', {'app': body['app'], 'domain': str(body['domain']).lower().strip()}, {'origin': 'wizard', 'actor': 'owner'})


@app.get('/api/domains')
async def domains(request: Request, app_name: str = None):
    require(request)
    from .domains import records_for
    ds = await liara.domains(app_name) if app_name else await liara.domains()
    return [{**{k: d.get(k) for k in ('_id', 'name', 'status', 'certificatesStatus', 'project')}, 'records': records_for(d)} for d in ds]


@app.get('/api/domains/{did}')
async def domain_guide(did: str, request: Request):
    require(request)
    from .domains import guide
    return await guide(did)


# ------------------------------------------------------------------ watchdog + MCP
@app.post('/api/watchdog/ping')
async def watchdog_ping(request: Request):
    tok = request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    if not settings.watchdog_token or not secrets.compare_digest(tok, settings.watchdog_token):
        raise HTTPException(401)
    body = await request.json()
    store.kv_set('watchdog_ping', {'ts': time.time(), 'host': str(body.get('host', ''))[:60], 'note': str(body.get('note', ''))[:200]})
    return {'ok': True}


# ------------------------------------------------------------------ control plane, LLM usage, reports, snapshots, prefs, setup
@app.get('/api/control')
async def control_status(request: Request):
    require(request)
    from . import control
    return control.status()


@app.put('/api/control/ai')
async def control_ai(request: Request):
    require(request, csrf=True)
    from . import control
    b = await request.json()
    control.set_ai(bool(b.get('enabled')), 'owner')
    bus.publish('control', ai_enabled=control.ai_enabled())
    return control.status()


@app.get('/api/llm/usage')
async def llm_usage(request: Request, days: int = 7):
    require(request)
    from .llm import usage_rows
    rows = usage_rows(max(1, min(days, 60)))
    days_ = {}
    for r in rows:
        d = days_.setdefault(r['day'], {'day': r['day'], 'calls': 0, 'tokens_in': 0, 'tokens_out': 0, 'cache_read': 0, 'cache_write': 0, 'cost_usd': 0.0, 'by_feature': {}})
        for k in ('calls', 'tokens_in', 'tokens_out', 'cache_read', 'cache_write', 'cost_usd'):
            d[k] += r[k]
        f = d['by_feature'].setdefault(r['feature'], {'calls': 0, 'cost_usd': 0.0})
        f['calls'] += r['calls']; f['cost_usd'] += r['cost_usd']
    for d in days_.values():
        tin = d['tokens_in'] + d['cache_read'] + d['cache_write']
        d['cache_hit_pct'] = round(100 * d['cache_read'] / tin, 1) if tin else None
        d['cost_usd'] = round(d['cost_usd'], 5)
    return {'days': sorted(days_.values(), key=lambda x: x['day'], reverse=True), 'rows': rows}


@app.put('/api/llm/cache')
async def llm_cache(request: Request):
    require(request, csrf=True)
    b = await request.json()
    store.kv_set('prompt_cache', bool(b.get('enabled')))
    store.audit('owner', 'prompt_cache', None, {'enabled': bool(b.get('enabled'))})
    return {'prompt_cache': bool(b.get('enabled'))}


@app.get('/api/reports')
async def reports_list(request: Request):
    require(request)
    from . import reports
    return {'reports': reports.listing(), 'report_hour': store.kv_get('report_hour', 8)}


@app.get('/api/reports/{rid}')
async def reports_get(rid: str, request: Request):
    require(request)
    from . import reports
    r = reports.get(None if rid == 'latest' else rid)
    if not r:
        raise HTTPException(404)
    return r


@app.post('/api/reports/generate')
async def reports_generate(request: Request):
    require(request, csrf=True)
    rate('report', 4, 60)
    from . import reports
    return await reports.build(origin='owner')


@app.put('/api/reports/hour')
async def reports_hour(request: Request):
    require(request, csrf=True)
    b = await request.json()
    h = int(b.get('hour', 8))
    if not 0 <= h <= 23:
        raise HTTPException(400)
    store.kv_set('report_hour', h)
    return {'report_hour': h}


@app.get('/api/snapshots')
async def snapshots_list(request: Request):
    require(request)
    from . import control
    return {'last': store.kv_get('snapshot_last'), 'snapshots': control.snapshots()}


@app.post('/api/snapshots')
async def snapshots_now(request: Request):
    require(request, csrf=True)
    from . import control
    p = await asyncio.to_thread(control.snapshot, 'manual')
    store.audit('owner', 'snapshot', None, {'file': p.name})
    return {'file': p.name}


@app.get('/api/snapshots/latest')
async def snapshots_latest(request: Request):
    """Off-site pull (Hetzner timer): read-only, Bearer STATUS_INGEST_TOKEN."""
    tok = request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    want = __import__('os').environ.get('STATUS_INGEST_TOKEN', '')
    if not want or not secrets.compare_digest(tok, want):
        raise HTTPException(401)
    from . import control
    snaps = control.snapshots()
    if not snaps:
        raise HTTPException(404, 'no snapshot yet')
    return FileResponse(control.SNAP_DIR() / snaps[0]['file'], media_type='application/gzip', filename=snaps[0]['file'])


@app.get('/api/prefs')
async def prefs_get(request: Request):
    require(request)
    return store.kv_get('owner_prefs', {})


@app.put('/api/prefs')
async def prefs_put(request: Request):
    require(request, csrf=True)
    b = await request.json()
    cur = store.kv_get('owner_prefs', {})
    cur.update({k: v for k, v in b.items() if k in ('tour', 'tour_ts')})
    store.kv_set('owner_prefs', cur)
    return cur


@app.post('/api/projects/launch')
async def project_launch(request: Request):
    """Wizard: one approval card for create+deploy; secret env values are parked privately (never in the card)."""
    require(request, csrf=True)
    b = await request.json()
    from .onboarding import stash_secrets
    from .tools import run
    args = {'project_id': str(b.get('project_id', '')), 'app': str(b.get('app', '')).strip().lower(), 'plan': b.get('plan') or 'small-g2',
            'bundle': b.get('bundle') or 'basic', 'envs': {str(k): str(v) for k, v in (b.get('envs') or {}).items() if v not in (None, '')}}
    ref = stash_secrets(b.get('secrets') or {})
    if ref:
        args['secret_ref'] = ref
    return await run('launch_project', args, {'origin': 'wizard', 'actor': 'owner', 'why': 'راه‌اندازی اولیه از ویزارد'})


@app.get('/api/scaler')
async def scaler_view(request: Request):
    require(request)
    from .tools_scaler import status
    return await status()


# ------------------------------------------------------------------ GitOps (read-only views; releases go through approval cards)
@app.get('/api/gitops')
async def gitops_view(request: Request, app_name: str = '', fresh: int = 0):
    require(request)
    from . import gitops
    if not gitops.gh.configured:
        return {'configured': False, 'apps': {}}
    names = [app_name] if app_name in gitops.APP_REPO else list(gitops.APP_REPO)

    async def one(n):
        try:
            return n, await gitops.overview(n, fresh=bool(fresh))
        except Exception as e:
            return n, {'error': redact(str(e))[:200]}
    res = await asyncio.gather(*(one(n) for n in names))
    return {'configured': True, 'owner': gitops.OWNER, 'apps': dict(res), 'policy': 'read-only + deploy workflow dispatch (approval card); no commits/pushes/PRs'}


@app.get('/api/gitops/{name}/changes')
async def gitops_changes(name: str, request: Request, base: str = '', head: str = ''):
    require(request)
    from . import gitops
    import re as _re
    for v in (base, head):
        if v and not _re.match(r'^[A-Za-z0-9._/-]{1,100}$', v):
            raise HTTPException(400, 'bad ref')
    try:
        return await gitops.what_changed(name, base or None, head or None)
    except Exception as e:
        raise HTTPException(502, redact(str(e))[:200])


@app.post('/api/gitops/{name}/release')
async def gitops_release(name: str, request: Request):
    """Dashboard button: creates the «release via pipeline» approval card (nothing is dispatched until the owner approves it)."""
    require(request, csrf=True)
    b = await request.json()
    from .tools import ToolError, run
    args = {'app': name, 'ref': str(b.get('ref') or 'main'), 'dry_run': bool(b.get('dry_run', False))}
    try:
        return await run('release_via_pipeline', args, {'origin': 'dashboard', 'actor': 'owner', 'why': 'درخواست انتشار از داشبورد (کارت CI/CD)'})
    except ToolError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ alert rules, notifications, push ingest, self-guardrails
@app.get('/api/alerts/rules')
async def alert_rules(request: Request):
    require(request)
    from . import alerts
    return {'rules': alerts.rules(), 'metrics': alerts.METRICS, 'allowed_autopilot': sorted(alerts.ALLOWED_AUTOPILOT)}


@app.post('/api/alerts/rules')
async def alert_rule_save(request: Request):
    require(request, csrf=True)
    from . import alerts
    try:
        return alerts.save_rule(await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete('/api/alerts/rules/{rid}')
async def alert_rule_delete(rid: str, request: Request):
    require(request, csrf=True)
    from . import alerts
    alerts.delete_rule(rid)
    return {'ok': True}


@app.get('/api/alerts/rules/{rid}/versions')
async def alert_rule_versions(rid: str, request: Request):
    require(request)
    from . import alerts
    return alerts.versions(rid)


@app.post('/api/alerts/test-rule')
async def alert_rule_test(request: Request):
    """Dry-run a log-pattern rule against sample lines (simulated inputs), without firing."""
    require(request, csrf=True)
    from . import alerts
    b = await request.json()
    return {'matches': [l for l in (b.get('lines') or [])[:200] if alerts.match_line(str(b.get('pattern', '')), str(l))]}


@app.get('/api/notifications')
async def notifications(request: Request, status: str = None):
    require(request)
    from . import alerts
    return alerts.listing(status, 100)


@app.post('/api/notifications/{nid}/{action}')
async def notification_action(nid: str, action: str, request: Request):
    require(request, csrf=True)
    from . import alerts
    if action not in ('ack', 'resolve'):
        raise HTTPException(400)
    alerts.set_status(nid, 'acked' if action == 'ack' else 'resolved')
    return {'ok': True}


@app.get('/api/alerts/channels')
async def alert_channels(request: Request):
    require(request)
    from . import alerts
    return alerts.channels()


@app.put('/api/alerts/channels')
async def alert_channels_put(request: Request):
    require(request, csrf=True)
    from . import alerts
    return alerts.set_channels(await request.json())


@app.post('/api/alerts/channels/{ch}/test')
async def alert_channel_test(ch: str, request: Request):
    require(request, csrf=True)
    from . import alerts
    n = {'id': 'test', 'ts': time.time(), 'app': '_test', 'severity': 'info', 'title': 'آزمایش اعلان نگهبان', 'body': 'اگر این پیام را می‌بینید، کانال درست تنظیم شده است.', 'link': '#alerts'}
    if ch == 'dashboard':
        bus.publish('notify', notification={**n, 'status': 'test'})
        return {'dashboard': 'sent'}
    res = await alerts.deliver(n, only=ch)
    return res or {ch: 'این کانال هنوز تنظیم نشده است'}


@app.get('/api/alerts/ingest-token/{app_name}')
async def ingest_token_get(app_name: str, request: Request, rotate: bool = False):
    require(request)
    from . import alerts
    if not store.get_app(app_name):
        raise HTTPException(404)
    return {'app': app_name, 'token': alerts.ingest_token(app_name, rotate), 'endpoint': '/api/ingest/event'}


@app.post('/api/ingest/event')
async def ingest_event(request: Request):
    """Push trigger from an app (per-app Bearer token): rules are evaluated immediately instead of waiting for the poll."""
    from . import alerts
    app_name = alerts.app_for_token(request.headers.get('authorization', '').removeprefix('Bearer ').strip())
    if not app_name:
        raise HTTPException(401)
    rate(f'ingest:{app_name}', 120, 60)
    b = await request.json()
    evs = b if isinstance(b, list) else [b]
    from .sanitize import clean
    lines = [{'line': clean(json.dumps(e, ensure_ascii=False), 2000), 'fields': {str(k): (str(v) if not isinstance(v, (dict, list)) else '') for k, v in e.items()}} for e in evs[:50] if isinstance(e, dict)]
    fired = await alerts.evaluate_lines(app_name, lines, 'push')
    store.audit(f'ingest:{app_name}', 'ingest_event', app_name, {'events': len(lines), 'fired': len(fired)})
    return {'accepted': len(lines), 'fired': [n['id'] for n in fired]}


@app.get('/api/guardrails')
async def guardrails_get(request: Request):
    require(request)
    from . import alerts, control
    return {**alerts.guardrails(), 'ai_enabled': control.ai_enabled(), 'ai_off_reason': store.kv_get('ai_off_reason')}


@app.put('/api/guardrails')
async def guardrails_put(request: Request):
    require(request, csrf=True)
    from . import alerts
    return alerts.set_guardrails(await request.json())


# ------------------------------------------------------------------ long-term memory
@app.get('/api/memory')
async def memory_list(request: Request, kind: str = None):
    require(request)
    from . import memory
    return memory.listing(kind, 300)


@app.post('/api/memory')
async def memory_add(request: Request):
    require(request, csrf=True)
    from . import memory
    b = await request.json()
    if b.get('kind', 'fact') not in ('fact', 'preference'):
        raise HTTPException(400)
    return {'id': memory.add(b.get('kind', 'fact'), str(b.get('text', '')), b.get('app') or None)}


@app.put('/api/memory/{mid}')
async def memory_edit(mid: str, request: Request):
    require(request, csrf=True)
    from . import memory
    b = await request.json()
    memory.update(mid, str(b.get('text', '')))
    return {'ok': True}


@app.delete('/api/memory/{mid}')
async def memory_del(mid: str, request: Request):
    require(request, csrf=True)
    from . import memory
    memory.delete(mid)
    return {'ok': True}


# ------------------------------------------------------------------ durable job queue (dead-letter + replay)
@app.get('/api/jobs')
async def jobs_list(request: Request, state: str = None):
    require(request)
    from . import jobs
    from .resilience import BREAKERS
    return {'stats': jobs.stats(), 'jobs': jobs.listing(state, 100), 'circuits': [b.info() for b in BREAKERS.values()]}


@app.post('/api/jobs/{jid}/replay')
async def jobs_replay(jid: str, request: Request):
    require(request, csrf=True)
    from . import jobs
    r = await jobs.replay(jid, 'owner')
    if r.get('error'):
        raise HTTPException(409, r['error'])
    return r


@app.post('/api/jobs/{jid}/discard')
async def jobs_discard(jid: str, request: Request):
    require(request, csrf=True)
    from . import jobs
    return jobs.discard(jid, 'owner')


# ------------------------------------------------------------------ public status page (data pushed by the external probe)
@app.post('/api/status/ingest')
async def status_ingest(request: Request):
    tok = request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    want = __import__('os').environ.get('STATUS_INGEST_TOKEN', '')
    if not want or not secrets.compare_digest(tok, want):
        raise HTTPException(401)
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get('apps'), list) or len(json.dumps(body)) > 200000:
        raise HTTPException(400, 'bad payload')
    store.kv_set('public_status', {'received_at': time.time(), **redact_obj(body)})
    return {'ok': True}


@app.get('/api/status/vantage')
async def status_vantage(request: Request):
    """Second vantage point for the external monitor: Negahban's latest in-Liara probe per app (read-only, token)."""
    tok = request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    want = __import__('os').environ.get('STATUS_INGEST_TOKEN', '')
    if not want or not secrets.compare_digest(tok, want):
        raise HTTPException(401)
    iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t)) if t else None
    out = []
    for a in store.apps():
        if a['role'] == 'off':
            continue
        r = store.q('select * from samples where app=? order by ts desc limit 1', (a['name'],), one=True)
        out.append({'name': a['name'], 'ok': bool(r and r['ok']), 'status': r['status'] if r else None, 'latency_ms': r['latency_ms'] if r else None,
                    'checked_at': iso(r['ts']) if r else None, 'error': None if (r and r['ok']) else ('no probe yet' if not r else f"HTTP {r['status']}")})
    return {'vantage': 'liara-internal', 'generated_at': iso(time.time()), 'apps': out}


@app.get('/status')
async def status_page():
    """Public, read-only, secret-free status page (no auth). Rendered server-side, all values escaped."""
    from fastapi.responses import HTMLResponse
    from html import escape as e
    d = store.kv_get('public_status') or {}
    VERD = {'up': '🟢 در دسترس', 'down': '🔴 قطع', 'degraded_external_only': '🟡 فقط از بیرون قطع', 'unknown': '⚪ نامعلوم'}

    def vant(v):
        if not v:
            return '—'
        return ('✅' if v.get('ok') else '❌') + f" {e(str(v.get('latency_ms') if v.get('latency_ms') is not None else '—'))} ms"
    rows = ''.join(
        f"<tr><td dir='ltr'>{e(str(a.get('name', '')))}</td><td>{VERD.get(a.get('verdict'), '🟢 در دسترس' if a.get('up') else '🔴 قطع')}</td>"
        f"<td>{vant((a.get('vantages') or {}).get('external') or {'ok': a.get('up'), 'latency_ms': a.get('latency_ms')})}</td>"
        f"<td>{vant((a.get('vantages') or {}).get('internal'))}</td>"
        f"<td>{e(str(a.get('p95_ms_24h', '—')))} ms</td><td>{e(str(a.get('availability_24h', '—')))}٪</td><td>{e(str(a.get('availability_7d', '—')))}٪</td>"
        f"<td>{e(str(a.get('tls_days_left', '—')))}</td></tr>" for a in d.get('apps', []))
    notes = ''.join(f"<li>{e(str(n.get('kind', '')))} — {e(str(n.get('vantage', '')))}: <span dir='ltr'>{e(str(n.get('start', '')))} → {e(str(n.get('end', '')))}</span> ({e(str(n.get('minutes', '')))} دقیقه)</li>" for n in (d.get('notes') or [])[:20])
    slo = d.get('slo') or {}
    gen = e(str(d.get('generated_at', '—')))
    body = (f"<table><tr><th>سرویس</th><th>وضعیت</th><th>پایش بیرونی</th><th>پایش داخل لیارا</th><th>p95 (۲۴ ساعت)</th><th>دسترس‌پذیری ۲۴ ساعت</th><th>۷ روز</th><th>روز تا انقضای TLS</th></tr>{rows}</table>"
            + (f"<h3>یادداشت‌ها</h3><ul>{notes}</ul>" if notes else '') + (f"<p class='muted small'>{e(str((d.get('slo') or {}).get('rules', '')))}</p>" if (d.get('slo') or {}).get('rules') else '')
            if rows else '<p>پایش بیرونی آماده است — فعال نشده.</p>')
    html = f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>وضعیت سرویس‌ها</title>
<link rel="stylesheet" href="/static/style.css"><style>body{{padding:20px}}table{{background:#fff;border-radius:12px}}</style></head><body><h1>🛡️ وضعیت سرویس‌ها</h1>
<p class="muted small">به‌روزرسانی: <span dir="ltr">{gen}</span> · هدف SLO: دسترس‌پذیری {e(str(slo.get('availability', '—')))}٪، p95 کمتر از {e(str(slo.get('p95_ms', '—')))} ms</p>
<div class="card tablewrap">{body}</div></body></html>"""
    return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})


@app.post('/mcp')
async def mcp_http(request: Request):
    from .mcp import handle_http
    return await handle_http(request)
