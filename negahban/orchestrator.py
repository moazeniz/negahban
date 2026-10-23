"""Always-on orchestrator. Every cycle (default 60 s) for each monitored app: health probe + metrics sample, new log
lines, container events and release state -> detectors -> incidents -> automatic diagnosis -> autopilot (only what
the owner pre-approved in the policy, within budget, with cooldowns/hysteresis) or an approval card for anything else.
Incidents auto-resolve after 3 clean cycles. Scale-down after sustained low usage."""
import asyncio, logging, time
from . import bus, plans, store
from .analysis import parse_logs, probe, restarts_from_events
from .config import settings
from .liara import LiaraError, liara

log = logging.getLogger('negahban.orch')
GB = 1024 ** 3
STATE = {'running': False, 'last_cycle': None, 'cycles': 0, 'last_error': None, 'apps': {}, 'balance': None}
_locks = {}
SEV = {'down': 'critical', 'errors': 'high', 'build_failed': 'medium', 'memory': 'high', 'cpu': 'medium', 'slow': 'medium', 'crash': 'high', 'budget': 'medium'}
TITLE = {'down': 'سایت از دسترس خارج شده است', 'errors': 'خطای سرور (5xx) در پاسخ‌ها', 'build_failed': 'استقرار نسخه‌ی جدید شکست خورد',
         'memory': 'حافظه نزدیک سقف پلن / OOM', 'cpu': 'پردازنده در سقف پلن', 'slow': 'کندی پاسخ‌ها', 'crash': 'کرش و ری‌استارت مکرر کانتینر', 'budget': 'اعتبار حساب رو به اتمام'}


def cool(app, key, seconds):
    t = store.kv_get(f'cool:{app}:{key}', 0)
    return time.time() - t >= seconds


def mark(app, key):
    store.kv_set(f'cool:{app}:{key}', time.time())


async def sample(a):
    app = a['name']
    info = await liara.app(app)
    pl = plans.plan_sync(info.get('planID') or 'small-g2')
    url = a['url'].rstrip('/') + (a.get('health_path') or '/')
    summ, pr = await asyncio.gather(liara.metrics_summary(app), probe(url, timeout=20) if info.get('scale', 1) else asyncio.sleep(0, {'ok': False, 'status': 0, 'latency_ms': 0, 'error': 'scaled to 0'}))
    s = {'ts': time.time(), 'cpu': summ['cpu'], 'mem': summ['mem'] / GB, 'cpu_limit': pl['cpu'], 'mem_limit': pl['ram'], 'latency_ms': pr.get('latency_ms'),
         'status': pr.get('status'), 'ok': pr.get('ok')}
    store.add_sample(app, **s)
    return info, pl, s, pr


def window(app, minutes):
    return store.samples(app, minutes * 60 + 20)


def sustained(rows, minutes, pred):
    need = max(3, int(minutes * 60 / max(settings.orchestrator_interval, 10) * 0.7))
    return len(rows) >= need and all(pred(r) for r in rows[-need:])


async def cycle_app(a):
    app = a['name']
    pol = store.policy(app)
    st = STATE['apps'].setdefault(app, {'clean': {}, 'fails': 0})
    info, pl, s, pr = await sample(a)
    now = time.time()
    found = {}
    actx = {'L': None, 'R': None}  # inputs for the owner-editable alert rules (alerts.py)
    # ---- detectors
    if info.get('scale') == 0:
        st['fails'] = 0
    elif not pr.get('ok'):
        st['fails'] += 1
        if st['fails'] >= 2:
            # an HTTP 5xx with a body means the app runs but fails => 'errors'; no response / gateway errors => 'down'
            if pr.get('status') in (0, 502, 503, 504) or pr.get('error'):
                found['down'] = f"HTTP {pr.get('status')} {pr.get('error') or ''}".strip()
            else:
                found['errors'] = f"صفحه‌ی سلامت HTTP {pr.get('status')} برمی‌گرداند"
    else:
        st['fails'] = 0
        rows = window(app, 3)
        if sum(1 for r in rows[-2:] if (r['latency_ms'] or 0) > 2500) >= 2:
            found['slow'] = f"{pr.get('latency_ms')}ms"
    last_ts = store.kv_get(f'logts:{app}', now - 180)
    try:
        lines = await liara.logs(app, start=last_ts + 0.001, end=now, max_lines=800)
        L = parse_logs(lines)
        actx['L'] = L; actx['lines'] = lines
        if lines:
            store.kv_set(f'logts:{app}', lines[-1]['ts'])
        if L['tracebacks'] or L['errors_5xx'] >= 3:
            found['errors'] = (found.get('errors', '') + ' — ' if found.get('errors') else '') + f"{L['errors_5xx']} پاسخ 5xx، {sum(t['count'] for t in L['tracebacks'])} traceback"
        if L['oom']:
            found['memory'] = 'OOM در لاگ'
        if L['missing_env'] and (L['restart_loop'] or not pr.get('ok')):
            found['crash'] = 'متغیر محیطی گم‌شده: ' + ', '.join(L['missing_env'])
    except LiaraError:
        pass
    try:
        evs = await liara.events(app, start=store.kv_get(f'evts:{app}', now - 300), end=now)
        if evs:
            store.kv_set(f'evts:{app}', max((e.get('time') or now) for e in evs) + 1)
        R = restarts_from_events(evs)
        actx['R'] = R
        if R['oom']:
            found['memory'] = f"{R['oom']} رویداد OOM (کانتینر به‌خاطر کمبود حافظه کشته شد)"
        if R['restarts'] >= 2:
            found.setdefault('crash', f"{R['restarts']} ری‌استارت ناخواسته‌ی کانتینر")
    except LiaraError:
        pass
    try:
        rel = (await liara.releases(app, 1)).get('releases', [])
        if rel and rel[0].get('state') in ('FAILED', 'TIMEDOUT', 'UNHEALTHY'):
            if store.kv_get(f'relseen:{app}') != rel[0]['_id']:
                found['build_failed'] = f"ریلیز {rel[0].get('tag')} {rel[0].get('state')}"
                store.kv_set(f'relseen:{app}', rel[0]['_id'])
    except LiaraError:
        pass
    settling = time.time() - (store.kv_get(f'settle:{app}', 0) or 0) < 240  # just restarted/resized/deployed: metrics overlap
    rows = [] if settling else window(app, pol['up_minutes'])
    if sustained(rows, pol['up_minutes'], lambda r: r['cpu_limit'] and r['cpu'] / r['cpu_limit'] >= pol['up_cpu']):
        found['cpu'] = f"CPU {s['cpu'] / pl['cpu'] * 100:.0f}٪ سقف پلن به مدت {pol['up_minutes']} دقیقه"
    if sustained(rows, pol['up_minutes'], lambda r: r['mem_limit'] and r['mem'] / r['mem_limit'] >= pol['up_mem']):
        found.setdefault('memory', f"حافظه {s['mem'] / pl['ram'] * 100:.0f}٪ سقف پلن")
    # predictive: memory rising steadily and projected to hit the plan limit within ~10 minutes (catch leaks before the OOM kill)
    from .analysis import trend
    recent = [] if settling else [(r['ts'], r['mem']) for r in window(app, 6) if r['mem_limit'] and r['ts'] > (store.kv_get(f'settle:{app}', 0) or 0) + 60]
    if len(recent) >= 4:
        tr = trend(recent, pl['ram'])
        if tr.get('util_last', 0) >= 0.55 and tr.get('monotonic_rise', 0) >= 0.75 and 0 < (tr.get('minutes_to_limit') or 99) < 10:
            found.setdefault('memory', f"حافظه {tr['util_last'] * 100:.0f}٪ و رو به رشد؛ حدود {tr['minutes_to_limit']:.0f} دقیقه تا پر شدن (احتمال نشت)")
    st['last'] = {'ts': now, 'plan': info.get('planID'), 'cpu_util': round(s['cpu'] / pl['cpu'], 3), 'mem_util': round(s['mem'] / pl['ram'], 3), 'latency_ms': s['latency_ms'],
                  'ok': s['ok'], 'status': s['status'], 'found': found}
    # ---- owner-editable alert rules (metric + log pattern); deterministic, the LLM is only woken via 'wake'
    try:
        from . import alerts
        L_, R_ = actx['L'] or {}, actx['R'] or {}
        lat = [r['latency_ms'] for r in window(app, 5) if r['latency_ms'] and r['ok']]
        mctx = {'http_5xx_rate': round(100 * L_['errors_5xx'] / L_['requests'], 1) if L_.get('requests', 0) >= 5 else (100.0 if (pr.get('status') or 0) >= 500 else None),
                'latency_p95': sorted(lat)[max(0, int(len(lat) * 0.95) - 1)] if len(lat) >= 3 else None,
                'cpu_pct': round(100 * s['cpu'] / pl['cpu'], 1), 'mem_pct': round(100 * s['mem'] / pl['ram'], 1),
                'restarts': R_.get('restarts'), 'oom': (R_.get('oom') or 0) + len(L_.get('oom') or []), 'down': st['fails'],
                'deploy_failed': 1 if 'build_failed' in found else 0}
        await alerts.evaluate_metrics(app, mctx)
        if actx.get('lines'):
            await alerts.evaluate_lines(app, [{'line': l['line']} for l in actx['lines']], 'logs')
    except Exception as e:
        log.warning('alert rules failed for %s: %s', app, e)
    # ---- incidents
    for kind, detail in found.items():
        inc, new = store.open_incident(app, kind, SEV.get(kind, 'medium'), TITLE.get(kind, kind), f'{kind}', {'trigger': detail})
        st['clean'][kind] = 0
        if new:
            bus.publish('incident', incident=inc)
            store.audit('orchestrator', 'incident_open', app, {'incident': inc['id'], 'kind': kind, 'detail': detail})
            from . import jobs
            jobs.submit('incident.handle', {'app': app, 'incident': inc['id'], 'kind': kind, 'info': {k: info.get(k) for k in ('planID', 'bundlePlanID', 'scale', 'status')},
                                            'pl': pl, 's': s}, idem=f"inc:{inc['id']}", max_attempts=3)
        else:
            store.update_incident(inc['id'], note=None)
    for inc in store.incidents(20, app, open_only=True):
        if inc['kind'] in found or inc['kind'] == 'budget':
            continue
        st['clean'][inc['kind']] = st['clean'].get(inc['kind'], 0) + 1
        if st['clean'][inc['kind']] >= 3:
            inc = store.update_incident(inc['id'], status='resolved', note='سه بررسی پیاپی سالم بود؛ رخداد به‌طور خودکار بسته شد.')
            try:
                from . import memory
                if not store.q("select id from memory where kind='incident' and meta like ?", (f'%{inc["id"]}%',), one=True):
                    memory.remember_incident(inc, 'بدون اقدام، خودبه‌خود برطرف شد')
            except Exception:
                pass
            for p in store.q("select id from proposals where incident_id=? and status='pending'", (inc['id'],)):
                if store.claim_proposal(p['id'], 'pending', 'expired'):  # the problem is gone: withdraw its open cards
                    store.set_proposal(p['id'], result={'note': 'رخداد برطرف شد؛ این پیشنهاد دیگر لازم نیست'})
            bus.publish('incident', incident=inc)
    # ---- autopilot scale-down after sustained low usage
    await maybe_scale_down(app, pol, info, pl)
    bus.publish('tick', app=app, sample=s, plan=info.get('planID'), status=info.get('status'), scale=info.get('scale'), found=list(found))


def _fixes_from(diag):
    for h in diag.get('hypotheses', []):
        for f in h.get('fixes', []):
            yield h, f


async def _job_incident(p):
    inc = store.get_incident(p['incident'])
    if inc and inc['status'] != 'resolved':
        await handle_incident(p['app'], inc, p['kind'], p['info'], p['pl'], p['s'])
    return {'incident': p['incident']}


def _register_jobs():
    from . import jobs
    jobs.handler('incident.handle', max_attempts=3)(_job_incident)


async def handle_incident(app, inc, kind, info, pl, s):
    lock = _locks.setdefault(app, asyncio.Lock())
    async with lock:
        from .diagnose import diagnose
        bus.publish('op', app=app, status='running', text=f"تشخیص خودکار برای «{inc['title']}»…")
        try:
            diag = await diagnose(app, 20, with_llm=True)
        except Exception as e:
            store.update_incident(inc['id'], note=f'تشخیص ناموفق: {e}')
            return
        top = diag['hypotheses'][0] if diag['hypotheses'] else None
        if top:  # one root cause -> one incident: several symptoms (5xx, crash loop, failed release) of the same cause are merged
            for other in store.incidents(20, app, open_only=True):
                otop = ((other.get('diagnosis') or {}).get('hypotheses') or [{}])[0]
                if other['id'] != inc['id'] and otop.get('id') == top['id']:
                    store.update_incident(other['id'], note=f"علامت دیگری از همین مشکل دیده شد ({TITLE.get(kind, kind)}) و در این رخداد ادغام شد.")
                    inc = store.update_incident(inc['id'], status='resolved', diagnosis=diag, title=top['title'],
                                                note=f"همان علتِ رخداد {other['id']} ({top['title']})؛ برای جلوگیری از اقدام تکراری ادغام شد.")
                    bus.publish('incident', incident=inc)
                    return
        inc = store.update_incident(inc['id'], diagnosis=diag, note=f"تشخیص: {top['title']} (اطمینان {top['confidence'] * 100:.0f}٪)" if top else 'تشخیص: علت قطعی پیدا نشد',
                                    title=top['title'] if top else None)
        bus.publish('incident', incident=inc)
        pol = store.policy(app)
        if pol.get('write_lock'):
            store.update_incident(inc['id'], note='این برنامه فقط-پایش است؛ اقدام خودکار یا پیشنهاد تغییر ساخته نمی‌شود. گزارش برای مالک آماده است.')
            return
        await act(app, inc, diag, pol, info, pl, s)


async def act(app, inc, diag, pol, info, pl, s):
    from .approvals import approve, propose
    from .tools import run
    cur = info.get('planID')
    bundle = info.get('bundlePlanID') or 'basic'
    before = {'cpu_util': round(s['cpu'] / pl['cpu'], 3), 'mem_util': round(s['mem'] / pl['ram'], 3), 'latency_ms': s['latency_ms'], 'plan': cur}
    done = False
    for h, f in _fixes_from(diag):
        tool, args = f['tool'], dict(f['args'])
        auto, why_auto = False, ''
        if tool == 'resize_app':
            target = args['plan'] = plans.neighbour(cur, +1) if cur in plans.ORDER else args['plan']
            in_range = plans.within(target, pol['min_plan'], pol['max_plan'])
            day_cost = plans.hourly(target, bundle) * 24
            if not (pol['autopilot'] and pol['resize']):
                why_auto = 'خودران برای تغییر پلن این برنامه خاموش است'
            elif not in_range:
                why_auto = f"پلن {target} خارج از بازه‌ی مجاز خودران ({pol['min_plan']}…{pol['max_plan']}) است"
            elif day_cost > pol['max_toman_day']:
                why_auto = f"سقف بودجه‌ی روزانه ({plans.fmt_toman(pol['max_toman_day'])}) اجازه‌ی ارتقای خودکار نمی‌دهد"
            elif not cool(app, 'resize', 600):
                why_auto = 'تغییر پلن اخیراً انجام شده (دوره‌ی خنک‌سازی ۱۰ دقیقه)'
            else:
                auto, why_auto = True, f"خودران: ارتقا مجاز است (بازه {pol['min_plan']}…{pol['max_plan']}، هزینه‌ی روزانه {plans.fmt_toman(day_cost)} ≤ سقف {plans.fmt_toman(pol['max_toman_day'])})"
        elif tool == 'restart_app':
            auto = pol['autopilot'] and pol['restart_on_crash'] and h['kind'] in ('memory', 'crash', 'latency', 'down') and cool(app, 'restart', 600)
            why_auto = 'خودران: ری‌استارت در کرش/نشت حافظه مجاز است' if auto else ''
        elif tool == 'rollback_release':
            auto = pol['autopilot'] and pol['auto_rollback'] and cool(app, 'rollback', 900)
        elif tool == 'propose_code_fix':
            if not cool(app, 'codefix', 600):
                store.update_incident(inc['id'], note='خودترمیمی در ۱۰ دقیقه‌ی اخیر اجرا شده؛ برای تلاش دوباره از دکمه‌ی «اصلاح خودکار» در همین رخداد استفاده کنید.')
                continue
            mark(app, 'codefix')
            store.update_incident(inc['id'], note='خودترمیمی: در حال آماده‌سازی وصله و اجرای تست‌ها…')
            r = await run('propose_code_fix', {'app': app}, {'origin': 'orchestrator', 'incident_id': inc['id']})
            note = r.get('status') if isinstance(r, dict) else 'error'
            store.update_incident(inc['id'], note=f"خودترمیمی: {note}" + (f" — {r.get('root_cause')}" if isinstance(r, dict) and r.get('root_cause') else ''))
            if isinstance(r, dict) and r.get('status') == 'proposed':
                done = True
                break
            continue
        elif tool in ('set_env', 'scale_app'):
            pass
        ctx = {'origin': 'autopilot' if auto else 'orchestrator', 'actor': 'orchestrator', 'why': (h['title'] + ' — ' + why_auto).strip(' —'),
               'extra': {'before': before, 'hypothesis': h['title'], 'confidence': h['confidence']}}
        prop = await propose(tool, args, ctx, auto=auto, incident_id=inc['id'])
        if prop.get('refused'):
            store.update_incident(inc['id'], note=f"اقدام ممکن نیست: {prop['error']}")
            done = True
            break
        if auto:
            mark(app, tool.split('_')[0])
            store.update_incident(inc['id'], note=f"خودران: {prop['title']} بدون پرسش اجرا شد ({why_auto})")
            await approve(prop['id'], 'autopilot')
        else:
            store.update_incident(inc['id'], note=f"کارت تأیید ساخته شد: {prop['title']}" + (f" — {why_auto}" if why_auto else ''))
        done = True
        break
    if not done:
        store.update_incident(inc['id'], note='اقدام خودکاری برای این رخداد تعریف نشده؛ گزارش تشخیص برای مالک آماده است.')


async def maybe_scale_down(app, pol, info, pl):
    if not (pol['autopilot'] and pol['resize'] and pol['scale_down']) or pol.get('write_lock'):
        return
    cur = info.get('planID')
    if cur not in plans.ORDER or plans.ORDER.index(cur) <= plans.ORDER.index(pol['min_plan']):
        return
    if not cool(app, 'resize', pol['down_minutes'] * 60) or store.incidents(5, app, open_only=True):
        return
    rows = window(app, pol['down_minutes'])
    if not rows or rows[0]['ts'] > time.time() - pol['down_minutes'] * 60 * 0.9:
        return  # not enough history on the current plan yet
    if not all(r['cpu_limit'] and r['cpu'] / r['cpu_limit'] < pol['down_cpu'] and r['mem'] / r['mem_limit'] < pol['down_mem'] and r['ok'] for r in rows):
        return
    target = plans.neighbour(cur, -1)
    tp = plans.plan_sync(target)
    peak_mem = max(r['mem'] for r in rows)
    peak_cpu = max(r['cpu'] for r in rows)
    if peak_mem > tp['ram'] * 0.7 or peak_cpu > tp['cpu'] * 0.7:
        return  # would be tight on the smaller plan
    from .approvals import approve, propose
    bundle = info.get('bundlePlanID') or 'basic'
    d = plans.cost_delta(cur, target, bundle)
    why = (f"خودران: {pol['down_minutes']} دقیقه مصرف پایین (اوج CPU {peak_cpu:.2f} هسته، اوج حافظه {peak_mem * 1024:.0f}MB) — "
           f"کاهش پلن به {target} و صرفه‌جویی {plans.fmt_toman(-d['delta_day'])} در روز")
    prop = await propose('resize_app', {'app': app, 'plan': target}, {'origin': 'autopilot', 'actor': 'orchestrator', 'why': why,
                         'extra': {'before': {'plan': cur, 'peak_cpu': round(peak_cpu, 3), 'peak_mem_gb': round(peak_mem, 3)}}}, auto=True)
    if prop.get('refused'):
        return
    mark(app, 'resize')
    store.audit('orchestrator', 'autoscale_down', app, {'from': cur, 'to': target, 'proposal': prop['id']})
    await approve(prop['id'], 'autopilot')


async def check_balance():
    if not cool('_team', 'balance', 1800):
        return
    mark('_team', 'balance')
    try:
        from .tools import account_balance
        b = await account_balance({}, {})
        STATE['balance'] = {**b, 'ts': time.time()}
        try:
            from . import alerts
            from .llm import usage_rows
            day = time.strftime('%Y-%m-%d', time.gmtime())
            await alerts.evaluate_metrics('_team', {'credit_days': round(b['hours_left'] / 24, 2) if b.get('hours_left') else None, 'burn_rate': b.get('burn_toman_per_hour'),
                                                    'llm_spend_day': round(sum(r['cost_usd'] for r in usage_rows(1) if r['day'] == day), 4)})
        except Exception as e:
            log.warning('team alert rules failed: %s', e)
        if b.get('hours_left') is not None and b['hours_left'] < 48:
            inc, new = store.open_incident('_team', 'budget', 'medium', f"اعتبار لیارا حدود {b['hours_left']} ساعت دیگر تمام می‌شود", 'budget', {'balance': b})
            if new:
                bus.publish('incident', incident=inc)
    except Exception as e:
        log.warning('balance check failed: %s', e)


_register_jobs()


async def loop():
    STATE['running'] = True
    await asyncio.sleep(5)
    while True:
        t0 = time.time()
        try:
            from .approvals import expire_old
            expire_old()
            await plans.catalogue()
            apps = [a for a in store.apps() if a['role'] not in ('off', 'replica')]  # replicas are owned by the replica controller
            res = await asyncio.gather(*(cycle_app(a) for a in apps), return_exceptions=True)
            errs = [f"{a['name']}: {r}" for a, r in zip(apps, res) if isinstance(r, Exception)]
            STATE['last_error'] = '; '.join(errs)[:500] if errs else None
            await check_balance()
            try:
                from . import alerts
                await alerts.escalate()
            except Exception as e:
                log.warning('alert escalation: %s', e)
            try:
                from . import reports
                await reports.maybe_scheduled()
            except Exception as e:
                log.warning('report scheduler: %s', e)
            if STATE['cycles'] % 60 == 0:
                store.prune_samples()
        except Exception as e:
            STATE['last_error'] = str(e)[:300]
            log.exception('cycle failed')
        STATE['cycles'] += 1
        STATE['last_cycle'] = time.time()
        await asyncio.sleep(max(5, settings.orchestrator_interval - (time.time() - t0)))
