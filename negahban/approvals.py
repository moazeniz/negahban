"""Human-in-the-loop: sensitive tool calls become proposal cards (what / why / impact incl. downtime and toman cost delta /
rollback plan). Approval is one-use (atomic claim), expires, and is bound to a fingerprint of the app state at proposal
time; if the app changed meanwhile the approval is refused as stale. After execution Negahban verifies the result
automatically and reports back to the chat/incident."""
import asyncio, hashlib, json, time
from . import bus, plans, store
from .config import settings
from .liara import LiaraError, liara
from .redact import redact

TTL = 20 * 60
VERIFY = {'launch_project', 'restart_app', 'resize_app', 'set_env', 'unset_env', 'rollback_release', 'deploy_workspace', 'apply_code_fix', 'redeploy_archive', 'scale_app', 'deploy_project'}
LABEL = {'restart_app': 'ری‌استارت برنامه', 'scale_app': 'روشن/خاموش کردن برنامه', 'resize_app': 'تغییر پلن سخت‌افزاری', 'set_env': 'تنظیم متغیر محیطی',
         'unset_env': 'حذف متغیر محیطی', 'toggle_zero_downtime': 'استقرار بدون قطعی', 'rollback_release': 'بازگشت به نسخه‌ی قبلی',
         'deploy_workspace': 'استقرار نسخه‌ی جدید', 'apply_code_fix': 'اعمال اصلاح کد و استقرار', 'redeploy_archive': 'استقرار مجدد از آرشیو',
         'create_app': 'ساخت برنامه‌ی جدید', 'deploy_project': 'استقرار پروژه', 'add_domain': 'افزودن دامنه', 'enable_ssl': 'فعال‌سازی SSL',
         'create_dns_zone': 'ساخت ناحیه‌ی DNS', 'delete_app': 'حذف برنامه', 'release_via_pipeline': 'انتشار از پایپ‌لاین گیت‌هاب'}


async def state_fingerprint(app):
    """Hash of the parts of app state an approval depends on (plan, scale, current release, env key set)."""
    if not app:
        return 'none'
    try:
        info, rels = await asyncio.gather(liara.app(app), liara.releases(app, 1))
    except LiaraError as e:
        return f'err{e.status}'
    basis = [info.get('planID'), info.get('scale'), info.get('zeroDowntime'), rels.get('currentRelease'), sorted(e['key'] for e in info.get('envs', []))]
    return hashlib.sha1(json.dumps(basis, default=str).encode()).hexdigest()[:16]


async def impact(tool, a):
    """Human-readable impact: downtime, cost delta, risk and the rollback plan."""
    app = a.get('app')
    await plans.catalogue()
    imp = {'downtime': 'ندارد', 'cost': 'بدون تغییر هزینه', 'risk': 'کم'}
    rollback = ''
    info = {}
    if app and tool not in ('create_app',):
        try:
            info = await liara.app(app)
        except LiaraError:
            info = {}
    zd = info.get('zeroDowntime')
    if tool == 'restart_app':
        imp.update(downtime='حدود ۱۰ تا ۳۰ ثانیه', risk='کم')
        rollback = 'نیازی نیست؛ ری‌استارت وضعیت دائمی را تغییر نمی‌دهد.'
    elif tool == 'scale_app':
        on = a.get('on')
        h = plans.hourly(info.get('planID', 'small-g2'), info.get('bundlePlanID') or 'basic')
        imp.update(downtime='ندارد (روشن می‌شود)' if on else 'سایت تا روشن شدن دوباره در دسترس نیست', risk='کم' if on else 'متوسط',
                   cost=f"{'+' if on else '−'}{plans.fmt_toman(h)} در ساعت")
        rollback = 'با همین دکمه برعکس آن را انجام دهید.'
    elif tool == 'resize_app':
        cur = info.get('planID', 'small-g2')
        d = plans.cost_delta(cur, a['plan'], info.get('bundlePlanID') or 'basic')
        up = d['delta_h'] > 0
        imp.update(downtime='حدود ۲۰ ثانیه خطای 502' + ('' if not zd else ' (با استقرار بدون قطعی کمتر)'), risk='کم',
                   cost=f"{'+' if up else '−'}{plans.fmt_toman(abs(d['delta_h']))} در ساعت ({'+' if up else '−'}{plans.fmt_toman(abs(d['delta_day']))} در روز)",
                   cost_delta_h=d['delta_h'], plan_from=cur, plan_to=a['plan'],
                   resources=f"{plans.plan_sync(cur)['cpu']}→{plans.plan_sync(a['plan'])['cpu']} هسته، {plans.plan_sync(cur)['ram']}→{plans.plan_sync(a['plan'])['ram']} گیگ رم")
        rollback = f'بازگشت به پلن {cur} با همین ابزار (دوباره ~۲۰ ثانیه قطعی).'
    elif tool in ('set_env', 'unset_env'):
        imp.update(downtime='برنامه ری‌استارت می‌شود (۱۰-۳۰ ثانیه)' if not zd else 'ری‌استارت بدون قطعی', risk='متوسط')
        rollback = 'لیارا برای تغییر متغیرها یک ریلیز ENV_UPDATE می‌سازد که با «بازگشت به نسخه‌ی قبلی» برگشت‌پذیر است.'
        if tool == 'set_env':
            imp['value'] = 'مقدار قبلی (پنهان) بازیابی می‌شود' if a.get('restore') or a.get('value') is None else 'مقدار جدید (پنهان در گزارش‌ها)'
    elif tool == 'rollback_release':
        imp.update(downtime='کوتاه (مثل یک استقرار)', risk='کم')
        rollback = 'می‌توانید دوباره به نسخه‌ی جدیدتر برگردید (ریلیزها در لیارا نگه داشته می‌شوند).'
    elif tool in ('deploy_workspace', 'apply_code_fix', 'redeploy_archive', 'deploy_project'):
        imp.update(downtime='ندارد اگر استقرار بدون قطعی فعال باشد' if zd else 'چند ثانیه هنگام جابه‌جایی نسخه', risk='متوسط', deploys='یکی از سهمیه‌ی استقرار روزانه مصرف می‌شود')
        rollback = 'سورس فعلی در آرشیو نگهبان ذخیره است؛ با «بازگشت به نسخه‌ی قبلی» یا استقرار از آرشیو برمی‌گردد.'
    elif tool == 'create_app':
        h = plans.hourly(a['plan'], a.get('bundle') or 'basic')
        imp.update(cost=f"+{plans.fmt_toman(h)} در ساعت (≈{plans.fmt_toman(h * 720)} در ماه)", risk='کم')
        rollback = 'حذف برنامه (با تأیید جداگانه).'
    elif tool == 'delete_app':
        imp.update(downtime='برنامه و همه‌ی داده‌هایش برای همیشه حذف می‌شود', risk='بسیار زیاد — غیرقابل بازگشت')
        rollback = 'ندارد.'
    elif tool == 'add_domain':
        imp.update(risk='کم'); rollback = 'حذف دامنه از پنل.'
    elif tool == 'toggle_zero_downtime':
        imp.update(risk='کم'); rollback = 'غیرفعال‌سازی دوباره.'
    return imp, rollback


def _describe(tool, a):
    app = a.get('app', '')
    t = {'restart_app': f'ری‌استارت {app}', 'scale_app': f"{'روشن' if a.get('on') else 'خاموش'} کردن {app}", 'resize_app': f"تغییر پلن {app} به {a.get('plan')}",
         'set_env': f"تنظیم متغیر {a.get('key')} در {app}", 'unset_env': f"حذف متغیر {a.get('key')} از {app}",
         'toggle_zero_downtime': f"{'فعال' if a.get('enable') else 'غیرفعال'} کردن استقرار بدون قطعی برای {app}",
         'rollback_release': f'بازگشت {app} به نسخه‌ی سالم قبلی', 'deploy_workspace': f'استقرار نسخه‌ی جدید {app}',
         'apply_code_fix': f'اعمال اصلاح کد روی {app} و استقرار', 'redeploy_archive': f'استقرار مجدد {app} از آرشیو',
         'create_app': f"ساخت برنامه‌ی {a.get('name')} با پلن {a.get('plan')}", 'deploy_project': f'استقرار پروژه روی {app}',
         'add_domain': f"افزودن دامنه‌ی {a.get('domain')} به {app}", 'enable_ssl': f"فعال‌سازی SSL برای {a.get('domain')}",
         'create_dns_zone': f"ساخت ناحیه‌ی DNS {a.get('zone')}", 'delete_app': f'حذف دائمی {app}'}
    return t.get(tool, tool)


def guard(tool, a, auto=False):
    """Hard safety rails that even an approval cannot override."""
    app = a.get('app')
    if not app:
        return None
    pol = store.policy(app)
    if tool == 'release_via_pipeline':
        from .gitops import NEVER_DISPATCH, PIPELINE_SELF_OK
        if app in NEVER_DISPATCH:
            return f'{app} فقط پایش می‌شود (معاملات با پول واقعی)؛ نگهبان هیچ‌وقت آن را منتشر نمی‌کند.'
        if app in PIPELINE_SELF_OK:  # releasing through the repo's own CI (tests + smoke + auto-rollback) is allowed with approval
            return None
    if pol.get('write_lock'):
        return f'برنامه‌ی {app} در حالت «فقط پایش» است؛ هیچ تغییری روی آن اجرا نمی‌شود. برای اجازه، قفل را در تنظیمات خودران همان برنامه بردارید.'
    if pol.get('env_lock') and tool in ('set_env', 'unset_env'):
        return f'متغیرهای محیطی {app} قفل است (اطلاعات حساس معاملاتی).'
    return None


def _team_of(a):
    """Team for a card: explicit args.team, else the registered app's team, else the operation's team (context)."""
    from . import teams
    if a.get('team'):
        return teams.resolve(a['team'])
    return teams.team_of_app(a.get('app')) or teams.current()


async def propose(tool, a, ctx, why=None, auto=False, incident_id=None):
    """Every card carries its Liara team (args.team when not the production team) and is built inside that team."""
    from . import teams
    a = dict(a)
    try:
        tid = _team_of(a)
    except ValueError as e:
        return {'refused': True, 'error': str(e), 'status': 'refused', 'note': 'NO approval card was created (unknown team).'}
    a.pop('team', None)
    if not teams.is_default(tid):
        a['team'] = tid
    with teams.use(tid):
        res = await _propose(tool, a, ctx, why, auto, incident_id)
    if isinstance(res, dict) and res.get('id'):
        res['team'] = teams.label(tid)  # the model and the dashboard always see which team a card belongs to
    return res


async def _propose(tool, a, ctx, why=None, auto=False, incident_id=None):
    from .tools import TOOLS
    a = {k: v for k, v in a.items()}
    secret_value = a.pop('value', None) if tool == 'set_env' else None
    if secret_value is not None:
        # keep the typed value out of the proposal row/UI: store it on the private kv, referenced by id
        a['value_ref'] = store.uid('val_')
        store.kv_set(f"pending_value:{a['value_ref']}", secret_value)
    if secret_value is None:  # same action already waiting for the owner -> reuse the card instead of piling up duplicates
        for ex in store.proposals('pending', 50, a.get('app')):
            if ex['tool'] == tool and ex['args'] == a and ex['expires_at'] > time.time():
                return {'id': ex['id'], 'title': ex['title'], 'status': 'pending_approval', 'impact': ex['impact'], 'rollback': ex['rollback'], 'note': 'an identical card is already waiting for approval'}
    blocked = guard(tool, a)
    if blocked:  # locked (monitor-only / env-locked) apps never get a card: refuse up front, with the reason
        store.audit(ctx.get('actor', 'agent'), 'propose_refused', a.get('app'), {'tool': tool, 'reason': blocked}, ok=False)
        return {'refused': True, 'error': blocked, 'status': 'refused', 'note': 'NO approval card was created (the app is locked by the owner policy).'}
    pid = store.uid('prp_')
    from .tools import TOOLS as _T
    if _T.get(tool, {}).get('impact'):
        imp, rb = await _T[tool]['impact'](a)
    else:
        imp, rb = await impact(tool, {**a, 'value': secret_value} if secret_value is not None else a)
    blocked = guard(tool, a)
    p = {'id': pid, 'app': a.get('app'), 'tool': tool, 'args': a, 'title': (_T[tool]['title'](a) if _T.get(tool, {}).get('title') else _describe(tool, a)), 'why': why or ctx.get('why') or a.get('reason') or '',
         'impact': {**imp, 'label': LABEL.get(tool, tool), **({'blocked': blocked} if blocked else {})}, 'rollback': rb,
         'created_at': time.time(), 'expires_at': time.time() + TTL, 'fingerprint': await state_fingerprint(a.get('app')),
         'origin': ctx.get('origin', 'chat'), 'incident_id': incident_id or ctx.get('incident_id'), 'status': 'pending', 'auto': int(auto)}
    if ctx.get('session'):
        p['impact']['session'] = ctx['session']
    if ctx.get('extra'):
        p['impact'].update(ctx['extra'])
    row = store.add_proposal(p)
    store.audit(ctx.get('actor', 'agent'), 'propose', a.get('app'), {'proposal': pid, 'tool': tool, 'args': a})
    bus.publish('proposal', proposal=public(row))
    return {'id': pid, 'title': p['title'], 'status': 'pending_approval', 'impact': p['impact'], 'rollback': rb,
            'note': 'Shown to the owner as an approval card; NOT executed yet.' + (f' Blocked: {blocked}' if blocked else '')}


AI_TOOLS = {'apply_code_fix'}  # decisions produced by the LLM; everything from the orchestrator/autopilot is rule-based


def public(p):
    if not p:
        return p
    out = {k: p.get(k) for k in ('id', 'app', 'tool', 'args', 'title', 'why', 'impact', 'rollback', 'status', 'created_at', 'expires_at', 'origin',
                                 'incident_id', 'result', 'verification', 'decided_by', 'decided_at', 'auto')}
    out['decision'] = 'ai' if (p.get('origin') in ('chat', 'mcp') or p.get('tool') in AI_TOOLS) else 'rules'
    from . import teams
    out['team'] = teams.label((p.get('args') or {}).get('team') or teams.default_id())
    return out


def expire_old():
    for p in store.proposals('pending', 200):
        if p['expires_at'] < time.time():
            if store.claim_proposal(p['id'], 'pending', 'expired'):
                bus.publish('proposal', proposal=public(store.get_proposal(p['id'])))


async def reject(pid, actor):
    if not store.claim_proposal(pid, 'pending', 'rejected'):
        return {'error': 'این پیشنهاد دیگر در انتظار تأیید نیست'}
    p = store.set_proposal(pid, decided_by=actor, decided_at=time.time())
    store.audit(actor, 'reject', p['app'], {'proposal': pid, 'tool': p['tool']})
    bus.publish('proposal', proposal=public(p))
    _feedback(p, f"مالک پیشنهاد «{p['title']}» را رد کرد.")
    return public(p)


async def approve(pid, actor, extra_args=None):
    from . import teams
    p = store.get_proposal(pid)
    with teams.use(((p or {}).get('args') or {}).get('team')):  # fingerprint/guard/execution happen in the card's own team
        return await _approve(pid, actor, extra_args)


async def _approve(pid, actor, extra_args=None):
    p = store.get_proposal(pid)
    if not p:
        return {'error': 'پیشنهاد پیدا نشد'}
    if p['status'] != 'pending':
        return {'error': f"این پیشنهاد در وضعیت «{p['status']}» است و قابل تأیید نیست (هر تأیید فقط یک بار)"}
    if p['expires_at'] < time.time():
        store.claim_proposal(pid, 'pending', 'expired')
        return {'error': 'مهلت این پیشنهاد تمام شده؛ دوباره درخواست بدهید'}
    blocked = guard(p['tool'], p['args'] or {})
    if blocked:
        return {'error': blocked}
    if actor == 'autopilot':
        from .alerts import auto_actions_ok
        if not auto_actions_ok():  # self-guardrail: cap on automatic actions per hour; the card stays for the owner
            store.update_incident(p['incident_id'], note='سقف اقدام‌های خودکار در ساعت پر شده؛ کارت برای تصمیم مالک باقی ماند.') if p.get('incident_id') else None
            return {'error': 'max automatic actions per hour reached'}
    if not store.claim_proposal(pid, 'pending', 'executing'):
        return {'error': 'این پیشنهاد هم‌زمان توسط درخواست دیگری در حال اجراست'}
    fp = await state_fingerprint(p['app'])
    if p['fingerprint'] and fp != p['fingerprint'] and p['tool'] not in ('create_app', 'create_dns_zone', 'enable_ssl'):
        store.set_proposal(pid, status='stale', decided_by=actor, decided_at=time.time(), result={'error': 'وضعیت برنامه از زمان پیشنهاد تغییر کرده است'})
        bus.publish('proposal', proposal=public(store.get_proposal(pid)))
        return {'error': 'وضعیت برنامه از زمان ساخت این پیشنهاد تغییر کرده (مثلاً پلن، نسخه یا متغیرها). برای امنیت، پیشنهاد باطل شد؛ دوباره بررسی کنید.'}
    store.set_proposal(pid, decided_by=actor, decided_at=time.time())
    store.audit(actor, 'approve', p['app'], {'proposal': pid, 'tool': p['tool']})
    bus.publish('proposal', proposal=public(store.get_proposal(pid)))
    extra = dict(extra_args or {})
    if 'value' in extra:  # never put a typed secret into the job payload
        ref = store.uid('val_'); store.kv_set(f'pending_value:{ref}', extra.pop('value')); extra['value_ref'] = ref
    from . import jobs
    jobs.submit('proposal.execute', {'pid': pid, 'actor': actor, 'extra': extra}, idem=f'exec:{pid}', max_attempts=1)
    return public(store.get_proposal(pid))


async def _job_execute(payload):
    extra = dict(payload.get('extra') or {})
    if extra.get('value_ref'):
        extra['value'] = store.kv_get(f"pending_value:{extra.pop('value_ref')}")
    await execute(payload['pid'], payload['actor'], extra)
    return {'proposal': payload['pid'], 'status': (store.get_proposal(payload['pid']) or {}).get('status')}


def _register_jobs():
    from . import jobs
    jobs.handler('proposal.execute', max_attempts=1)(_job_execute)


_register_jobs()


async def execute(pid, actor, extra_args=None):
    from . import teams
    p = store.get_proposal(pid)
    with teams.use(((p or {}).get('args') or {}).get('team')):
        return await _execute(pid, actor, extra_args)


async def _execute(pid, actor, extra_args=None):
    from .tools import TOOLS, ToolError
    p = store.get_proposal(pid)
    t = TOOLS[p['tool']]
    args = dict(p['args'] or {})
    args.pop('team', None)  # the team is the execution context, not a tool argument
    if args.get('value_ref'):
        args['value'] = store.kv_get(f"pending_value:{args.pop('value_ref')}")
    args.update({k: v for k, v in (extra_args or {}).items() if (k in t['schema']['properties'] or k == 'confirm_name') and k != 'team'})
    if (extra_args or {}).get('env_ref'):
        ctx_env_ref = extra_args['env_ref']
    else:
        ctx_env_ref = None
    t0 = time.time()
    if p.get('app'):  # resource detectors ignore the next minutes (two containers overlap during restart/resize/deploy)
        store.kv_set(f"settle:{p['app']}", time.time())
    ctx = {'origin': p['origin'], 'actor': actor, 'proposal_id': pid, 'incident_id': p.get('incident_id'), 'env_ref': ctx_env_ref}
    bus.publish('op', proposal_id=pid, app=p['app'], status='running', text=f"در حال اجرا: {p['title']}")
    try:
        if settings.dry_run:
            res = {'dry_run': True}
        else:
            res = await t['fn'](args, ctx)
        store.audit(actor, p['tool'], p['app'], {'proposal': pid, 'args': p['args'], 'result': res}, ok=True)
        store.set_proposal(pid, status='verifying' if p['tool'] in VERIFY else 'done', result=res)
    except (LiaraError, ToolError, Exception) as e:
        msg = e.fa() if isinstance(e, LiaraError) else redact(str(e))
        store.audit(actor, p['tool'], p['app'], {'proposal': pid, 'error': msg}, ok=False)
        p = store.set_proposal(pid, status='failed', result={'error': msg})
        bus.publish('proposal', proposal=public(p))
        bus.publish('op', proposal_id=pid, app=p['app'], status='failed', text=f"اجرا ناموفق بود: {msg}")
        _feedback(p, f"اجرای «{p['title']}» ناموفق بود: {msg}")
        if p.get('incident_id'):
            store.update_incident(p['incident_id'], note=f"اقدام ناموفق: {p['title']} — {msg}")
        return
    p = store.get_proposal(pid)
    bus.publish('proposal', proposal=public(p))
    if p['status'] == 'verifying':
        if p['incident_id']:
            store.update_incident(p['incident_id'], status='mitigating', note=f"اقدام اجرا شد: {p['title']} — در حال راستی‌آزمایی")
        bus.publish('op', proposal_id=pid, app=p['app'], status='verifying', text='در حال راستی‌آزمایی نتیجه (سلامت، لاگ، منابع)…')
        from .diagnose import verify
        expect = {}
        since = time.time()  # errors are counted only after the action completed (old containers may still log meanwhile)
        if isinstance(res, dict) and res.get('release_id'):
            expect['release_id'] = res['release_id']
        if p['tool'] in ('restart_app', 'resize_app', 'set_env', 'unset_env') or (p['tool'] == 'scale_app' and args.get('on')):
            expect['await_restart'] = True
        if p['tool'] == 'resize_app':
            expect['plan'] = args['plan']
        elif p['tool'] == 'set_env':
            expect['env_key'] = args['key']
        elif p['tool'] == 'restart_app':
            expect['mem_below'] = 0.85
        if p['tool'] == 'scale_app' and not args.get('on'):
            v = {'passed': True, 'checks': [{'name': 'خاموش شد', 'ok': True, 'detail': 'scale=0'}]}
        else:
            if isinstance(res, dict) and res.get('release_state') and res['release_state'] != 'READY':
                v = {'passed': False, 'checks': [{'name': 'وضعیت ریلیز', 'ok': False, 'detail': res['release_state']}]}
            else:
                v = await verify(p['app'], since, expect, attempts=24 if (isinstance(res, dict) and res.get('created_app')) else 8 if p['tool'] != 'resize_app' else 10)  # a brand-new *.liara.run name needs a few minutes to route
        p = store.set_proposal(pid, status='done' if v['passed'] else 'unverified', verification=v)
        store.audit('negahban', 'verify', p['app'], {'proposal': pid, 'passed': v['passed'], 'checks': v['checks']}, ok=v['passed'])
        bus.publish('proposal', proposal=public(p))
        txt = ('✅ راستی‌آزمایی موفق: ' if v['passed'] else '⚠️ راستی‌آزمایی ناموفق: ') + '، '.join(f"{c['name']} {'✓' if c['ok'] else '✗'} ({c['detail']})" for c in v['checks'])
        bus.publish('op', proposal_id=pid, app=p['app'], status='done' if v['passed'] else 'unverified', text=txt)
        if p['incident_id']:
            inc = store.update_incident(p['incident_id'], status='resolved' if v['passed'] else 'open', note=txt)
            bus.publish('incident', incident=inc)
            if v['passed'] and inc:
                try:
                    from . import memory
                    memory.remember_incident(inc, f"{p['title']} ({'خودران' if p.get('auto') else 'با تأیید مالک'}) — راستی‌آزمایی موفق")
                except Exception:
                    pass
        _feedback(p, f"«{p['title']}» اجرا شد. {txt}")
    else:
        bus.publish('op', proposal_id=pid, app=p['app'], status='done', text=f"انجام شد: {p['title']}")
        _feedback(p, f"«{p['title']}» با موفقیت اجرا شد. نتیجه: {json.dumps(res, ensure_ascii=False, default=str)[:400]}")


def _feedback(p, text):
    """Feed the outcome back into the chat that asked for it, so the agent (and the user) see it."""
    sess = (p.get('impact') or {}).get('session')
    if sess:
        store.chat_add(sess, 'system_note', text, {'proposal': p['id']})
        bus.publish('chat_note', session=sess, text=text, proposal_id=p['id'])
