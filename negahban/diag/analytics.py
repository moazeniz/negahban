"""Analytics on metrics, logs, probe history and billing: EWMA/z-score anomaly detection, linear trend + forecast (memory-leak
"RAM hits the plan limit in ~X h"), deploy <-> error/latency correlation with a significance check, cost forecast and budget
burn, SLO/uptime and error-budget burn from the orchestrator's probe samples. Core maths are pure functions (unit-tested)."""
import asyncio, math, statistics, time
from .. import plans
from ..liara import liara
from .common import DiagError, spec


# ---------------------------------------------------------------- pure maths
def ewma_anomalies(series, alpha=0.3, z=3.0, warmup=8, min_rel_std=0.02):
    """series [(ts, v)] -> points whose deviation from the EWMA forecast exceeds z * EWM std.
    The std floor (min_rel_std * |mean|) stops flat series from flagging noise."""
    if len(series) <= warmup:
        return []
    mean, var, out = series[0][1], 0.0, []
    for i, (t, v) in enumerate(series[1:], 1):
        std = math.sqrt(var)
        floor = max(min_rel_std * abs(mean), 1e-9)
        sd = max(std, floor)
        zz = (v - mean) / sd
        if i >= warmup and abs(zz) >= z:
            out.append({'ts': t, 'value': round(v, 4), 'expected': round(mean, 4), 'z': round(zz, 1), 'direction': 'spike' if zz > 0 else 'drop'})
        d = v - mean
        mean += alpha * d
        var = (1 - alpha) * (var + alpha * d * d)
    return out


def linfit(series):
    """Least squares on [(ts, v)] -> slope per second, intercept, r2."""
    n = len(series)
    if n < 3:
        return None
    xs, ys = [t for t, _ in series], [v for _, v in series]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs) or 1e-9
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    icpt = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys) or 1e-12
    ss_res = sum((y - (icpt + slope * x)) ** 2 for x, y in zip(xs, ys))
    return {'slope': slope, 'intercept': icpt, 'r2': max(0.0, 1 - ss_res / ss_tot), 'n': n}


def forecast_limit(series, limit, horizon_h=72):
    """When does a rising series cross `limit`? Returns verdict + hours_to_limit (None if not rising/fit poor)."""
    f = linfit(series)
    if not f:
        return {'verdict': 'insufficient_data', 'points': len(series)}
    last_t, last_v = series[-1]
    level = statistics.fmean(v for _, v in series[-3:])
    rise = sum(1 for (_, a), (_, b) in zip(series, series[1:]) if b > a) / (len(series) - 1)
    per_h = f['slope'] * 3600
    out = {'slope_per_hour': round(per_h, 4), 'r2': round(f['r2'], 2), 'monotonic_rise': round(rise, 2), 'level': round(level, 4),
           'limit': limit, 'util': round(level / limit, 3) if limit else None, 'points': f['n'], 'span_h': round((last_t - series[0][0]) / 3600, 2)}
    if limit and per_h > 0 and level < limit:
        hrs = (limit - level) / per_h
        out['hours_to_limit'] = round(hrs, 1)
        out['eta'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(last_t + hrs * 3600))
    leak = per_h > 0 and f['r2'] >= 0.6 and rise >= 0.55 and out['span_h'] >= 0.5
    if leak and out.get('hours_to_limit') is not None and out['hours_to_limit'] <= horizon_h:
        out['verdict'] = 'leak_likely'
    elif leak:
        out['verdict'] = 'slow_rise'
    elif limit and level >= 0.9 * limit:
        out['verdict'] = 'near_limit'
    else:
        out['verdict'] = 'stable'
    return out


def two_proportion_p(e1, n1, e2, n2):
    """Two-sided p-value that error rates e1/n1 and e2/n2 differ (normal approximation; conservative for tiny n)."""
    if not n1 or not n2:
        return None
    p1, p2, p = e1 / n1, e2 / n2, (e1 + e2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0 if p1 == p2 else 0.0
    zz = abs(p1 - p2) / se
    return round(math.erfc(zz / math.sqrt(2)), 4)


def welch_p(a, b):
    """Two-sided p-value for different means (Welch t, normal approx for the tail; fine for n >= ~8)."""
    if len(a) < 3 or len(b) < 3:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return 1.0 if statistics.fmean(a) == statistics.fmean(b) else 0.0
    t = abs(statistics.fmean(a) - statistics.fmean(b)) / se
    return round(math.erfc(t / math.sqrt(2)), 4)


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round((len(xs) - 1) * p)))], 1)


def compare_windows(before, after):
    """before/after: {'requests','errors','latencies':[ms]} -> deltas + significance + verdict."""
    rb = before['errors'] / before['requests'] if before['requests'] else None
    ra = after['errors'] / after['requests'] if after['requests'] else None
    p_err = two_proportion_p(before['errors'], before['requests'], after['errors'], after['requests'])
    p_lat = welch_p(before['latencies'], after['latencies'])
    out = {'error_rate_before': round(rb, 4) if rb is not None else None, 'error_rate_after': round(ra, 4) if ra is not None else None, 'p_error': p_err,
           'errors_before': before['errors'], 'errors_after': after['errors'], 'requests_before': before['requests'], 'requests_after': after['requests'],
           'p95_before_ms': pct(before['latencies'], .95), 'p95_after_ms': pct(after['latencies'], .95), 'p_latency': p_lat,
           'tracebacks_before': before.get('tracebacks', 0), 'tracebacks_after': after.get('tracebacks', 0)}
    worse_err = (ra or 0) > (rb or 0) * 1.5 + 0.01 and (p_err is not None and p_err < 0.05)
    new_errors = before.get('tracebacks', 0) == 0 and after.get('tracebacks', 0) >= 3
    mb, ma = (statistics.fmean(before['latencies']) if before['latencies'] else None), (statistics.fmean(after['latencies']) if after['latencies'] else None)
    worse_lat = mb and ma and ma > mb * 1.3 and p_lat is not None and p_lat < 0.05
    better = (rb or 0) > (ra or 0) * 1.5 + 0.01 and p_err is not None and p_err < 0.05
    out['verdict'] = 'regression' if worse_err or new_errors else 'slower' if worse_lat else 'improved' if better else 'no_significant_change'
    return out


def slo(samples, target=99.5):
    """samples: [{ts, ok, latency_ms, status}] (orchestrator probes, ~1/min) -> availability, error budget, burn rates, outages."""
    if not samples:
        return {'samples': 0}
    samples = sorted(samples, key=lambda s: s['ts'])
    n, good = len(samples), sum(1 for s in samples if s.get('ok'))
    avail = 100 * good / n
    allowed_bad = (1 - target / 100) * n
    out = {'samples': n, 'from': time.strftime('%Y-%m-%d %H:%M', time.localtime(samples[0]['ts'])), 'to': time.strftime('%Y-%m-%d %H:%M', time.localtime(samples[-1]['ts'])),
           'availability_pct': round(avail, 3), 'target_pct': target, 'met': avail >= target,
           'error_budget_used_pct': round(100 * (n - good) / allowed_bad, 1) if allowed_bad > 0 else None,
           'latency_p50_ms': pct([s['latency_ms'] for s in samples if s.get('latency_ms') is not None and s.get('ok')], .5),
           'latency_p95_ms': pct([s['latency_ms'] for s in samples if s.get('latency_ms') is not None and s.get('ok')], .95)}
    now = samples[-1]['ts']
    for label, win in (('1h', 3600), ('6h', 6 * 3600)):
        w = [s for s in samples if s['ts'] >= now - win]
        if w:
            bad = sum(1 for s in w if not s.get('ok')) / len(w)
            out[f'burn_rate_{label}'] = round(bad / (1 - target / 100), 2)  # 1.0 = spending the budget exactly on schedule
    outages, cur = [], None
    for s in samples:
        if not s.get('ok'):
            cur = cur or {'start': s['ts'], 'samples': 0, 'statuses': {}}
            cur['samples'] += 1
            cur['end'] = s['ts']
            k = str(s.get('status'))
            cur['statuses'][k] = cur['statuses'].get(k, 0) + 1
        elif cur:
            outages.append(cur)
            cur = None
    if cur:
        outages.append(cur)
    step = (samples[-1]['ts'] - samples[0]['ts']) / max(1, n - 1)
    out['downtime_minutes'] = round((n - good) * step / 60, 1)
    out['outages'] = [{'start': time.strftime('%m-%d %H:%M', time.localtime(o['start'])), 'minutes': round((o['end'] - o['start'] + step) / 60, 1), 'statuses': o['statuses']}
                      for o in sorted(outages, key=lambda o: -o['samples'])[:5]]
    return out


def burn(balance, hourly, history=None, budget_month=None):
    """balance (toman) + current run rate (+ optional [(ts, balance)] history) -> days left, observed burn, budget use."""
    out = {'balance_toman': balance, 'run_rate_toman_per_hour': round(hourly or 0, 1)}
    if balance is not None and hourly:
        out['hours_left'] = round(balance / hourly, 1)
        out['empty_at'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() + balance / hourly * 3600))
    hist = sorted(history or [])
    if len(hist) >= 2 and hist[-1][0] - hist[0][0] >= 1800:
        f = linfit(hist)
        if f and f['slope'] < 0:
            out['observed_toman_per_hour'] = round(-f['slope'] * 3600, 1)
            out['observed_vs_list_price'] = round(-f['slope'] * 3600 / hourly, 2) if hourly else None
    if budget_month:
        month = (hourly or 0) * 720
        out['budget_month_toman'] = budget_month
        out['projected_month_toman'] = round(month)
        out['budget_use_pct'] = round(100 * month / budget_month, 1)
        out['over_budget'] = month > budget_month
    return out


# ---------------------------------------------------------------- data access
async def series(app, metric, hours):
    since = time.time() - hours * 3600
    if metric == 'latency':
        from .. import store
        return [(s['ts'], s['latency_ms']) for s in store.samples(app, hours * 3600) if s.get('latency_ms') is not None]
    raw = await liara.metrics(app, 'memory' if metric == 'memory' else 'cpu', since)
    return [(t, v / 2 ** 20) for t, v in raw] if metric == 'memory' else raw


async def plan_limits(app):
    info = await liara.app(app)
    await plans.catalogue()
    pl = plans.plan_sync(info.get('planID'))
    return info.get('planID'), {'cpu': pl['cpu'], 'memory': pl['ram'] * 1024}


def _windows(rows, t, minutes):
    from .logintel import analyze
    lo, hi = t - minutes * 60, t + minutes * 60
    pick = lambda a, b: [r for r in rows if r.get('ts') and a <= r['ts'] < b]
    res = []
    for part in (pick(lo, t), pick(t, hi)):
        s = analyze(part)
        lat = []
        for r in part:
            from .logintel import parse_line
            ev = parse_line(r.get('line', ''), r.get('ts'))
            if ev.get('duration_ms') is not None and ev.get('status') is not None:
                lat.append(ev['duration_ms'])
        res.append({'requests': s['requests'], 'errors': s['errors_5xx'], 'latencies': lat, 'tracebacks': s['errors_total'], 'lines': s['lines']})
    return res


async def deploy_impact(app, releases=3, window_min=30):
    from datetime import datetime
    rels = [r for r in (await liara.releases(app, 15)).get('releases', []) if r.get('state') == 'READY'][:releases]
    if not rels:
        raise DiagError('no READY releases to compare')
    out = []
    for r in rels:
        ts = r.get('finishedAt') or r.get('createdAt')
        try:
            t = datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
        except (AttributeError, ValueError):
            continue
        end = min(time.time(), t + window_min * 60)
        rows = await liara.logs(app, start=t - window_min * 60, end=end, max_lines=1500)
        before, after = _windows(rows, t, window_min)
        cmp = compare_windows(before, after)
        cmp.update(release=r.get('tag'), type=r.get('type'), at=time.strftime('%Y-%m-%d %H:%M', time.localtime(t)), message=(r.get('message') or '')[:80],
                   lines_before=before['lines'], lines_after=after['lines'], after_window_min=round((end - t) / 60))
        out.append(cmp)
    worst = next((c for c in out if c['verdict'] == 'regression'), None)
    return {'app': app, 'window_minutes': window_min, 'releases': out,
            'suspect_release': worst['release'] if worst else None,
            'note': 'error rates use 5xx access lines + tracebacks in the logs; p-values from a two-proportion z-test (errors) and Welch test (latency)'}


async def balance_history():
    from .. import store
    h = store.kv_get('diag:balance_hist', [])
    try:
        b = await liara.billing()
        bal = (b.get('team') or b.get('user') or {}).get('balance')
        if bal is not None and (not h or time.time() - h[-1][0] > 300):
            h = (h + [[time.time(), float(bal)]])[-500:]
            store.kv_set('diag:balance_hist', h)
    except Exception:
        pass
    return h


# ---------------------------------------------------------------- tools
APP = {'app': {'type': 'string', 'description': 'Liara app name'}}


@spec('metric_anomalies', 'در حال یافتن ناهنجاری در متریک‌ها…', 'read',
      'Detect spikes/drops in cpu, memory or latency (probe history) with an EWMA + z-score model; returns anomalous points and a chart.',
      {**APP, 'metric': {'type': 'string', 'enum': ['cpu', 'memory', 'latency']}, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 72},
       'z': {'type': 'number', 'minimum': 2, 'maximum': 6}}, ['app', 'metric'])
async def metric_anomalies_tool(a, ctx):
    s = await series(a['app'], a['metric'], int(a.get('hours') or 6))
    an = ewma_anomalies(s, z=float(a.get('z') or 3.0))
    unit = {'cpu': 'هسته', 'memory': 'MB', 'latency': 'ms'}[a['metric']]
    step = max(1, len(s) // 150)
    return {'app': a['app'], 'metric': a['metric'], 'points': len(s), 'anomalies': [{**x, 'at': time.strftime('%H:%M', time.localtime(x['ts']))} for x in an[-20:]],
            'baseline': round(statistics.median(v for _, v in s), 3) if s else None,
            '_attach': {'kind': 'chart', 'type': 'line', 'title': f"{a['metric']} {a['app']} و ناهنجاری‌ها", 'unit': unit,
                        'series': [{'name': a['metric'], 'points': [[t, round(v, 3)] for t, v in s[::step]]},
                                   {'name': 'ناهنجاری', 'points': [[x['ts'], x['value']] for x in an]}]}}


@spec('resource_forecast', 'در حال پیش‌بینی روند حافظه/CPU…', 'read',
      'Linear trend of memory (or cpu) vs the plan limit: slope per hour, fit quality, leak verdict and "hits the limit in ~X hours" forecast.',
      {**APP, 'metric': {'type': 'string', 'enum': ['memory', 'cpu']}, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 72}}, ['app'])
async def resource_forecast_tool(a, ctx):
    m = a.get('metric') or 'memory'
    (plan, lim), s = await asyncio.gather(plan_limits(a['app']), series(a['app'], m, int(a.get('hours') or 6)))
    f = forecast_limit(s, lim[m])
    msg = None
    if f.get('hours_to_limit') is not None and f['verdict'] in ('leak_likely', 'slow_rise'):
        msg = f"با این روند، {'حافظه' if m == 'memory' else 'CPU'} حدود {f['hours_to_limit']} ساعت دیگر به سقف پلن {plan} می‌رسد."
    return {'app': a['app'], 'metric': m, 'plan': plan, 'unit': 'MB' if m == 'memory' else 'cores', **f, 'summary_fa': msg}


@spec('deploy_impact', 'در حال مقایسه‌ی خطاها قبل و بعد از استقرارها…', 'read',
      'Deploy <-> error correlation: for each recent READY release (incl. env updates) compare 5xx rate, tracebacks and latency in the logs '
      'before vs after it (significance-tested) and name the suspect release.',
      {**APP, 'releases': {'type': 'integer', 'minimum': 1, 'maximum': 5}, 'window_minutes': {'type': 'integer', 'minimum': 5, 'maximum': 180}}, ['app'], timeout=120)
async def deploy_impact_tool(a, ctx):
    return await deploy_impact(a['app'], int(a.get('releases') or 3), int(a.get('window_minutes') or 30))


@spec('cost_forecast', 'در حال پیش‌بینی هزینه و اعتبار…', 'read',
      'Credit forecast: hours/days until the balance runs out at the current run-rate, the observed burn from balance history, and budget use for an optional monthly budget.',
      {'monthly_budget_toman': {'type': 'integer', 'minimum': 1000}})
async def cost_forecast_tool(a, ctx):
    hist, usage, bill = await asyncio.gather(balance_history(), liara.usage_report(), liara.billing())
    bal = (bill.get('team') or bill.get('user') or {}).get('balance')
    out = burn(bal, usage.get('totalHourlyPrice') or 0, [tuple(x) for x in hist], a.get('monthly_budget_toman'))
    out['top_apps'] = sorted([{'app': p['project_id'], 'toman_per_day': round((p.get('totalHourlyPrice') or 0) * 24)} for p in usage.get('projects', [])],
                             key=lambda x: -x['toman_per_day'])[:6]
    return out


@spec('slo_report', 'در حال محاسبه‌ی دسترس‌پذیری و SLO…', 'read',
      'Availability/SLO of an app from Negahban\'s probe history (~1/min): availability %, error-budget used, burn rates (1h, 6h), latency p50/p95, downtime and worst outages.',
      {**APP, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 72}, 'target': {'type': 'number', 'minimum': 90, 'maximum': 99.99}}, ['app'])
async def slo_report_tool(a, ctx):
    from .. import store
    r = slo(store.samples(a['app'], int(a.get('hours') or 24) * 3600), float(a.get('target') or 99.5))
    return {'app': a['app'], **r} if r.get('samples') else {'app': a['app'], 'samples': 0, 'note': 'no probe history yet (the orchestrator records one sample per minute)'}
