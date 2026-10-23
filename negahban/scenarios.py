"""Demo scenarios ("سناریوهای نمایشی"): inject realistic faults into the demo app so the full loop
detect -> diagnose -> propose -> approve -> fix -> verify can be shown live. Only ever targets the demo app."""
import asyncio, time
import httpx
from . import bus, store
from . import workspace as ws
from .config import settings
from .liara import liara

BUGGY_PRICING = '''"""Price helpers for the store."""


def discount_percent(p):
    """Percentage off the old price (simplified)."""
    old = p.get("old_price") or 0
    return round(100 * (old - p["price"]) / old)


def fmt_price(v):
    """12500000 -> '۱۲٬۵۰۰٬۰۰۰ تومان'"""
    digits = str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬")
    return f"{int(v):,}".translate(digits) + " تومان"


def cart_total(items, catalogue):
    total = 0
    for it in items:
        p = catalogue[it["id"]]
        total += p["price"] * int(it.get("qty", 1))
    return total
'''

SCENARIOS = {
    'bad_release': {'title': 'انتشار نسخه‌ی باگ‌دار', 'desc': 'یک توسعه‌دهنده «ساده‌سازی محاسبه‌ی تخفیف» را بدون اجرای تست منتشر می‌کند؛ صفحه‌ی اصلی خطای 500 می‌دهد.', 'icon': '🐞', 'deploys': 1},
    'broken_build': {'title': 'بیلد شکسته', 'desc': 'یک وابستگی ناموجود به requirements.txt اضافه می‌شود؛ بیلد در لیارا شکست می‌خورد.', 'icon': '🧱', 'deploys': 1},
    'missing_env': {'title': 'حذف متغیر محیطی', 'desc': 'کلید درگاه پرداخت (PAYMENT_GATEWAY_KEY) از تنظیمات حذف می‌شود؛ برنامه بالا نمی‌آید.', 'icon': '🔑', 'deploys': 1},
    'memory_leak': {'title': 'نشت حافظه', 'desc': 'کش جلسه‌ها بی‌وقفه بزرگ می‌شود تا به سقف ۵۱۲ مگابایت برسد.', 'icon': '💧', 'deploys': 0},
    'traffic_spike': {'title': 'هجوم ترافیک', 'desc': 'ده دقیقه درخواست هم‌زمان به گزارش فروش (پرمصرف از نظر CPU).', 'icon': '📈', 'deploys': 0},
    'slow': {'title': 'کندی پاسخ', 'desc': 'هر درخواست ۳٫۵ ثانیه تأخیر می‌گیرد (شبیه وابستگی کند).', 'icon': '🐢', 'deploys': 0},
    'reset': {'title': 'بازگرداندن به حالت عادی', 'desc': 'خاموش کردن کلیدهای خطا و توقف ترافیک مصنوعی (بدون استقرار).', 'icon': '🧹', 'deploys': 0},
}
LOAD = {'task': None, 'until': 0, 'sent': 0, 'errors': 0, 'lat': []}
LEAK = {'task': None}


async def _leak_ramp(step_mb=10, every=10, max_mb=900, minutes=14):
    """Grow the demo app's leak gradually (~2 MB/s) so the trend is visible before an OOM; stop if the app restarts."""
    target, peak, t0 = 60, 0, time.time()
    import json as _j
    while time.time() - t0 < minutes * 60 and target < max_mb:
        try:  # look first: if the container was restarted (by Negahban or an OOM kill) the leak is gone -> stop, don't re-seed it
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(_url() + '/__chaos/state', headers={'X-Chaos-Token': settings.demo_chaos_token})
            mb = r.json().get('leak_mb', 0) if r.status_code == 200 else None
        except Exception:
            mb = None
        if mb is not None and peak and mb < peak - 50:
            bus.publish('op', app=settings.demo_app, status='done', text=f'نشت حافظه متوقف شد (کانتینر دوباره راه‌اندازی شد؛ اوج {peak}MB)')
            return
        if mb is None:  # app unreachable (e.g. being restarted): wait and look again
            await asyncio.sleep(every)
            continue
        target += step_mb
        try:
            await _chaos('/__chaos/leak', target_mb=target)
        except Exception:
            pass
        peak = max(peak, mb)
        await asyncio.sleep(every)


def _url():
    a = store.get_app(settings.demo_app) or {}
    return (a.get('url') or f'https://{settings.demo_app}.liara.run').rstrip('/')


async def _chaos(path, **params):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(_url() + path, params=params, headers={'X-Chaos-Token': settings.demo_chaos_token})
        return {'status': r.status_code, 'body': r.text[:200]}


async def _load(minutes, concurrency):
    LOAD.update(until=time.time() + minutes * 60, sent=0, errors=0, lat=[])

    async def worker(c):
        while time.time() < LOAD['until']:
            t0 = time.perf_counter()
            try:
                r = await c.get(_url() + '/api/report', params={'n': 150000})
                LOAD['errors'] += r.status_code >= 500
            except Exception:
                LOAD['errors'] += 1
                await asyncio.sleep(1)
            LOAD['sent'] += 1
            LOAD['lat'] = (LOAD['lat'] + [round((time.perf_counter() - t0) * 1000)])[-50:]
    async with httpx.AsyncClient(timeout=30) as c:
        await asyncio.gather(*(worker(c) for _ in range(concurrency)))
    bus.publish('op', app=settings.demo_app, status='done', text=f"ترافیک مصنوعی تمام شد ({LOAD['sent']} درخواست)")


def load_status():
    lat = LOAD['lat']
    return {'active': LOAD['until'] > time.time(), 'seconds_left': max(0, int(LOAD['until'] - time.time())), 'sent': LOAD['sent'], 'errors': LOAD['errors'],
            'avg_latency_ms': round(sum(lat) / len(lat)) if lat else None}


async def trigger(name, actor, wait=False):
    app = settings.demo_app
    if name not in SCENARIOS:
        raise ValueError('unknown scenario')
    store.audit(actor, f'scenario:{name}', app, {'scenario': name})
    bus.publish('op', app=app, status='running', text=f"سناریوی نمایشی: {SCENARIOS[name]['title']}")
    ctx = {'origin': 'scenario'}
    if name == 'bad_release':
        ws.ensure(app)
        ws.write_files(app, [{'path': 'shop/pricing.py', 'content': BUGGY_PRICING}], 'refactor: ساده‌سازی محاسبه‌ی درصد تخفیف')
        from .deployer import deploy_workspace
        job = _bg(deploy_workspace(app, 'refactor: simplify discount (by developer)', ctx), app)
        if wait:
            return await job
        asyncio.create_task(job)
        return {'started': True, 'note': 'نسخه‌ی باگ‌دار در حال استقرار است (حدود ۱-۲ دقیقه). نگهبان خطا را خودش پیدا می‌کند.'}
    if name == 'broken_build':
        ws.ensure(app)
        req = ws.read(app, 'requirements.txt') or ''
        ws.write_files(app, [{'path': 'requirements.txt', 'content': req.rstrip('\n') + '\npersian-invoice-toolkit==4.2.0\n'}], 'feat: صدور فاکتور رسمی (persian-invoice-toolkit)')
        from .deployer import deploy_workspace
        job = _bg(deploy_workspace(app, 'feat: invoices (by developer)', ctx), app)
        if wait:
            return await job
        asyncio.create_task(job)
        return {'started': True, 'note': 'بیلد در حال اجراست و شکست خواهد خورد؛ نسخه‌ی فعلی همچنان سرویس می‌دهد.'}
    if name == 'missing_env':
        envs = await liara.envs(app)
        from .diagnose import env_snapshot
        env_snapshot(app, envs)
        envs.pop('PAYMENT_GATEWAY_KEY', None)
        await liara.update_envs(app, envs)
        return {'started': True, 'note': 'متغیر حذف شد؛ لیارا برنامه را با تنظیمات جدید ری‌استارت می‌کند و برنامه بالا نمی‌آید.'}
    if name == 'memory_leak':
        if not (LEAK['task'] and not LEAK['task'].done()):
            LEAK['task'] = asyncio.create_task(_leak_ramp())
        return {'started': True, 'note': 'کش جلسه‌ها حدود ۱ مگابایت در ثانیه بزرگ می‌شود؛ نگهبان باید پیش از پر شدن حافظه (OOM) آن را تشخیص دهد.'}
    if name == 'slow':
        return await _chaos('/__chaos/slow', ms=3500)
    if name == 'traffic_spike':
        if not (LOAD['task'] and not LOAD['task'].done()):
            LOAD['task'] = asyncio.create_task(_load(10, 12))
        return {'started': True, 'load': load_status()}
    if name == 'reset':
        LOAD['until'] = 0
        try:
            return await _chaos('/__chaos/reset')
        except Exception as e:
            return {'note': f'برنامه در دسترس نیست ({type(e).__name__})'}


async def _bg(coro, app):
    try:
        r = await coro
        bus.publish('op', app=app, status='done', text=f"استقرار سناریو: {r.get('release_state')}")
        return r
    except Exception as e:
        bus.publish('op', app=app, status='failed', text=f'استقرار سناریو ناموفق: {e}')
