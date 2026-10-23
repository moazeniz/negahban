"""Liara plan catalogue and cost maths. Prices come live from GET /v1/me (toman per HOUR); a verified snapshot
(2026-10-22) is the fallback when the API is unreachable."""
import time
from .liara import liara

ORDER = ['small-g2', 'medium-g2', 'standard-base-g2', 'standard-plus-g2', 'pro-g2', 'pro-plus-g2', 'pro-max-g2', 'express-g2']
FA_NAME = {'small-g2': 'زمین (Earth)', 'medium-g2': 'مریخ (Mars)', 'standard-base-g2': 'مشتری (Jupiter)', 'standard-plus-g2': 'زحل (Saturn)',
           'pro-g2': 'اورانوس (Uranus)', 'pro-plus-g2': 'نپتون (Neptune)', 'pro-max-g2': 'پلوتو (Pluto)', 'express-g2': 'اکسپرس'}
BUNDLE_FA = {'basic': 'برنزی (basic)', 'standard': 'نقره‌ای (standard)', 'pro': 'طلایی (pro)'}
_SNAPSHOT = {  # planID: (cpu cores, ram GB, disk GB, toman/h)
    'small-g2': (0.5, 0.5, 5, 659.72), 'medium-g2': (1, 1, 10, 1145.83), 'standard-base-g2': (1, 2, 20, 2048.61),
    'standard-plus-g2': (2, 4, 40, 3680.55), 'pro-g2': (4, 8, 80, 6388.88), 'pro-plus-g2': (8, 16, 160, 11111.11),
    'pro-max-g2': (16, 32, 320, 19444.44), 'express-g2': (32, 64, 640, 34166.66)}
_BUNDLE_SNAPSHOT = {'small-g2': {'basic': 381.94, 'standard': 659.72, 'pro': 1145.83}, 'medium-g2': {'basic': 659.72, 'standard': 1145.83, 'pro': 2048.61},
                    'standard-base-g2': {'basic': 1145.83, 'standard': 2048.61, 'pro': 3680.55}, 'standard-plus-g2': {'basic': 2048.61, 'standard': 3680.55, 'pro': 6388.88},
                    'pro-g2': {'basic': 3680.55, 'standard': 6388.88, 'pro': 11111.11}}
_cache = {'t': 0, 'plans': None, 'bundles': None, 'limits': None}


async def catalogue(force=False):
    if _cache['plans'] and not force and time.time() - _cache['t'] < 3600:
        return _cache
    try:
        plans, _, _ = await liara.plans()
        pp, bb = {}, {}
        limits = {}
        for pid, v in plans.get('projects', {}).items():
            if 'g2' in pid and v.get('available'):
                pp[pid] = {'id': pid, 'cpu': v['CPU']['amount'], 'ram': v['RAM']['amount'], 'disk': v.get('volume'), 'price_h': float(v['price']), 'name_fa': FA_NAME.get(pid, pid)}
        for pid, bund in plans.get('projectBundlePlans', {}).items():
            bb[pid] = {b: float(x.get('price', 0)) for b, x in bund.items() if x.get('available')}
            limits[pid] = {b: {k: x.get(k) for k in ('maxDeploymentCountInDay', 'zeroDowntime', 'maxLogsRetention', 'buildTimeout', 'maxSourceSize', 'maxDomainsCount', 'rollback')} for b, x in bund.items()}
        if pp:
            _cache.update(t=time.time(), plans=pp, bundles=bb, limits=limits, live=True)
            return _cache
    except Exception:
        pass
    if not _cache['plans']:
        _cache.update(t=time.time() - 3000, live=False, limits={},
                      plans={k: {'id': k, 'cpu': c, 'ram': r, 'disk': d, 'price_h': p, 'name_fa': FA_NAME[k]} for k, (c, r, d, p) in _SNAPSHOT.items()},
                      bundles=_BUNDLE_SNAPSHOT)
    return _cache


def plan_sync(pid):
    p = (_cache['plans'] or {}).get(pid)
    if p:
        return p
    c, r, d, pr = _SNAPSHOT.get(pid, (0.5, 0.5, 5, 659.72))
    return {'id': pid, 'cpu': c, 'ram': r, 'disk': d, 'price_h': pr, 'name_fa': FA_NAME.get(pid, pid)}


def hourly(pid, bundle='basic'):
    b = ((_cache['bundles'] or _BUNDLE_SNAPSHOT).get(pid) or {}).get(bundle, 0)
    return round(plan_sync(pid)['price_h'] + b, 2)


def neighbour(pid, step):
    i = ORDER.index(pid) if pid in ORDER else 0
    j = max(0, min(len(ORDER) - 1, i + step))
    return ORDER[j]


def within(pid, lo, hi):
    return ORDER.index(lo) <= ORDER.index(pid) <= ORDER.index(hi) if all(p in ORDER for p in (pid, lo, hi)) else False


def cost_delta(cur, new, bundle='basic'):
    a, b = hourly(cur, bundle), hourly(new, bundle)
    return {'from': cur, 'to': new, 'from_h': a, 'to_h': b, 'delta_h': round(b - a, 2), 'delta_day': round((b - a) * 24), 'delta_month': round((b - a) * 720)}


def recommend(cpu_need, ram_need_gb, bundle='basic'):
    """Smallest plan that leaves ~30% headroom over the measured/estimated need."""
    for pid in ORDER:
        p = plan_sync(pid)
        if p['cpu'] >= cpu_need * 1.3 and p['ram'] >= ram_need_gb * 1.3:
            return pid
    return ORDER[-1]


def fmt_toman(v):
    return f'{int(round(v)):,}'.replace(',', '٬') + ' تومان'
