"""Daily health report: per-app uptime/latency/resources from Negahban's own probes, incidents and how they were fixed,
autopilot actions, cost & credit runway, LLM usage, and deterministic recommendations (+ a short LLM summary).
Generated automatically every day at the configured Tehran hour (kv `report_hour`, default 8) and on demand."""
import json, statistics, time
from . import plans, store
from .liara import LiaraError, liara

SCHEMA = 'create table if not exists reports(id text primary key, day text, created_at real, data text);'
TEHRAN = 3.5 * 3600


def ensure():
    from contextlib import closing
    with store._lock, closing(store._db()) as c:
        c.executescript(SCHEMA); c.commit()


def tehran_day(ts=None):
    return time.strftime('%Y-%m-%d', time.gmtime((ts or time.time()) + TEHRAN))


def _pct(v):
    return round(100 * v, 2) if v is not None else None


async def build(hours=24, with_llm=True, origin='manual'):
    ensure()
    now, since = time.time(), time.time() - hours * 3600
    await plans.catalogue()
    team = {}
    try:
        team = {p['project_id']: p for p in await liara.list_apps()}
    except LiaraError:
        pass
    apps, recs = [], []
    for a in store.apps():
        if a['role'] in ('off', 'replica'):
            continue
        rows = store.samples(a['name'], hours * 3600)
        lat = [r['latency_ms'] for r in rows if r['latency_ms'] and r['ok']]
        cpu = [r['cpu'] / r['cpu_limit'] for r in rows if r['cpu_limit']]
        mem = [r['mem'] / r['mem_limit'] for r in rows if r['mem_limit']]
        p = team.get(a['name'], {})
        incs = [i for i in store.incidents(100, a['name']) if i['opened_at'] >= since]
        props = store.q('select tool,title,status,auto,origin,created_at from proposals where app=? and created_at>=? order by created_at', (a['name'], since))
        uptime = (sum(1 for r in rows if r['ok']) / len(rows)) if rows else None
        item = {'app': a['name'], 'role': a['role'], 'plan': p.get('planID'), 'bundle': p.get('bundlePlanID'), 'scale': p.get('scale'),
                'toman_per_hour': plans.hourly(p['planID'], p.get('bundlePlanID') or 'basic') if p.get('planID') else None,
                'samples': len(rows), 'uptime_pct': _pct(uptime),
                'latency_avg_ms': round(statistics.mean(lat)) if lat else None,
                'latency_p95_ms': round(sorted(lat)[int(len(lat) * 0.95) - 1]) if len(lat) >= 5 else None,
                'cpu_avg_pct': _pct(statistics.mean(cpu)) if cpu else None, 'cpu_peak_pct': _pct(max(cpu)) if cpu else None,
                'mem_avg_pct': _pct(statistics.mean(mem)) if mem else None, 'mem_peak_pct': _pct(max(mem)) if mem else None,
                'incidents': [{'title': i['title'], 'kind': i['kind'], 'status': i['status'],
                               'resolution': (i['timeline'] or [{}])[-1].get('text', '')[:200]} for i in incs],
                'autopilot_actions': [{'title': x['title'], 'status': x['status']} for x in props if x['auto']],
                'owner_actions': [{'title': x['title'], 'status': x['status']} for x in props if not x['auto'] and x['status'] in ('done', 'unverified', 'failed')],
                'locked': store.policy(a['name']).get('write_lock')}
        apps.append(item)
        n = a['name']
        if uptime is not None and uptime < 0.99:
            recs.append(f'دسترس‌پذیری {n} در ۲۴ ساعت گذشته {item["uptime_pct"]}٪ بود؛ رخدادها و علت‌شان را مرور کنید.')
        if item['cpu_peak_pct'] and item['cpu_peak_pct'] > 85 or item['mem_peak_pct'] and item['mem_peak_pct'] > 85:
            pol = store.policy(n)
            recs.append(f'{n} به سقف منابع رسیده است (اوج CPU {item["cpu_peak_pct"]}٪، حافظه {item["mem_peak_pct"]}٪)' +
                        ('؛ مقیاس خودکار فعال است.' if pol.get('autopilot') and pol.get('resize') else '؛ فعال کردن مقیاس خودکار یا ارتقای پلن را در نظر بگیرید.'))
        if item['cpu_avg_pct'] is not None and item['cpu_avg_pct'] < 10 and (item['mem_peak_pct'] or 0) < 30 and p.get('planID') in plans.ORDER and plans.ORDER.index(p['planID']) > 0 and not item['locked']:
            lower = plans.neighbour(p['planID'], -1)
            d = plans.cost_delta(p['planID'], lower, p.get('bundlePlanID') or 'basic')
            recs.append(f'{n} کم‌مصرف است (میانگین CPU {item["cpu_avg_pct"]}٪)؛ کاهش پلن به {lower} حدود {plans.fmt_toman(-d["delta_day"])} در روز صرفه‌جویی دارد.')
        kinds = [i['kind'] for i in incs]
        for k in set(kinds):
            if kinds.count(k) >= 2:
                recs.append(f'رخداد «{k}» روی {n} {kinds.count(k)} بار تکرار شده؛ علت ریشه‌ای (کد یا پیکربندی) را برطرف کنید.')
    cost = {}
    try:
        from .tools import team_usage
        cost = await team_usage({}, {})
        if cost.get('hours_left') is not None and cost['hours_left'] < 7 * 24:
            recs.append(f"اعتبار تیم حدود {round(cost['hours_left'] / 24, 1)} روز دیگر کافی است؛ حساب را شارژ کنید یا برنامه‌های بی‌استفاده را خاموش کنید.")
    except Exception as e:
        cost = {'error': str(e)[:200]}
    from .llm import usage_rows
    day = time.strftime('%Y-%m-%d', time.gmtime())
    lu = [r for r in usage_rows(1) if r['day'] == day]
    tin = sum(r['tokens_in'] + r['cache_read'] + r['cache_write'] for r in lu)
    llm_use = {'calls': sum(r['calls'] for r in lu), 'tokens_in': sum(r['tokens_in'] for r in lu), 'tokens_out': sum(r['tokens_out'] for r in lu),
               'cache_read': sum(r['cache_read'] for r in lu), 'cost_usd': round(sum(r['cost_usd'] for r in lu), 4),
               'cache_hit_pct': round(100 * sum(r['cache_read'] for r in lu) / tin, 1) if tin else None,
               'by_feature': {r['feature']: r['calls'] for r in lu}}
    pend = store.proposals('pending', 50)
    if pend:
        recs.append(f'{len(pend)} کارت تأیید منتظر تصمیم شماست.')
    if not recs:
        recs.append('همه‌چیز پایدار است؛ اقدام خاصی لازم نیست.')
    rep = {'day': tehran_day(), 'generated_at': now, 'period_hours': hours, 'origin': origin, 'apps': apps, 'cost': cost, 'llm': llm_use,
           'recommendations': recs, 'pending_cards': len(pend)}
    rep['summary'] = _fallback_summary(rep)
    if with_llm:
        rep['summary'] = await _llm_summary(rep) or rep['summary']
    rid = store.uid('rep_')
    store.x('insert into reports(id,day,created_at,data) values(?,?,?,?)', (rid, rep['day'], now, store.J(rep)))
    store.audit('negahban', 'daily_report', None, {'report': rid, 'origin': origin})
    rep['id'] = rid
    return rep


def _fallback_summary(rep):
    ups = [a for a in rep['apps'] if a['uptime_pct'] is not None]
    worst = min(ups, key=lambda a: a['uptime_pct']) if ups else None
    n_inc = sum(len(a['incidents']) for a in rep['apps'])
    n_auto = sum(len(a['autopilot_actions']) for a in rep['apps'])
    s = f"در ۲۴ ساعت گذشته {len(rep['apps'])} برنامه پایش شد؛ {n_inc} رخداد و {n_auto} اقدام خودکار ثبت شد."
    if worst:
        s += f" کمترین دسترس‌پذیری مربوط به {worst['app']} با {worst['uptime_pct']}٪ بود."
    if rep['cost'].get('total_toman_per_hour'):
        s += f" هزینه‌ی جاری {plans.fmt_toman(rep['cost']['total_toman_per_hour'])} در ساعت است."
    return s


async def _llm_summary(rep):
    from .llm import LLMError, feature, llm
    if not llm.available:
        return None
    feature.set('report')
    compact = {k: rep[k] for k in ('apps', 'cost', 'recommendations')}
    try:
        m = await llm.chat([{'role': 'system', 'content': 'You write a 3-sentence daily operations summary in fluent Persian for a non-technical website owner, '
                                                          'from the JSON data. Mention the most important event, cost, and the top recommendation. No markdown.'},
                            {'role': 'user', 'content': json.dumps(compact, ensure_ascii=False, default=str)[:9000]}], max_tokens=400, timeout=60)
        return (m.get('content') or '').strip()[:1200] or None
    except LLMError:
        return None


def listing(limit=30):
    ensure()
    rows = store.q('select id, day, created_at, data from reports order by created_at desc limit ?', (limit,))
    out = []
    for r in rows:
        d = json.loads(r['data'])
        out.append({'id': r['id'], 'day': r['day'], 'created_at': r['created_at'], 'origin': d.get('origin'), 'summary': d.get('summary'),
                    'recommendations': len(d.get('recommendations') or [])})
    return out


def get(rid=None):
    ensure()
    r = store.q('select * from reports where id=?', (rid,), one=True) if rid else store.q('select * from reports order by created_at desc limit 1', one=True)
    if not r:
        return None
    d = json.loads(r['data'])
    d['id'] = r['id']
    return d


async def maybe_scheduled():
    """Called from the orchestrator loop: one report per Tehran day at/after the configured hour."""
    hour = int(store.kv_get('report_hour', 8))
    tnow = time.gmtime(time.time() + TEHRAN)
    if tnow.tm_hour < hour or store.kv_get('report_last_day') == tehran_day():
        return None
    store.kv_set('report_last_day', tehran_day())
    rep = await build(origin='scheduled')
    from . import bus
    bus.publish('report', report={'id': rep['id'], 'day': rep['day'], 'summary': rep['summary']})
    return rep
