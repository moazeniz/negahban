"""Scripted runbooks: ordered evidence gathering with the diag modules, then rules that turn evidence into findings, ranked
hypotheses and recommended actions. Actions are never executed here: with propose=true the top ones become approval
cards through the core approval system (approvals.propose), otherwise they are returned as suggestions for the agent.
Runbooks: oom, crash_loop, slow_responses, build_failure, missing_env, disk_full, tls_problem, dependency_outage; 'auto' triages first."""
import asyncio, re, time
from .. import plans
from ..liara import LiaraError, liara
from . import analytics, liara_data, logintel, probes
from .common import DiagError, spec
from .exec import run_checks

RUNBOOKS = {'oom': 'کمبود حافظه / OOM', 'crash_loop': 'کرش و ری‌استارت پیاپی', 'slow_responses': 'کندی پاسخ‌ها', 'build_failure': 'شکست بیلد',
            'missing_env': 'متغیر محیطی گمشده', 'disk_full': 'پر شدن دیسک', 'tls_problem': 'مشکل TLS/SSL', 'dependency_outage': 'قطعی سرویس وابسته'}


class Run:
    def __init__(self, app, name):
        self.app, self.name, self.t0 = app, name, time.time()
        self.evidence, self.findings, self.hyp, self.actions = {}, [], [], []

    async def step(self, key, title_fa, coro, timeout=60):
        t = time.perf_counter()
        try:
            res = await asyncio.wait_for(coro, timeout)
            self.evidence[key] = {'title': title_fa, 'ok': True, 'ms': round((time.perf_counter() - t) * 1000), 'data': res}
        except Exception as e:  # one failed step never aborts the runbook
            msg = e.fa() if isinstance(e, LiaraError) else f'{type(e).__name__}: {str(e)[:200]}'
            self.evidence[key] = {'title': title_fa, 'ok': False, 'ms': round((time.perf_counter() - t) * 1000), 'error': msg}
            res = None
        return res

    def d(self, key):
        e = self.evidence.get(key) or {}
        return e.get('data') if e.get('ok') else None

    def find(self, text_fa, severity='info', **facts):
        self.findings.append({'severity': severity, 'text': text_fa, **facts})

    def hypo(self, hid, title_fa, confidence, evidence, actions=()):
        self.hyp.append({'id': hid, 'title': title_fa, 'confidence': round(min(confidence, 0.99), 2), 'evidence': evidence[:6]})
        for a in actions:
            self.actions.append({**a, 'for': hid, 'confidence': round(min(confidence, 0.99), 2)})

    def result(self):
        self.hyp.sort(key=lambda h: -h['confidence'])
        seen, acts = set(), []
        for a in sorted(self.actions, key=lambda a: -a['confidence']):
            k = (a.get('tool'), str(a.get('args')))
            if k not in seen:
                seen.add(k)
                acts.append(a)
        return {'app': self.app, 'runbook': self.name, 'title': RUNBOOKS.get(self.name, self.name), 'seconds': round(time.time() - self.t0, 1),
                'findings': self.findings, 'hypotheses': self.hyp[:6], 'recommended_actions': acts[:6],
                'steps': [{'key': k, 'title': v['title'], 'ok': v['ok'], 'ms': v['ms'], **({'error': v['error']} if not v['ok'] else {})} for k, v in self.evidence.items()],
                'evidence': {k: v.get('data') for k, v in self.evidence.items() if v['ok']}}


async def _logs(app, minutes):
    return logintel.analyze(await logintel.fetch(app, minutes, None, 1500))


async def _next_plan(app):
    info = await liara.app(app)
    cur = info.get('planID')
    await plans.catalogue()
    nxt = plans.ORDER[plans.ORDER.index(cur) + 1] if cur in plans.ORDER and plans.ORDER.index(cur) + 1 < len(plans.ORDER) else None
    return {'plan': cur, 'next_plan': nxt, 'bundle': info.get('bundlePlanID')}  # never the raw app object (it carries env values)


# ---------------------------------------------------------------- runbooks
async def rb_oom(r, minutes):
    ev, mem, fc, lg = await asyncio.gather(
        r.step('events', 'رویدادهای کانتینر', liara_data.events_timeline(r.app, max(1, minutes // 60) + 3)),
        r.step('memory', 'حافظه داخل کانتینر', run_checks(r.app, ['memory', 'processes'], {'limit': 6})),
        r.step('forecast', 'روند حافظه', _forecast(r.app, 'memory', 6)),
        r.step('logs', 'تحلیل لاگ', _logs(r.app, minutes)))
    plan = await r.step('plan', 'پلن فعلی', _next_plan(r.app))
    m = (mem or {}).get('memory') or {}
    ooms = (ev or {}).get('oom', 0) + (m.get('oom_kills') or 0) + len((lg or {}).get('oom') or [])
    util = m.get('util')
    if ev and ev.get('oom'):
        r.find(f"{ev['oom']} رویداد OOM در بازه‌ی اخیر ثبت شده", 'critical', oom_events=ev['oom'])
    if m.get('oom_kills'):
        r.find(f"cgroup از آغاز کانتینر {m['oom_kills']} بار پروسه‌ای را به‌خاطر حافظه کشته است", 'critical')
    if util is not None:
        r.find(f"مصرف حافظه‌ی فعلی {round(util * 100)}٪ سقف ({m.get('usage_mb')} از {m.get('limit_mb')} مگابایت)", 'warn' if util > .8 else 'info')
    procs = ((mem or {}).get('processes') or {}).get('processes') or []
    if procs:
        top = procs[0]
        r.find(f"بزرگ‌ترین پروسه: {top['name']} با {top['rss_mb']} مگابایت", 'info', process=top)
    cur, nxt = (plan or {}).get('plan'), (plan or {}).get('next_plan')
    if fc and fc.get('verdict') == 'leak_likely':
        r.hypo('memory_leak', 'نشت حافظه: مصرف به‌طور پیوسته بالا می‌رود', 0.75 + 0.2 * fc.get('r2', 0) * 0.5,
               [f"شیب {fc['slope_per_hour']} مگابایت/ساعت، r²={fc['r2']}", f"رسیدن به سقف در ~{fc.get('hours_to_limit')} ساعت"],
               [{'tool': 'restart_app', 'args': {'app': r.app, 'reason': 'mitigate memory leak (runbook oom)'}, 'why': 'راه‌حل موقت تا رفع نشت در کد'}])
    if (util or 0) >= 0.85 or (fc and fc.get('verdict') == 'near_limit'):
        r.hypo('plan_too_small', 'پلن برای بار فعلی کوچک است (مصرف پایدار نزدیک سقف)', 0.7 if ooms else 0.55,
               [f"مصرف {round((util or fc.get('util') or 0) * 100)}٪ سقف"],
               [{'tool': 'resize_app', 'args': {'app': r.app, 'plan': nxt, 'reason': 'memory near plan limit (runbook oom)'}, 'why': f'ارتقا از {cur} به {nxt}'}] if nxt else [])
    if ooms and (util or 0) < 0.6 and not (fc and fc.get('verdict') == 'leak_likely'):
        r.hypo('memory_spike', 'جهش ناگهانی حافظه در یک درخواست/کار سنگین (نه مصرف پایدار)', 0.5,
               ['OOM با مصرف پایه‌ی پایین', *[f"مسیر پرترافیک: {x['route']}" for x in ((lg or {}).get('busiest_routes') or [])[:2]]],
               [{'tool': 'log_insights', 'args': {'app': r.app, 'minutes': minutes}, 'why': 'یافتن درخواستی که هم‌زمان با OOM اجرا شده'}])
    if not r.hyp:
        r.hypo('no_oom', 'نشانه‌ای از کمبود حافظه دیده نشد', 0.6, [f"OOM: {ooms}", f"مصرف: {util}"])


async def _forecast(app, metric, hours):
    (plan, lim), s = await asyncio.gather(analytics.plan_limits(app), analytics.series(app, metric, hours))
    return analytics.forecast_limit(s, lim[metric])


async def rb_crash_loop(r, minutes):
    ev, lg, rel, inside = await asyncio.gather(
        r.step('events', 'رویدادهای کانتینر', liara_data.events_timeline(r.app, 6)),
        r.step('logs', 'تحلیل لاگ', _logs(r.app, minutes)),
        r.step('release', 'آخرین نسخه', liara_data.release_details(r.app)),
        r.step('inside', 'بررسی داخل کانتینر', run_checks(r.app, ['ports', 'env_names', 'memory'])))
    raw = await r.step('missing_env', 'جست‌وجوی متغیر گمشده', _missing_env_from_logs(r.app, minutes))
    restarts = (ev or {}).get('unplanned_restarts', 0) + (ev or {}).get('crashes', 0)
    if ev:
        r.find(f"در ۶ ساعت اخیر {ev.get('crashes', 0)} کرش (die)، {ev.get('oom', 0)} OOM و {ev.get('unplanned_restarts', 0)} ری‌استارت ناخواسته", 'critical' if restarts >= 3 else 'info')
    if lg and lg.get('restart_loop'):
        r.find('لاگ‌ها نشان می‌دهد برنامه چند بار پشت سر هم بالا آمده (حلقه‌ی ری‌استارت)', 'critical')
    if raw:
        r.hypo('missing_env', f"متغیر محیطی لازم تنظیم نشده: {', '.join(raw)}", 0.85, [f'KeyError/missing در لاگ برای {k}' for k in raw],
               [{'tool': 'set_env', 'args': {'app': r.app, 'key': k, 'restore': True}, 'why': 'بازگرداندن آخرین مقدار شناخته‌شده'} for k in raw[:2]])
    groups = (lg or {}).get('error_groups') or []
    if groups and restarts:
        g = groups[0]
        r.hypo('startup_exception', f"استثنای تکرارشونده هنگام اجرا: {g['error_type']}", 0.6 + min(0.3, g['count'] / 50),
               [g['fingerprint'], (g['message'] or '')[:120]], [{'tool': 'propose_code_fix', 'args': {'app': r.app, 'hint': g['fingerprint']}, 'why': 'وصله با تست در sandbox'}])
    if rel and rel.get('is_current') and restarts:
        created = rel.get('finished_at') or rel.get('created_at')
        r.hypo('bad_release', f"نسخه‌ی جدید {rel.get('tag')} پس از استقرار ناپایدار است", 0.55, [f"استقرار {created}"],
               [{'tool': 'rollback_release', 'args': {'app': r.app}, 'why': 'بازگشت به آخرین نسخه‌ی سالم'}])
    ports = (((inside or {}).get('ports') or {}).get('listening')) or []
    if rel and rel.get('port') and ports and rel['port'] not in [p['port'] for p in ports]:
        r.hypo('port_mismatch', f"برنامه روی پورت {[p['port'] for p in ports]} گوش می‌دهد ولی لیارا پورت {rel['port']} را انتظار دارد", 0.8,
               ['health check لیارا به پورت اشتباه وصل می‌شود'], [{'tool': None, 'manual': f"پورت برنامه را {rel['port']} کنید یا پورت لیارا را به‌روز کنید"}])
    if (ev or {}).get('oom'):
        r.hypo('oom_kill', 'کرش‌ها به‌خاطر کمبود حافظه است (OOM)', 0.7, [f"{ev['oom']} رویداد oom"], [{'tool': 'run_runbook', 'args': {'app': r.app, 'runbook': 'oom'}, 'why': 'بررسی دقیق حافظه'}])
    if not r.hyp:
        r.hypo('stable', 'حلقه‌ی کرش دیده نشد', 0.6, [f'ری‌استارت‌ها: {restarts}'])


async def _missing_env_from_logs(app, minutes):
    from ..analysis import parse_logs
    lines = await logintel.fetch(app, minutes, None, 1500)
    keys = list((parse_logs(lines).get('missing_env') or {}).keys())
    if not keys:
        return []
    have = set((await liara.envs(app)).keys())
    return [k for k in keys if k not in have][:5]


async def rb_slow(r, minutes):
    reg_url = probes._team_url(r.app, '/')
    pr, lg, inside, dep = await asyncio.gather(
        r.step('probe', 'زمان‌بندی درخواست از بیرون', probes.http_probe(reg_url)),
        r.step('logs', 'کندترین مسیرها', _logs(r.app, minutes)),
        r.step('inside', 'CPU و درخواست از داخل کانتینر', run_checks(r.app, ['cpu', 'memory', 'http_local'])),
        r.step('deploys', 'اثر استقرارها', analytics.deploy_impact(r.app, 2, 30), timeout=90))
    t = (pr or {}).get('timing') or {}
    cpu = (inside or {}).get('cpu') or {}
    loc = (inside or {}).get('http_local') or {}
    if t:
        r.find(f"از بیرون: DNS {t.get('dns_ms')}ms، اتصال {t.get('connect_ms')}ms، TLS {t.get('tls_ms')}ms، TTFB {t.get('ttfb_ms')}ms، کل {pr.get('total_ms')}ms")
    if loc.get('ms') is not None:
        r.find(f"از داخل کانتینر: {loc.get('ms')}ms (وضعیت {loc.get('status')})")
    if (cpu.get('throttle_ratio') or 0) > 0.2 or (cpu.get('now_cores') and cpu.get('limit_cores') and cpu['now_cores'] / cpu['limit_cores'] > .85):
        np = await _next_plan(r.app)
        cur, nxt = np['plan'], np['next_plan']
        r.hypo('cpu_saturation', 'CPU به سقف پلن رسیده و throttle می‌شود', 0.75, [f"throttle {cpu.get('throttle_ratio')}", f"مصرف {cpu.get('now_cores')} از {cpu.get('limit_cores')} هسته"],
               [{'tool': 'resize_app', 'args': {'app': r.app, 'plan': nxt, 'reason': 'CPU throttling (runbook slow_responses)'}, 'why': f'ارتقا از {cur} به {nxt}'}] if nxt else [])
    if t.get('ttfb_ms') and loc.get('ms') is not None and t['ttfb_ms'] > 4 * max(loc['ms'], 50):
        r.hypo('edge_network', 'کندی بیرون از برنامه است (شبکه/لبه‌ی لیارا/DNS)، نه در خود برنامه', 0.55, [f"TTFB بیرون {t['ttfb_ms']}ms در برابر {loc['ms']}ms داخل"])
    slow = (lg or {}).get('slowest_routes') or []
    if slow and (slow[0].get('p95_ms') or 0) > 1500:
        r.hypo('slow_route', f"مسیر {slow[0]['route']} کند است (p95={slow[0]['p95_ms']}ms)", 0.6, [f"{x['route']}: p95 {x['p95_ms']}ms" for x in slow[:3]],
               [{'tool': 'log_insights', 'args': {'app': r.app, 'minutes': minutes}, 'why': 'جزئیات مسیرهای کند'}])
    llm = [x for x in (lg or {}).get('llm') or [] if (x.get('p95_ms') or 0) > 5000]
    if llm:
        r.hypo('slow_upstream_llm', f"فراخوانی مدل زبانی کند است ({llm[0]['model']} p95={llm[0]['p95_ms']}ms)", 0.6, ['تأخیر سرویس بیرونی'])
    reg = next((x for x in (dep or {}).get('releases') or [] if x['verdict'] in ('slower', 'regression')), None)
    if reg:
        r.hypo('deploy_regression', f"پس از نسخه‌ی {reg['release']} پاسخ‌ها کندتر/خطادارتر شده", 0.65, [f"p95 {reg['p95_before_ms']}→{reg['p95_after_ms']}ms، p={reg['p_latency']}"],
               [{'tool': 'rollback_release', 'args': {'app': r.app}, 'why': 'بازگشت به نسخه‌ی قبل'}])
    if not r.hyp:
        r.hypo('no_slowness', 'کندی معناداری دیده نشد', 0.5, [f"TTFB {t.get('ttfb_ms')}ms"])


async def rb_build(r, minutes):
    rel = await r.step('release', 'آخرین نسخه و لاگ بیلد', liara_data.release_details(r.app), timeout=90)
    if not rel:
        return
    st, s = rel.get('state'), rel.get('build_summary') or {}
    r.find(f"نسخه {rel.get('tag')}: وضعیت {st}" + (f"، علت: {rel.get('fail_reason')}" if rel.get('fail_reason') else ''), 'critical' if st in ('FAILED', 'TIMEDOUT', 'UNHEALTHY') else 'info')
    errs = ' '.join(s.get('error_lines') or []) + ' ' + ' '.join(rel.get('stderr_lines') or [])
    if st == 'TIMEDOUT':
        r.hypo('build_timeout', 'بیلد از سقف زمانی باندل (۵/۱۰/۲۰ دقیقه) بیشتر شد', 0.85, ['state=TIMEDOUT'],
               [{'tool': None, 'manual': 'کش بیلد را فعال کنید، وابستگی‌های سنگین را کم کنید یا باندل را ارتقا دهید'}])
    if s.get('missing_package'):
        r.hypo('missing_package', f"بسته‌ی {s['missing_package']} پیدا نشد", 0.85, [s['missing_package']],
               [{'tool': 'propose_code_fix', 'args': {'app': r.app, 'hint': f"fix dependency {s['missing_package']}"}, 'why': 'اصلاح نسخه/نام بسته'}])
    if re.search(r'(?i)(timed out|connection (reset|refused)|could not resolve|temporary failure|ReadTimeoutError|ETIMEDOUT|EAI_AGAIN|403 Forbidden)', errs):
        r.hypo('registry_network', 'دسترسی بیلدر به مخزن بسته‌ها (pip/npm/docker hub) قطع یا فیلتر است', 0.7, ['خطای شبکه در لاگ بیلد'],
               [{'tool': None, 'manual': 'build.location را germany کنید یا از mirror داخلی (مثلاً mirror-pypi.runflare.com) استفاده کنید'}])
    if 'disk_not_found' in str(rel.get('fail_reason')):
        r.hypo('disk_not_found', 'دیسکی که در استقرار mount شده وجود ندارد', 0.9, [str(rel.get('fail_reason'))], [{'tool': None, 'manual': 'دیسک را بسازید یا از liara.json حذف کنید'}])
    if re.search(r'(?i)dockerfile|COPY failed|failed to (compute cache key|solve)|unknown instruction', errs):
        r.hypo('dockerfile_error', 'خطا در Dockerfile (مسیر COPY یا دستور نامعتبر)', 0.7, [x[:150] for x in (s.get('error_lines') or [])[-2:]],
               [{'tool': 'propose_code_fix', 'args': {'app': r.app, 'hint': 'Dockerfile build error'}, 'why': 'اصلاح Dockerfile'}])
    if st == 'UNHEALTHY':
        r.hypo('healthcheck_failed', 'بیلد موفق بود ولی health check نسخه‌ی جدید رد شد؛ نسخه‌ی قبلی همچنان سرویس می‌دهد', 0.8, ['state=UNHEALTHY'],
               [{'tool': 'run_runbook', 'args': {'app': r.app, 'runbook': 'crash_loop'}, 'why': 'بررسی علت بالا نیامدن برنامه'}])
    if not r.hyp and st in ('FAILED',):
        r.hypo('build_error', 'بیلد با خطا متوقف شد', 0.5, (s.get('error_lines') or [])[-3:], [{'tool': 'web_search_error', 'args': {'error': (s.get('error_lines') or ['build failed'])[-1][:300]}, 'why': 'جست‌وجوی خطای ناشناخته'}])
    if not r.hyp:
        r.hypo('build_ok', 'آخرین بیلد مشکلی ندارد', 0.6, [f'state={st}'])


async def rb_missing_env(r, minutes):
    keys, inside, info = await asyncio.gather(
        r.step('logs', 'جست‌وجوی KeyError و پیام‌های missing در لاگ', _missing_env_from_logs(r.app, minutes)),
        r.step('inside', 'نام متغیرهای داخل کانتینر', run_checks(r.app, ['env_names'])),
        r.step('liara_env', 'نام متغیرهای تعریف‌شده در لیارا', _env_keys(r.app)))
    names = set(((inside or {}).get('env_names') or {}).get('names') or [])
    empty = ((inside or {}).get('env_names') or {}).get('empty') or []
    if empty:
        r.find(f"متغیرهای خالی در کانتینر: {', '.join(empty[:8])}", 'warn')
    from .. import store
    snap = store.kv_get(f'envsnap:{r.app}', {}) or {}
    lost = [k for k in snap if info is not None and k not in info]
    for k in keys or []:
        r.hypo(f'missing_{k}', f'متغیر {k} تعریف نشده ولی برنامه به آن نیاز دارد', 0.9 if k not in names else 0.5, [f'خطای لاگ برای {k}'],
               [{'tool': 'set_env', 'args': {'app': r.app, 'key': k, 'restore': True} if k in snap else {'app': r.app, 'key': k}, 'why': 'تنظیم متغیر (مقدار قبلی محفوظ است)' if k in snap else 'مالک باید مقدار را وارد کند'}])
    if lost:
        r.hypo('env_removed', f"این متغیرها قبلاً بودند و حذف شده‌اند: {', '.join(lost[:6])}", 0.7, ['مقایسه با آخرین snapshot نگهبان'],
               [{'tool': 'set_env', 'args': {'app': r.app, 'key': k, 'restore': True}, 'why': 'بازگرداندن مقدار قبلی'} for k in lost[:2]])
    if not r.hyp:
        r.hypo('env_ok', 'نشانه‌ای از متغیر گمشده نیست', 0.6, [f'{len(names)} متغیر در کانتینر'])


async def _env_keys(app):
    return sorted((await liara.envs(app)).keys())


async def rb_disk(r, minutes):
    inside, dk = await asyncio.gather(
        r.step('inside', 'فضای mountها و پوشه‌ی داده', run_checks(r.app, ['disk', 'data_usage', 'sqlite_check'], {'skip_check': True})),
        r.step('disks', 'دیسک‌های لیارا و پشتیبان‌ها', liara_data.disks(r.app)))
    mounts = ((inside or {}).get('disk') or {}).get('mounts') or []
    full = [m for m in mounts if (m.get('use_pct') or 0) >= 85 and m['mount'] not in ('/dev/shm',)]
    for m in mounts:
        if (m.get('use_pct') or 0) >= 70:
            r.find(f"{m['mount']}: {m['use_pct']}٪ پر ({m.get('avail_mb')} مگابایت آزاد)", 'critical' if m['use_pct'] >= 90 else 'warn')
    du = (inside or {}).get('data_usage') or {}
    if du.get('largest_files'):
        r.find('بزرگ‌ترین فایل‌ها: ' + '، '.join(f"{x['path']} ({x['mb']}MB)" for x in du['largest_files'][:4]))
    dbs = ((inside or {}).get('sqlite_check') or {}).get('databases') or []
    wal = [d for d in dbs if (d.get('wal_mb') or 0) > max(50, (d.get('size_mb') or 0) * 0.5)]
    if wal:
        r.hypo('sqlite_wal_growth', f"فایل WAL دیتابیس {wal[0]['path']} بزرگ شده ({wal[0]['wal_mb']}MB)", 0.65, ['checkpoint انجام نمی‌شود یا اتصال بازِ طولانی وجود دارد'],
               [{'tool': None, 'manual': 'PRAGMA wal_checkpoint(TRUNCATE) را در برنامه اجرا کنید و اتصال‌ها را ببندید'}])
    if any(m['mount'] == '/tmp' for m in full):
        r.hypo('tmp_full', 'پوشه‌ی /tmp (سقف ۱۰۰ مگابایت در لیارا) پر شده', 0.75, ['use_pct ≥ 85'], [{'tool': 'restart_app', 'args': {'app': r.app, 'reason': 'clear /tmp'}, 'why': 'ری‌استارت /tmp را خالی می‌کند'}])
    if any(m['mount'] not in ('/tmp',) for m in full):
        m = next(m for m in full if m['mount'] != '/tmp')
        r.hypo('disk_full', f"دیسک {m['mount']} تقریباً پر است", 0.8, [f"{m['use_pct']}٪"],
               [{'tool': None, 'manual': 'دیسک را در کنسول لیارا بزرگ کنید (POST /v1/projects/{app}/disks/{disk}/resize) یا داده‌های قدیمی را پاک کنید'}])
    if not r.hyp:
        r.hypo('disk_ok', 'فضای دیسک مشکلی ندارد', 0.6, [f"{m['mount']}: {m.get('use_pct')}٪" for m in mounts[:3]])


async def rb_tls(r, minutes, domain=None):
    doms = await r.step('domains', 'دامنه‌ها و وضعیت گواهی در لیارا', liara_data.domains_health(r.app))
    hosts = [domain] if domain else [d['name'] for d in (doms or {}).get('domains') or []][:3] or [f'{r.app}.liara.run']
    for h in hosts:
        t, dns = await asyncio.gather(r.step(f'tls_{h}', f'گواهی {h}', probes.tls_check(h)), r.step(f'dns_{h}', f'DNS {h}', probes.dns_lookup(h, ('A', 'CNAME', 'CAA'))))
        cert = (t or {}).get('cert') or {}
        if t and not t.get('ok'):
            r.hypo(f'tls_invalid_{h}', f"گواهی {h} معتبر نیست: {t.get('verify_error') or t.get('error')}", 0.85, [str(cert.get('issuer')), f"انقضا: {cert.get('not_after')}"],
                   [{'tool': 'enable_ssl', 'args': {'domain': h}, 'why': 'درخواست گواهی رایگان'}] if not h.endswith('.liara.run') else [])
        elif cert.get('days_left') is not None and cert['days_left'] < 14:
            r.hypo(f'tls_expiring_{h}', f"گواهی {h} تا {cert['days_left']} روز دیگر منقضی می‌شود", 0.8, [f"صادرکننده {cert.get('issuer')}"],
                   [{'tool': 'enable_ssl', 'args': {'domain': h}, 'why': 'تمدید گواهی'}])
        elif cert:
            r.find(f"{h}: گواهی معتبر از {cert.get('issuer')}، {cert.get('days_left')} روز باقی‌مانده", 'info')
        if dns and not (dns['records'].get('A') or dns['records'].get('CNAME')):
            r.hypo(f'dns_missing_{h}', f'{h} رکورد A/CNAME ندارد', 0.8, [str(dns['rcode'])], [{'tool': 'domain_setup_guide', 'args': {}, 'why': 'رکوردهای لازم را ببینید'}])
    for d in (doms or {}).get('domains') or []:
        if d.get('status') != 'ACTIVE':
            r.hypo(f"domain_pending_{d['name']}", f"دامنه‌ی {d['name']} هنوز تأیید نشده ({d.get('status')})", 0.7, ['رکوردهای CNAME/TXT تنظیم نشده'],
                   [{'tool': 'domain_setup_guide', 'args': {'domain_id': d['_id']}, 'why': 'رکوردهای DNS لازم'}])
    if not r.hyp:
        r.hypo('tls_ok', 'مشکل TLS دیده نشد', 0.6, hosts)


UPSTREAM = re.compile(r'https?://([a-z0-9.-]+\.[a-z]{2,})', re.I)


async def rb_dependency(r, minutes):
    lines = await r.step('logs_raw', 'لاگ‌های اخیر', logintel.fetch(r.app, minutes, None, 1500))
    lg = logintel.analyze(lines or [])
    r.evidence['logs'] = {'title': 'تحلیل لاگ', 'ok': True, 'ms': 0, 'data': logintel.compact(lg)}
    r.evidence.pop('logs_raw', None)
    conn = re.compile(r'(?i)(connection (refused|reset|error)|timed? ?out|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|Name or service not known|503 Service|502 Bad Gateway|circuit.*open|upstream)')
    hosts, hits = {}, 0
    for x in lines or []:
        s = x.get('line', '')
        if conn.search(s):
            hits += 1
            for h in UPSTREAM.findall(s):
                hosts[h.lower()] = hosts.get(h.lower(), 0) + 1
            ev = logintel.parse_line(s)
            if ev.get('upstream'):
                hosts[str(ev['upstream'])[:80]] = hosts.get(str(ev['upstream'])[:80], 0) + 1
    if hits:
        r.find(f'{hits} خطای اتصال/تایم‌اوت به سرویس‌های بیرونی در {minutes} دقیقه‌ی اخیر', 'warn' if hits < 10 else 'critical', upstreams=hosts)
    for h, n in sorted(hosts.items(), key=lambda x: -x[1])[:3]:
        if not re.match(r'^[a-z0-9.-]+$', h):
            continue
        p = await r.step(f'probe_{h}', f'دسترسی به {h}', probes.http_probe(f'https://{h}/', timeout=10))
        last = ((p or {}).get('chain') or [{}])[-1]
        down = not p or bool(last.get('error')) or (p.get('status') or 0) >= 500
        r.hypo(f'upstream_{h}', f"سرویس وابسته‌ی {h} {'در دسترس نیست' if down else 'پاسخ می‌دهد ولی برنامه خطا می‌گیرد'}", 0.75 if down else 0.45,
               [f'{n} خطا در لاگ', f"probe: {(p or {}).get('status') or ((p or {}).get('chain') or [{}])[-1].get('error')}"],
               [{'tool': None, 'manual': 'برای این سرویس retry با backoff و circuit breaker و fallback فعال کنید؛ از ایران ممکن است فیلتر باشد'}])
    if lg.get('llm') and any(x['errors'] for x in lg['llm']):
        bad = max(lg['llm'], key=lambda x: x['errors'])
        r.hypo('llm_provider', f"خطای سرویس مدل زبانی {bad['model']} ({bad['errors']} خطا از {bad['calls']} فراخوانی)", 0.6, ['llm.call با خطا'])
    if not r.hyp:
        r.hypo('deps_ok', 'نشانه‌ای از قطعی سرویس وابسته نیست', 0.55, [f'{hits} خطای اتصال'])


async def triage(app, minutes):
    """Cheap signals -> which runbook fits best."""
    ev, rel, pr = await asyncio.gather(liara_data.events_timeline(app, 3), liara.releases(app, 1), probes.http_probe(probes._team_url(app, '/')), return_exceptions=True)
    ok = lambda x: not isinstance(x, Exception)
    last = ((rel.get('releases') or [{}])[0] if ok(rel) else {}) or {}
    if ok(ev) and ev.get('oom'):
        return 'oom', 'رویداد OOM'
    if last.get('state') in ('FAILED', 'TIMEDOUT', 'UNHEALTHY'):
        return 'build_failure', f"آخرین نسخه {last.get('state')}"
    if ok(ev) and (ev.get('crashes', 0) >= 2 or ev.get('unplanned_restarts', 0) >= 2):
        return 'crash_loop', 'کرش/ری‌استارت پیاپی'
    if ok(pr):
        ch = (pr.get('chain') or [{}])[-1]
        if ch.get('error') == 'tls_verify_failed':
            return 'tls_problem', 'خطای گواهی'
        if (pr.get('total_ms') or 0) > 3000:
            return 'slow_responses', f"پاسخ در {pr['total_ms']}ms"
        if (pr.get('status') or 0) >= 500:
            return 'dependency_outage', f"وضعیت {pr['status']}"
    return 'slow_responses' if not ok(pr) else 'dependency_outage', 'بدون نشانه‌ی قطعی؛ بررسی کلی'


RUNNERS = {'oom': rb_oom, 'crash_loop': rb_crash_loop, 'slow_responses': rb_slow, 'build_failure': rb_build, 'missing_env': rb_missing_env,
           'disk_full': rb_disk, 'tls_problem': rb_tls, 'dependency_outage': rb_dependency}


async def run(app, runbook, minutes=60, domain=None):
    why = None
    if runbook == 'auto':
        runbook, why = await triage(app, minutes)
    if runbook not in RUNNERS:
        raise DiagError(f'unknown runbook {runbook}')
    r = Run(app, runbook)
    if runbook == 'tls_problem':
        await rb_tls(r, minutes, domain)
    else:
        await RUNNERS[runbook](r, minutes)
    out = r.result()
    if why:
        out['triage'] = why
    return out


async def propose_actions(result, ctx, limit=2):
    """Turn the top recommended actions that map to registered sensitive tools into approval cards (never executes them)."""
    from ..approvals import propose
    from ..tools import TOOLS
    cards = []
    for a in result['recommended_actions']:
        t = TOOLS.get(a.get('tool') or '')
        if not t or t['risk'] != 'sensitive' or not all(v is not None for v in (a.get('args') or {}).values()):
            continue
        try:
            cards.append(await propose(a['tool'], a['args'], ctx, why=f"runbook {result['runbook']}: {a.get('why', '')}", incident_id=ctx.get('incident_id')))
        except Exception as e:  # e.g. guard/lock or validation; reported, not raised
            cards.append({'tool': a['tool'], 'error': str(e)[:200]})
        if len(cards) >= limit:
            break
    return cards


@spec('run_runbook', 'در حال اجرای دستورالعمل عیب‌یابی…', 'safe',
      'Run a scripted SRE runbook that gathers evidence step by step (events, metrics, logs, in-container checks, probes, releases) and returns '
      'findings, ranked hypotheses and recommended actions. runbook: auto (triage first) | oom | crash_loop | slow_responses | build_failure | '
      'missing_env | disk_full | tls_problem | dependency_outage. propose=true turns the top sensitive actions into approval cards (nothing is executed).',
      {'app': {'type': 'string'}, 'runbook': {'type': 'string', 'enum': ['auto', *RUNBOOKS]}, 'minutes': {'type': 'integer', 'minimum': 5, 'maximum': 720},
       'domain': {'type': 'string', 'description': 'tls_problem: domain to check'}, 'propose': {'type': 'boolean'}}, ['app'], timeout=170)
async def run_runbook_tool(a, ctx):
    res = await run(a['app'], a.get('runbook') or 'auto', int(a.get('minutes') or 60), a.get('domain'))
    if a.get('propose'):
        res['proposals'] = await propose_actions(res, ctx)
    ev = res.pop('evidence')
    res['evidence_keys'] = list(ev)
    res['evidence'] = {k: (logintel.compact(v) if isinstance(v, dict) and 'error_groups' in v and 'errors_per_minute' in v else v) for k, v in ev.items()}
    return res
