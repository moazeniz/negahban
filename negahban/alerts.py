"""Guardrails & notifications (deterministic; the LLM is only woken to diagnose/explain).
* Owner-editable rules (versioned, audited): metric rules and log-pattern rules with scope, window, minimum duration,
  severity, dedup key, cooldown, quiet hours and an action: notify | wake (open an incident -> automatic diagnosis) |
  autopilot:<tool> (only allowed, non-destructive actions; still subject to the app policy / locks / approvals engine).
* Push ingest: POST /api/ingest/event with a per-app token -> rules evaluated immediately.
* Notifications: in-dashboard bell + browser notification (SSE), escalation (reminder at 10 / 30 min if unacknowledged),
  acknowledge / resolve; channel adapters (Liara mail, Bale bot, webhook) disabled until configured.
* Agent self-guardrails: daily LLM spend caps (total + per feature) -> automatic AI-off + notification; max automatic
  actions per hour."""
import json, re, secrets, time
import httpx
from . import bus, store
from .obs import event

SCHEMA = """create table if not exists alert_rules(id text primary key, version int, data text, updated_at real);
create table if not exists alert_rule_versions(id text, version int, data text, actor text, ts real);
create table if not exists notifications(id text primary key, ts real, rule_id text, app text, severity text, title text, body text,
  status text, level int default 0, last_sent real, dedup text, link text, data text default '{}');
create index if not exists notif_dedup on notifications(dedup, status);"""
METRICS = {'http_5xx_rate': 'نرخ خطای 5xx (٪)', 'latency_p95': 'تأخیر p95 (ms)', 'cpu_pct': 'CPU (٪ سقف پلن)', 'mem_pct': 'حافظه (٪ سقف پلن)',
           'restarts': 'ری‌استارت ناخواسته (تعداد)', 'oom': 'OOM (تعداد)', 'down': 'قطعی (تعداد بررسی ناموفق)', 'disk_pct': 'دیسک (٪)',
           'credit_days': 'اعتبار باقی‌مانده (روز)', 'burn_rate': 'نرخ هزینه (تومان/ساعت)', 'llm_spend_day': 'هزینه‌ی مدل زبانی امروز (دلار)',
           'deploy_failed': 'استقرار ناموفق (تعداد)'}
SEV = {'info': 0, 'warning': 1, 'critical': 2}
ALLOWED_AUTOPILOT = {'restart_app'}  # rules may only trigger non-destructive actions (they still go through approvals/policy)
DEFAULTS = [
    {'id': 'r-5xx', 'name': 'نرخ خطای سرور بالا', 'kind': 'metric', 'metric': 'http_5xx_rate', 'op': '>', 'threshold': 5, 'window_min': 5, 'min_duration_min': 0, 'severity': 'critical', 'action': 'wake'},
    {'id': 'r-latency', 'name': 'کندی پاسخ (p95)', 'kind': 'metric', 'metric': 'latency_p95', 'op': '>', 'threshold': 2500, 'window_min': 5, 'min_duration_min': 3, 'severity': 'warning', 'action': 'notify'},
    {'id': 'r-cpu', 'name': 'CPU در سقف', 'kind': 'metric', 'metric': 'cpu_pct', 'op': '>', 'threshold': 85, 'window_min': 5, 'min_duration_min': 5, 'severity': 'warning', 'action': 'notify'},
    {'id': 'r-mem', 'name': 'حافظه نزدیک سقف', 'kind': 'metric', 'metric': 'mem_pct', 'op': '>', 'threshold': 85, 'window_min': 5, 'min_duration_min': 3, 'severity': 'warning', 'action': 'notify'},
    {'id': 'r-oom', 'name': 'کمبود حافظه (OOM)', 'kind': 'metric', 'metric': 'oom', 'op': '>', 'threshold': 0, 'window_min': 10, 'min_duration_min': 0, 'severity': 'critical', 'action': 'wake'},
    {'id': 'r-down', 'name': 'سایت در دسترس نیست', 'kind': 'metric', 'metric': 'down', 'op': '>', 'threshold': 1, 'window_min': 3, 'min_duration_min': 0, 'severity': 'critical', 'action': 'wake'},
    {'id': 'r-credit', 'name': 'اعتبار لیارا رو به اتمام', 'kind': 'metric', 'scope': '_team', 'metric': 'credit_days', 'op': '<', 'threshold': 3, 'window_min': 60, 'min_duration_min': 0, 'severity': 'critical', 'action': 'notify', 'cooldown_min': 360},
    {'id': 'r-llm', 'name': 'هزینه‌ی مدل زبانی امروز', 'kind': 'metric', 'scope': '_team', 'metric': 'llm_spend_day', 'op': '>', 'threshold': 1.0, 'window_min': 60, 'min_duration_min': 0, 'severity': 'warning', 'action': 'notify', 'cooldown_min': 360},
    {'id': 'r-deploy', 'name': 'استقرار ناموفق', 'kind': 'metric', 'metric': 'deploy_failed', 'op': '>', 'threshold': 0, 'window_min': 10, 'min_duration_min': 0, 'severity': 'warning', 'action': 'wake'},
    {'id': 'r-crashlog', 'name': 'کرش برنامه در لاگ', 'kind': 'log', 'pattern': 'event=app.crash', 'severity': 'critical', 'action': 'wake'},
    {'id': 'r-traceback', 'name': 'Traceback در لاگ', 'kind': 'log', 'pattern': r're:Traceback \(most recent call last\)', 'severity': 'warning', 'action': 'notify', 'cooldown_min': 15},
    {'id': 'r-deadletter', 'name': 'کار مرده در صف (dead-letter)', 'kind': 'log', 'pattern': 'event=job.dead_letter', 'severity': 'warning', 'action': 'notify'},
    {'id': 'r-llmfail', 'name': 'خطای فراخوانی مدل زبانی', 'kind': 'log', 'pattern': 'event=llm.call level=error', 'severity': 'warning', 'action': 'notify', 'cooldown_min': 30},
]
BASE = {'scope': '*', 'enabled': True, 'cooldown_min': 10, 'quiet_hours': '', 'dedup': '{rule}:{app}'}


def ensure():
    from contextlib import closing
    with store._lock, closing(store._db()) as c:
        c.executescript(SCHEMA); c.commit()
    if not store.kv_get('alert_rules_seeded'):
        for r in DEFAULTS:
            save_rule({**BASE, **r}, 'negahban')
        store.kv_set('alert_rules_seeded', True)


def validate(r):
    errs = []
    r = {**BASE, **{k: v for k, v in r.items() if k in ('id', 'name', 'kind', 'metric', 'op', 'threshold', 'window_min', 'min_duration_min', 'severity', 'action',
                                                       'scope', 'enabled', 'cooldown_min', 'quiet_hours', 'dedup', 'pattern')}}
    if r.get('kind') not in ('metric', 'log'):
        errs.append('نوع قانون باید metric یا log باشد')
    if r['kind'] == 'metric' and r.get('metric') not in METRICS:
        errs.append('معیار نامعتبر است')
    if r['kind'] == 'metric' and r.get('op') not in ('>', '<'):
        errs.append('عملگر باید > یا < باشد')
    if r['kind'] == 'log':
        try:
            _compile(r.get('pattern') or '')
        except re.error as e:
            errs.append(f'الگو نامعتبر است: {e}')
    if r.get('severity') not in SEV:
        errs.append('شدت باید info، warning یا critical باشد')
    a = r.get('action') or 'notify'
    if not (a in ('notify', 'wake') or (a.startswith('autopilot:') and a.split(':', 1)[1] in ALLOWED_AUTOPILOT)):
        errs.append('اقدام مجاز: notify، wake یا autopilot:restart_app')
    if r.get('quiet_hours') and not re.fullmatch(r'\d{1,2}-\d{1,2}', r['quiet_hours']):
        errs.append('ساعات سکوت باید مثل 23-7 باشد')
    for k in ('threshold', 'window_min', 'min_duration_min', 'cooldown_min'):
        if k in r and r[k] is not None:
            try:
                r[k] = float(r[k]) if k == 'threshold' else int(r[k])
            except (TypeError, ValueError):
                errs.append(f'{k} باید عدد باشد')
    if not str(r.get('name') or '').strip():
        errs.append('نام لازم است')
    if errs:
        raise ValueError('؛ '.join(errs))
    r['id'] = r.get('id') or store.uid('r-')
    return r


def save_rule(r, actor='owner'):
    ensure_tables()
    r = validate(r)
    cur = store.q('select version from alert_rules where id=?', (r['id'],), one=True)
    v = (cur['version'] + 1) if cur else 1
    store.x('insert into alert_rules(id,version,data,updated_at) values(?,?,?,?) on conflict(id) do update set version=excluded.version, data=excluded.data, updated_at=excluded.updated_at',
            (r['id'], v, store.J(r), time.time()))
    store.x('insert into alert_rule_versions(id,version,data,actor,ts) values(?,?,?,?,?)', (r['id'], v, store.J(r), actor, time.time()))
    store.audit(actor, 'alert_rule_save', None, {'rule': r['id'], 'version': v})
    return {**r, 'version': v}


def ensure_tables():
    from contextlib import closing
    with store._lock, closing(store._db()) as c:
        c.executescript(SCHEMA); c.commit()


def delete_rule(rid, actor='owner'):
    ensure()
    store.x('delete from alert_rules where id=?', (rid,))
    store.audit(actor, 'alert_rule_delete', None, {'rule': rid})


def rules(enabled_only=False):
    ensure()
    out = [{**json.loads(r['data']), 'version': r['version']} for r in store.q('select * from alert_rules order by id')]
    return [r for r in out if r.get('enabled')] if enabled_only else out


def versions(rid):
    ensure()
    return store.q('select version, actor, ts, data from alert_rule_versions where id=? order by version desc', (rid,))


# ---------------------------------------------------------------- matching
def _compile(pattern):
    """`re:<regex>` | `k=v k2=v2` (structured fields, all must match) | plain keyword (case-insensitive)."""
    p = (pattern or '').strip()
    if p.startswith('re:'):
        return ('re', re.compile(p[3:], re.I))
    if '=' in p and all('=' in t for t in p.split()):
        return ('fields', dict(t.split('=', 1) for t in p.split()))
    return ('kw', p.lower())


def match_line(pattern, line, fields=None):
    kind, m = _compile(pattern)
    if kind == 're':
        return bool(m.search(line))
    if kind == 'kw':
        return bool(m) and m in line.lower()
    if fields is None:
        try:
            fields = json.loads(line) if line.lstrip().startswith('{') else {}
        except ValueError:
            fields = {}
    return all(str(fields.get(k, '')).lower() == v.lower() for k, v in m.items())


def _quiet(r):
    q = r.get('quiet_hours') or ''
    if not q:
        return False
    a, b = (int(x) for x in q.split('-'))
    h = time.gmtime(time.time() + 3.5 * 3600).tm_hour
    return (a <= h or h < b) if a > b else (a <= h < b)


# ---------------------------------------------------------------- firing
async def fire(r, app, value, detail, source='orchestrator'):
    """Dedup + cooldown + quiet hours, then notify and run the rule action."""
    ensure()
    dedup = (r.get('dedup') or '{rule}:{app}').format(rule=r['id'], app=app)
    open_ = store.q("select * from notifications where dedup=? and status='open' order by ts desc limit 1", (dedup,), one=True)
    if open_:
        return None  # already open: escalation handles reminders
    last = store.q('select ts from notifications where dedup=? order by ts desc limit 1', (dedup,), one=True)
    if last and time.time() - last['ts'] < 60 * int(r.get('cooldown_min') or 10):
        return None
    sev = r['severity']
    if _quiet(r) and sev != 'critical':
        return None
    title = f"{r['name']} — {app}"
    body = f"{detail}" + (f" (مقدار: {value})" if value is not None else '')
    nid = store.uid('ntf_')
    link = None
    if r.get('action') == 'wake' and app != '_team':
        from . import orchestrator
        inc, new = store.open_incident(app, 'alert', 'high' if sev == 'critical' else 'medium', title, f"alert:{r['id']}", {'trigger': body, 'rule': r['id']})
        link = f"#incidents/{inc['id']}"
        if new:
            store.update_incident(inc['id'], note=f'قانون هشدار «{r["name"]}» فعال شد؛ نگهبان برای تشخیص بیدار شد ({source}).')
            from . import jobs
            jobs.submit('incident.handle', {'app': app, 'incident': inc['id'], 'kind': 'alert', 'info': {}, 'pl': {'cpu': 1, 'ram': 1}, 's': {'cpu': 0, 'mem': 0, 'latency_ms': None}},
                        idem=f"inc:{inc['id']}", max_attempts=3)
    elif (r.get('action') or '').startswith('autopilot:') and app != '_team':
        tool = r['action'].split(':', 1)[1]
        if tool in ALLOWED_AUTOPILOT:
            from .approvals import approve, propose
            pol = store.policy(app)
            prop = await propose(tool, {'app': app}, {'origin': 'autopilot' if pol.get('autopilot') else 'orchestrator', 'actor': 'alerts', 'why': f"قانون هشدار «{r['name']}»: {body}"},
                                 auto=bool(pol.get('autopilot')))
            if not prop.get('refused') and pol.get('autopilot') and pol.get('restart_on_crash'):
                await approve(prop['id'], 'autopilot')
            link = '#approvals'
    store.x('insert into notifications(id,ts,rule_id,app,severity,title,body,status,level,last_sent,dedup,link,data) values(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (nid, time.time(), r['id'], app, sev, title, body[:500], 'open', 0, time.time(), dedup, link, store.J({'source': source, 'action': r.get('action')})))
    store.audit('alerts', 'alert_fired', None if app == '_team' else app, {'rule': r['id'], 'severity': sev, 'notification': nid, 'source': source})
    event('alert.fired', 'warn' if sev != 'critical' else 'error', title, rule=r['id'], severity=sev, app=app)
    n = store.q('select * from notifications where id=?', (nid,), one=True)
    bus.publish('notify', notification=n)
    await deliver(n)
    return n


async def evaluate_metrics(app, ctx):
    """ctx: computed per cycle by the orchestrator: {'http_5xx_rate','latency_p95','cpu_pct','mem_pct','restarts','oom','down','deploy_failed', ...}"""
    out = []
    for r in rules(True):
        if r['kind'] != 'metric' or (r.get('scope') not in ('*', app)):
            continue
        if r.get('scope') == '*' and app == '_team':
            continue
        v = ctx.get(r['metric'])
        if v is None:
            continue
        hit = v > r['threshold'] if r['op'] == '>' else v < r['threshold']
        key = f"alertdur:{r['id']}:{app}"
        if not hit:
            store.kv_set(key, None)
            continue
        since = store.kv_get(key) or time.time()
        store.kv_set(key, since)
        if time.time() - since < 60 * int(r.get('min_duration_min') or 0):
            continue
        n = await fire(r, app, round(v, 2) if isinstance(v, float) else v, f"{METRICS.get(r['metric'], r['metric'])} {r['op']} {r['threshold']}")
        if n:
            out.append(n)
    return out


async def evaluate_lines(app, lines, source='logs'):
    """lines: [{'line': str, 'fields': dict|None}] -> log-pattern rules."""
    out = []
    for r in rules(True):
        if r['kind'] != 'log' or r.get('scope') not in ('*', app):
            continue
        hits = [l for l in lines if match_line(r['pattern'], l.get('line', ''), l.get('fields'))]
        if hits:
            n = await fire(r, app, len(hits), f"الگوی «{r['pattern']}» {len(hits)} بار دیده شد: {hits[-1].get('line', '')[:160]}", source)
            if n:
                out.append(n)
    return out


async def escalate():
    """Reminders for unacknowledged notifications: 10 min -> level 1 (warning becomes critical), 30 min -> level 2."""
    ensure()
    for n in store.q("select * from notifications where status='open'"):
        age = time.time() - n['ts']
        lvl = 2 if age > 1800 else 1 if age > 600 else 0
        if lvl > n['level']:
            sev = 'critical' if lvl >= 1 else n['severity']
            store.x('update notifications set level=?, severity=?, last_sent=? where id=?', (lvl, sev, time.time(), n['id']))
            n = store.q('select * from notifications where id=?', (n['id'],), one=True)
            bus.publish('notify', notification={**n, 'reminder': True})
            await deliver(n, reminder=True)


def set_status(nid, status, actor='owner'):
    ensure()
    store.x('update notifications set status=? where id=?', (status, nid))
    store.audit(actor, f'alert_{status}', None, {'notification': nid})
    bus.publish('notify', notification=store.q('select * from notifications where id=?', (nid,), one=True))


def listing(status=None, limit=100):
    ensure()
    return store.q('select * from notifications ' + ('where status=? ' if status else '') + 'order by ts desc limit ?', ((status, limit) if status else (limit,)))


# ---------------------------------------------------------------- channels (disabled until configured)
def channels():
    c = store.kv_get('alert_channels', {}) or {}
    return {'email': {'enabled': bool(c.get('email_to') and c.get('mail_server_id')), 'to': c.get('email_to', ''), 'mail_server_id': c.get('mail_server_id', ''),
                      'note': 'سرویس ایمیل لیارا (نیازمند پلن ایمیل)'},
            'bale': {'enabled': bool(c.get('bale_token') and c.get('bale_chat')), 'chat_id': c.get('bale_chat', ''), 'token_set': bool(c.get('bale_token'))},
            'webhook': {'enabled': bool(c.get('webhook_url')), 'url': c.get('webhook_url', '')}}


def set_channels(data, actor='owner'):
    c = store.kv_get('alert_channels', {}) or {}
    for k in ('email_to', 'mail_server_id', 'bale_chat', 'webhook_url'):
        if k in data:
            c[k] = str(data[k] or '').strip()[:300]
    if data.get('bale_token'):
        c['bale_token'] = str(data['bale_token']).strip()[:200]
    if data.get('clear_bale_token'):
        c.pop('bale_token', None)
    store.kv_set('alert_channels', c)
    store.audit(actor, 'alert_channels', None, {'channels': {k: bool(v) for k, v in channels().items()}})
    return channels()


def _text(n, reminder=False):
    icon = {'critical': '🔴', 'warning': '🟠', 'info': '🔵'}.get(n['severity'], '🔔')
    return f"{icon} {'یادآوری: ' if reminder else ''}{n['title']}\n{n['body']}\nنگهبان: https://negahban-sre.liara.run/{n.get('link') or '#alerts'}"


async def deliver(n, reminder=False, only=None):
    """Send to every configured channel. Returns per-channel results. Never raises."""
    c = store.kv_get('alert_channels', {}) or {}
    res = {}
    async with httpx.AsyncClient(timeout=15) as h:
        if (only in (None, 'bale')) and c.get('bale_token') and c.get('bale_chat'):
            try:
                r = await h.post(f"https://tapi.bale.ai/bot{c['bale_token']}/sendMessage", json={'chat_id': c['bale_chat'], 'text': _text(n, reminder)})
                res['bale'] = r.status_code
            except Exception as e:
                res['bale'] = type(e).__name__
        if (only in (None, 'webhook')) and c.get('webhook_url'):
            try:
                r = await h.post(c['webhook_url'], json={'source': 'negahban', 'notification': {k: n.get(k) for k in ('id', 'ts', 'app', 'severity', 'title', 'body', 'link')}, 'reminder': reminder})
                res['webhook'] = r.status_code
            except Exception as e:
                res['webhook'] = type(e).__name__
        if (only in (None, 'email')) and c.get('email_to') and c.get('mail_server_id'):
            try:
                from .liara import liara
                r = await liara.post(f"/api/v1/mails/{c['mail_server_id']}/messages", {'to': c['email_to'], 'subject': n['title'][:120], 'text': _text(n, reminder)},
                                     base='https://mail-service.liara.ir')
                res['email'] = 'sent'
            except Exception as e:
                res['email'] = str(e)[:120]
    return res


# ---------------------------------------------------------------- push ingest tokens
def ingest_token(app, rotate=False):
    toks = store.kv_get('ingest_tokens', {}) or {}
    if rotate or app not in toks:
        toks[app] = secrets.token_urlsafe(24)
        store.kv_set('ingest_tokens', toks)
        store.audit('owner', 'ingest_token_rotate', app, {})
    return toks[app]


def app_for_token(tok):
    for app, t in (store.kv_get('ingest_tokens', {}) or {}).items():
        if t and secrets.compare_digest(t, tok or ''):
            return app
    return None


# ---------------------------------------------------------------- agent self-guardrails
def guardrails():
    return {**{'llm_daily_cap_usd': 2.0, 'llm_feature_caps_usd': {'chat': 1.5, 'selffix': 0.5, 'diagnose': 0.5}, 'max_auto_actions_per_hour': 6},
            **(store.kv_get('self_guardrails', {}) or {})}


def set_guardrails(data, actor='owner'):
    g = guardrails()
    if 'llm_daily_cap_usd' in data:
        g['llm_daily_cap_usd'] = max(0.0, float(data['llm_daily_cap_usd']))
    if isinstance(data.get('llm_feature_caps_usd'), dict):
        g['llm_feature_caps_usd'] = {str(k)[:20]: max(0.0, float(v)) for k, v in data['llm_feature_caps_usd'].items()}
    if 'max_auto_actions_per_hour' in data:
        g['max_auto_actions_per_hour'] = max(0, int(data['max_auto_actions_per_hour']))
    store.kv_set('self_guardrails', g)
    store.audit(actor, 'self_guardrails', None, g)
    return g


def llm_budget_ok(feature):
    """False (and AI switched off + notification) when today's LLM spend exceeds the caps."""
    from .llm import usage_rows
    g = guardrails()
    day = time.strftime('%Y-%m-%d', time.gmtime())
    rows = [r for r in usage_rows(1) if r['day'] == day]
    total = sum(r['cost_usd'] for r in rows)
    feat = sum(r['cost_usd'] for r in rows if r['feature'] == feature)
    cap_f = (g.get('llm_feature_caps_usd') or {}).get(feature)
    if total >= g['llm_daily_cap_usd']:
        from . import control
        if control.ai_enabled():
            control.set_ai(False, 'guardrail')
            store.kv_set('ai_off_reason', f'سقف هزینه‌ی روزانه‌ی مدل زبانی (${g["llm_daily_cap_usd"]}) پر شد')
            import asyncio
            asyncio.get_event_loop().create_task(fire({**BASE, 'id': 'g-llm-cap', 'name': 'سقف هزینه‌ی مدل زبانی پر شد؛ حالت بدون هوش مصنوعی فعال شد', 'severity': 'critical', 'action': 'notify'},
                                                      '_team', round(total, 4), 'هسته‌ی پایش و خودران قاعده‌محور ادامه می‌دهد', 'guardrail'))
        return False
    return not (cap_f is not None and feat >= cap_f)


def auto_actions_ok():
    g = guardrails()
    n = store.q("select count(*) n from audit where actor='autopilot' and action='approve' and ts>?", (time.time() - 3600,), one=True)['n']
    return n < g['max_auto_actions_per_hour']
