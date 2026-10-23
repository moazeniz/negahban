"""Allow-listed tool registry shared by the chat agent, the orchestrator and the MCP server.
Risk classes: read (runs automatically), safe (runs automatically: diagnostics/sandboxed tests, no change to Liara),
sensitive (never runs directly: it becomes a proposal card that the owner must approve; see approvals.py)."""
import asyncio, json, time
from . import plans, store
from .analysis import probe
from .config import settings
from .liara import LiaraError, liara
from .redact import mask_envs, redact_obj

TOOLS = {}


def tool(name, risk, desc, params=None, required=(), title=None, impact=None, label_fa=None):
    """Register a tool. Extension modules (tools_*.py) use the same decorator.
    title(args)->str and impact(args)->awaitable (impact_dict, rollback_text) customise approval cards of sensitive tools;
    label_fa is the live step text shown in chat (e.g. 'در حال بررسی گواهی TLS…')."""
    def deco(fn):
        TOOLS[name] = {'name': name, 'risk': risk, 'desc': desc, 'fn': fn, 'title': title, 'impact': impact, 'label_fa': label_fa,
                       'schema': {'type': 'object', 'properties': params or {}, 'required': list(required), 'additionalProperties': False}}
        return fn
    return deco


APP = {'app': {'type': 'string', 'description': 'Liara app name, e.g. demo-shop'}}


def monitored(app):
    a = store.get_app(app)
    if not a:
        raise ToolError(f'app "{app}" is not registered in Negahban (registered: {", ".join(x["name"] for x in store.apps())})')
    return a


class ToolError(Exception):
    pass


# ------------------------------------------------------------------ read tools
@tool('list_apps', 'read', 'List Liara apps in the team with plan, status, hourly price and whether Negahban monitors them.')
async def list_apps(a, ctx):
    await plans.catalogue()
    reg = {x['name']: x for x in store.apps()}
    out = []
    for p in await liara.list_apps():
        n = p['project_id']
        out.append({'app': n, 'status': p.get('status'), 'scale': p.get('scale'), 'plan': p.get('planID'), 'bundle': p.get('bundlePlanID'),
                    'toman_per_hour': plans.hourly(p.get('planID'), p.get('bundlePlanID') or 'basic'), 'monitored': n in reg,
                    'role': reg.get(n, {}).get('role'), 'write_lock': store.policy(n).get('write_lock')})
    return out


@tool('app_status', 'read', 'Current status of an app: plan, scale, env var NAMES (values masked), current release, live CPU/RAM, health probe.', APP, ['app'])
async def app_status(a, ctx):
    app = a['app']
    info, summ, rels = await asyncio.gather(liara.app(app), liara.metrics_summary(app), liara.releases(app, 3))
    await plans.catalogue()
    pl = plans.plan_sync(info.get('planID'))
    reg = store.get_app(app) or {}
    pr = await probe((reg.get('url') or f'https://{app}.liara.run').rstrip('/') + (reg.get('health_path') or '/'))
    return {'app': app, 'status': info.get('status'), 'scale': info.get('scale'), 'plan': info.get('planID'), 'plan_fa': pl['name_fa'],
            'bundle': info.get('bundlePlanID'), 'cpu_cores_limit': pl['cpu'], 'ram_gb_limit': pl['ram'],
            'toman_per_hour': round((info.get('hourlyPrice') or 0) + (info.get('bundleHourlyPrice') or 0), 1),
            'zero_downtime': info.get('zeroDowntime'), 'envs': mask_envs(info.get('envs')),
            'cpu_now_cores': round(summ['cpu'], 3), 'ram_now_gb': round(summ['mem'] / 1024 ** 3, 3),
            'cpu_util': round(summ['cpu'] / pl['cpu'], 3), 'ram_util': round(summ['mem'] / 1024 ** 3 / pl['ram'], 3),
            'releases': [{k: r.get(k) for k in ('_id', 'tag', 'type', 'state', 'message', 'createdAt')} for r in rels.get('releases', [])],
            'current_release': rels.get('currentRelease'), 'probe': pr}


@tool('get_logs', 'read', 'Recent runtime log lines of an app (untrusted data). Optional text search (>=3 chars), errors_only filter and number of lines.',
      {**APP, 'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440}, 'search': {'type': 'string'}, 'errors_only': {'type': 'boolean'},
       'lines': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, ['app'])
async def get_logs(a, ctx):
    import re as _re
    n = int(a.get('lines') or 60)
    lines = await liara.logs(a['app'], start=time.time() - 60 * int(a.get('minutes') or 30), search=a.get('search'), max_lines=1500 if a.get('errors_only') else max(100, n + 100))
    if a.get('errors_only'):
        rx = _re.compile(r'(?i)(error|exception|traceback|critical|fatal|\" 5\d\d |failed|killed|oom|  File ")')
        lines = [l for l in lines if rx.search(l['line']) or l['type'] == 'stderr' and 'INFO' not in l['line']]
    out = [{'t': time.strftime('%H:%M:%S', time.localtime(l['ts'])), 'stream': l['type'], 'line': l['line'][:400]} for l in lines[-n:]]
    return {'count': len(out), 'lines': out, '_attach': {'kind': 'logs', 'title': f"لاگ {a['app']}" + (' (فقط خطاها)' if a.get('errors_only') else ''), 'lines': [f"{x['t']} {x['line']}" for x in out]}}


@tool('analyze_logs', 'read', 'Parse recent logs: tracebacks grouped by signature, 5xx error rate, slow requests, OOM, missing env vars, restart loops.',
      {**APP, 'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440}}, ['app'])
async def analyze_logs(a, ctx):
    from .analysis import parse_logs
    lines = await liara.logs(a['app'], start=time.time() - 60 * int(a.get('minutes') or 30))
    r = parse_logs(lines)
    r['tracebacks'] = [{k: t[k] for k in ('signature', 'exc', 'msg', 'count', 'top_frame', 'sample')} for t in r['tracebacks'][:4]]
    return r


@tool('get_metrics', 'read', 'CPU (cores) and memory (GB) time series with trend analysis vs the plan limits.',
      {**APP, 'minutes': {'type': 'integer', 'minimum': 5, 'maximum': 10080}}, ['app'])
async def get_metrics(a, ctx):
    from .analysis import trend
    since = time.time() - 60 * int(a.get('minutes') or 60)
    info, cpu, mem = await asyncio.gather(liara.app(a['app']), liara.metrics(a['app'], 'cpu', since), liara.metrics(a['app'], 'memory', since))
    pl = plans.plan_sync(info.get('planID'))
    memg = [(t, v / 1024 ** 3) for t, v in mem]
    step = max(1, len(cpu) // 40)
    return {'plan': info.get('planID'), 'cpu': trend(cpu, pl['cpu']), 'mem': trend(memg, pl['ram']),
            'cpu_points': [round(v, 3) for _, v in cpu[::step]], 'mem_points_gb': [round(v, 3) for _, v in memg[::step]]}


@tool('get_events', 'read', 'Container lifecycle events (oom, die, kill, restart, start) of the last hours.',
      {**APP, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 72}}, ['app'])
async def get_events(a, ctx):
    return await liara.events(a['app'], start=time.time() - 3600 * int(a.get('hours') or 3))


@tool('list_releases', 'read', 'Release history (tag, state, type, message, time) and the current release id.', APP, ['app'])
async def list_releases(a, ctx):
    r = await liara.releases(a['app'], 10)
    return {'current': r.get('currentRelease'), 'releases': [{k: x.get(k) for k in ('_id', 'tag', 'type', 'state', 'message', 'createdAt', 'finishedAt', 'failReason')} for x in r.get('releases', [])]}


@tool('build_logs', 'read', 'Build output of a release (defaults to the latest one) with a failure summary.', {**APP, 'release_id': {'type': 'string'}}, ['app'])
async def build_logs(a, ctx):
    from .analysis import parse_build_logs
    rid = a.get('release_id') or ((await liara.releases(a['app'], 1)).get('releases') or [{}])[0].get('_id')
    if not rid:
        return {'error': 'no releases'}
    lines, rel = await liara.build_log_lines(rid)
    return {'release': {k: rel.get(k) for k in ('state', 'failReason')}, **parse_build_logs(lines)}


@tool('probe_url', 'read', 'HTTP GET a URL of a registered app (or the app health path) and report status and latency.',
      {**APP, 'path': {'type': 'string', 'description': 'path such as / or /healthz'}}, ['app'])
async def probe_url(a, ctx):
    reg = monitored(a['app'])
    path = a.get('path') or reg.get('health_path') or '/'
    if not path.startswith('/') or '..' in path:
        raise ToolError('path must start with /')
    return await probe(reg['url'].rstrip('/') + path, tries=2)


@tool('plans_and_prices', 'read', 'Liara hardware plans (CPU, RAM, disk) with live prices in toman per hour/month, plus bundle prices and limits.')
async def plans_and_prices(a, ctx):
    c = await plans.catalogue()
    out = []
    for pid in plans.ORDER:
        p = c['plans'].get(pid)
        if p:
            out.append({**p, 'basic_bundle_h': (c['bundles'].get(pid) or {}).get('basic'), 'total_basic_h': plans.hourly(pid, 'basic'),
                        'total_basic_month': round(plans.hourly(pid, 'basic') * 720)})
    return {'live': c.get('live'), 'plans': out, 'note': 'prices are toman per hour; monthly = x720; app cost = plan + bundle'}


@tool('estimate_cost', 'read', 'Cost of running a plan+bundle (hour/day/month) and the delta vs another plan.',
      {'plan': {'type': 'string'}, 'bundle': {'type': 'string', 'enum': ['basic', 'standard', 'pro']}, 'compare_to': {'type': 'string'}}, ['plan'])
async def estimate_cost(a, ctx):
    await plans.catalogue()
    b = a.get('bundle') or 'basic'
    h = plans.hourly(a['plan'], b)
    out = {'plan': a['plan'], 'bundle': b, 'hour': h, 'day': round(h * 24), 'month': round(h * 720)}
    if a.get('compare_to'):
        out['delta'] = plans.cost_delta(a['compare_to'], a['plan'], b)
    return out


@tool('account_balance', 'read', 'Team credit balance (toman) and the current hourly burn rate of all apps.')
async def account_balance(a, ctx):
    me = await liara.me()
    bal = (me.get('team') or {}).get('balance', (me.get('user') or {}).get('balance'))
    burn = 0
    await plans.catalogue()
    for p in await liara.list_apps():
        if p.get('scale', 1):
            burn += plans.hourly(p.get('planID'), p.get('bundlePlanID') or 'basic')
    return {'balance_toman': bal, 'burn_toman_per_hour': round(burn), 'hours_left': round(bal / burn) if bal and burn else None}


@tool('list_domains', 'read', 'Domains attached to an app with verification/SSL status.', APP, ['app'])
async def list_domains(a, ctx):
    return [{k: d.get(k) for k in ('_id', 'name', 'status', 'certificatesStatus', 'CNameRecord', 'target')} for d in await liara.domains(a['app'])]


@tool('domain_setup_guide', 'read', 'For a domain already added to Liara: exact DNS records the owner must create (at IRNIC/their DNS) and current status.',
      {'domain_id': {'type': 'string'}}, ['domain_id'])
async def domain_setup_guide(a, ctx):
    from .domains import guide
    return await guide(a['domain_id'])


@tool('diagnose', 'safe', 'Full incident analysis of an app: collects status, logs, events, metrics, releases; returns ranked root-cause hypotheses with evidence and fixes.',
      {**APP, 'minutes': {'type': 'integer', 'minimum': 5, 'maximum': 240}}, ['app'])
async def diagnose_tool(a, ctx):
    from .diagnose import diagnose
    monitored(a['app'])
    d = await diagnose(a['app'], int(a.get('minutes') or 20), with_llm=False)
    return {'healthy': d['healthy'], 'hypotheses': d['hypotheses'][:5], 'probe': d['evidence']['probe'], 'cpu': d['evidence']['cpu'], 'mem': d['evidence']['mem'],
            'log_tail': d['evidence']['log_tail'][-15:]}


@tool('list_archives', 'read', 'Source archives of releases Negahban deployed (used for rollback when Liara rollback is unavailable).', APP, ['app'])
async def list_archives(a, ctx):
    return [{k: x[k] for k in ('id', 'ts', 'release_id', 'message', 'commit_sha', 'origin', 'size', 'state')} for x in store.archives(a['app'], 15)]


@tool('list_incidents', 'read', 'Open and recent incidents detected by the orchestrator.', {'app': {'type': 'string'}})
async def list_incidents(a, ctx):
    return [{k: i[k] for k in ('id', 'app', 'kind', 'severity', 'title', 'status', 'opened_at')} for i in store.incidents(20, a.get('app'))]


@tool('get_policy', 'read', 'Autopilot policy of an app (what Negahban may do without asking).', APP, ['app'])
async def get_policy(a, ctx):
    return store.policy(a['app'])


@tool('workspace_info', 'read', 'For apps whose source Negahban manages: recent commits, file list, and optionally a file content.',
      {**APP, 'path': {'type': 'string'}}, ['app'])
async def workspace_info(a, ctx):
    from . import workspace as ws
    if (monitored(a['app']).get('role')) != 'demo':
        raise ToolError('Negahban does not hold the source code of this app (only of the demo app).')
    ws.ensure(a['app'])
    out = {'commits': ws.log(a['app'], 8), 'files': ws.files(a['app'])}
    if a.get('path'):
        out['content'] = ws.read(a['app'], a['path'])
    return out


@tool('run_tests', 'safe', 'Run the app test-suite in an isolated sandbox (copy of the workspace, no secrets, resource limits).', APP, ['app'])
async def run_tests_tool(a, ctx):
    from . import workspace as ws
    if monitored(a['app']).get('role') != 'demo':
        raise ToolError('no source workspace for this app')
    ws.ensure(a['app'])
    r = await ws.run_tests(a['app'])
    return {**r, 'output': r['output'][-2500:]}


@tool('propose_code_fix', 'safe', 'Self-repair: read the traceback/build error, write a patch with the LLM, run tests in the sandbox, and create an approval card with the diff. Does NOT deploy.',
      {**APP, 'hint': {'type': 'string', 'description': 'optional: what to fix'}}, ['app'])
async def propose_code_fix(a, ctx):
    from .selffix import prepare_fix
    if monitored(a['app']).get('role') != 'demo':
        raise ToolError('self-repair is only available for apps whose source lives in a Negahban workspace (demo app)')
    return await prepare_fix(a['app'], hint=a.get('hint'), origin=ctx.get('origin', 'chat'), session=ctx.get('session'), incident_id=ctx.get('incident_id'))


@tool('analyze_project', 'safe', 'Analyse a project source (public git URL over https) to detect platform, port, env vars, resource needs and recommend a Liara plan with price.',
      {'git_url': {'type': 'string'}}, ['git_url'])
async def analyze_project_tool(a, ctx):
    from .analyzer import analyze_git
    return await analyze_git(a['git_url'])


# ------------------------------------------------------------------ charts & diagrams (rendered in the chat; data comes from Liara, not the LLM)
def _attach(kind, **spec):
    return {'kind': kind, **spec}


def _downsample(series, n=120):
    step = max(1, len(series) // n)
    return series[::step]


@tool('chart_metrics', 'read', 'Show a time-series CHART in the chat of an app metric (cpu | memory | network) for the last N hours, using real Liara data. Use for any "show/plot/graph" request.',
      {**APP, 'metric': {'type': 'string', 'enum': ['cpu', 'memory', 'network']}, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 168}}, ['app', 'metric'])
async def chart_metrics(a, ctx):
    from .analysis import trend
    app, m, hours = a['app'], a['metric'], int(a.get('hours') or 6)
    since = time.time() - hours * 3600
    info = await liara.app(app)
    pl = plans.plan_sync(info.get('planID'))
    if m == 'network':
        rx, tx = await asyncio.gather(liara.metrics(app, 'network-receive', since), liara.metrics(app, 'network-transmit', since))
        series = [{'name': 'دریافت (KB/s)', 'points': [(t, round(v / 1024, 2)) for t, v in _downsample(rx)]},
                  {'name': 'ارسال (KB/s)', 'points': [(t, round(v / 1024, 2)) for t, v in _downsample(tx)]}]
        summary = {'rx_peak_kbs': round(max((v for _, v in rx), default=0) / 1024, 1), 'tx_peak_kbs': round(max((v for _, v in tx), default=0) / 1024, 1)}
        att = _attach('chart', type='line', title=f'ترافیک شبکه‌ی {app} — {hours} ساعت اخیر', unit='KB/s', series=series)
    else:
        raw = await liara.metrics(app, 'cpu' if m == 'cpu' else 'memory', since)
        conv = (lambda v: round(v, 4)) if m == 'cpu' else (lambda v: round(v / 1024 ** 2, 1))
        pts = [(t, conv(v)) for t, v in _downsample(raw)]
        limit = pl['cpu'] if m == 'cpu' else pl['ram'] * 1024
        unit = 'هسته' if m == 'cpu' else 'MB'
        summary = trend(pts, limit)
        att = _attach('chart', type='area', title=f"{'مصرف CPU' if m == 'cpu' else 'مصرف حافظه'} {app} — {hours} ساعت اخیر", unit=unit,
                      series=[{'name': 'CPU' if m == 'cpu' else 'RAM', 'points': pts}], limit=limit, limit_label=f"سقف پلن {info.get('planID')}")
    return {'summary': summary, 'points': sum(len(s['points']) for s in att['series']), '_attach': att}


@tool('chart_costs', 'read', 'Show a PIE chart of the hourly cost split between the team apps (live prices from Liara usage report).')
async def chart_costs(a, ctx):
    u = await liara.usage_report()
    rows = [(p['project_id'], round(p.get('totalHourlyPrice') or 0, 1)) for p in u.get('projects', [])]
    total = round(u.get('totalHourlyPrice') or sum(v for _, v in rows))
    return {'total_toman_per_hour': total, 'total_toman_per_month': round(u.get('totalMonthlyPrice') or total * 720), 'apps': dict(rows),
            '_attach': _attach('chart', type='pie', title=f'سهم هزینه‌ی ساعتی برنامه‌ها (جمع {total:,} تومان در ساعت)', unit='تومان/ساعت',
                               series=[{'name': n, 'value': v} for n, v in rows])}


@tool('show_chart', 'read', 'Render a small custom chart in the chat from numbers you already have (bar/line/pie). Prefer chart_metrics/chart_costs for Liara data.',
      {'type': {'type': 'string', 'enum': ['bar', 'line', 'pie']}, 'title': {'type': 'string'}, 'unit': {'type': 'string'},
       'labels': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 40}, 'values': {'type': 'array', 'items': {'type': 'number'}, 'maxItems': 40}},
      ['type', 'title', 'labels', 'values'])
async def show_chart(a, ctx):
    labels, values = [str(x)[:40] for x in a['labels']], [float(v) for v in a['values']][:len(a['labels'])]
    if a['type'] == 'pie':
        att = _attach('chart', type='pie', title=a['title'][:120], unit=a.get('unit', '')[:20], series=[{'name': l, 'value': v} for l, v in zip(labels, values)])
    else:
        att = _attach('chart', type=a['type'], title=a['title'][:120], unit=a.get('unit', '')[:20], labels=labels, series=[{'name': a['title'][:40], 'values': values}])
    return {'rendered': True, '_attach': att}


@tool('show_diagram', 'read', 'Render a Mermaid diagram in the chat (flowchart/sequence/etc.). Keep it small and valid Mermaid syntax.',
      {'mermaid': {'type': 'string'}, 'title': {'type': 'string'}}, ['mermaid'])
async def show_diagram(a, ctx):
    src = a['mermaid'][:6000]
    if '<script' in src.lower() or 'javascript:' in src.lower():
        raise ToolError('unsafe diagram content')
    return {'rendered': True, '_attach': _attach('diagram', title=(a.get('title') or '')[:120], mermaid=src)}


@tool('topology_diagram', 'read', 'Draw the real topology of the team: Liara apps, plans, network, Negahban monitoring, external watchdog and BoxAPI. Use for "how do our apps connect" questions.')
async def topology_diagram(a, ctx):
    apps = await liara.list_apps()
    reg = {x['name']: x for x in store.apps()}
    from .llm import llm
    li = llm.info()
    prov = f"🧠 {'Anthropic' if li['provider'] == 'anthropic' else 'BoxAPI'} {li['model']}"
    fb = li.get('fallback')
    lines = ['flowchart LR', '  owner(["👤 مالک سایت"]) -->|داشبورد و گفتگو| ngh', '  wd["🐕 watchdog روی سرور خارجی"] -->|سلامت هر ۲ دقیقه| ngh',
             '  ngh -->|API لیارا| liara[("☁️ Liara API")]', f'  ngh -->|LLM فعال| llm1["{prov}"]',
             *([f'  ngh -.->|پشتیبان| llm2["{"BoxAPI gpt-6-luna" if fb == "boxapi" else "Anthropic " + li["model"]}"]'] if fb else []),
             '  subgraph net["شبکه‌ی خصوصی meow-net"]']
    ids = {}
    for i, p in enumerate(apps):
        n = p['project_id']; ids[n] = f'a{i}'
        if n == settings.self_app:
            lines.append(f'    ngh["🛡️ {n}<br/>{p.get("planID")}"]')
            continue
        lock = store.policy(n)
        tag = '🔒 فقط پایش' if lock.get('write_lock') else ('🤖 خودران' if lock.get('autopilot') else 'پایش')
        lines.append(f'    a{i}["{n}<br/>{p.get("planID")} · {tag}"]')
    lines.append('  end')
    for n, i in ids.items():
        if n != settings.self_app and n in reg and reg[n]['role'] != 'off':
            lines.append(f'  ngh -.->|پایش هر دقیقه| {i}')
    if settings.self_app not in ids:
        lines.insert(1, '  ngh["🛡️ Negahban"]')
    m = '\n'.join(lines)
    return {'apps': [p['project_id'] for p in apps], '_attach': _attach('diagram', title='نقشه‌ی برنامه‌ها و نگهبان', mermaid=m)}


@tool('team_usage', 'read', 'Team billing: balance, hourly/monthly run-rate per app from the Liara usage report.')
async def team_usage(a, ctx):
    u, b = await asyncio.gather(liara.usage_report(), liara.billing())
    bal = (b.get('team') or b.get('user') or {}).get('balance')
    tot = u.get('totalHourlyPrice') or 0
    return {'balance_toman': bal, 'total_toman_per_hour': tot, 'total_toman_per_month': u.get('totalMonthlyPrice'), 'hours_left': round(bal / tot) if bal and tot else None,
            'apps': [{k: p.get(k) for k in ('project_id', 'planID', 'bundlePlanID', 'totalHourlyPrice', 'totalMonthlyPrice')} for p in u.get('projects', [])]}


@tool('running_info', 'read', 'What is running for an app: platform, port, image, current release tag, start time/uptime, disks and their usage, domains.', APP, ['app'])
async def running_info(a, ctx):
    app = a['app']
    info, rels, summ, evs, doms = await asyncio.gather(liara.app(app), liara.releases(app, 10), liara.metrics_summary(app),
                                                      liara.events(app, start=time.time() - 7 * 86400), liara.domains(app))
    cur = next((r for r in rels.get('releases', []) if r['_id'] == rels.get('currentRelease')), {})
    starts = [e['time'] for e in evs if e.get('action') == 'start' and e.get('release') == cur.get('_id')]
    up = time.time() - max(starts) if starts else None
    return {'app': app, 'platform': rels.get('platform'), 'port': cur.get('port'), 'image': (cur.get('imageName') or '').split('/')[-1], 'release_tag': cur.get('tag'),
            'release_message': cur.get('message'), 'deployed_at': cur.get('finishedAt'), 'uptime_minutes': round(up / 60) if up else None,
            'build_location': cur.get('buildLocation'), 'disks': summ.get('disks'), 'mounts': cur.get('disks'),
            'domains': [d.get('name') for d in doms] + [f'{app}.liara.run'], 'plan': info.get('planID'), 'bundle': info.get('bundlePlanID')}


# ------------------------------------------------------------------ sensitive tools (=> approval cards)
@tool('restart_app', 'sensitive', 'Restart an app (short downtime).', {**APP, 'reason': {'type': 'string'}}, ['app'])
async def restart_app(a, ctx):
    await liara.restart(a['app'])
    return {'restarted': a['app']}


@tool('scale_app', 'sensitive', 'Turn an app on (true) or off (false). Off stops billing of the plan but makes the site unavailable.',
      {**APP, 'on': {'type': 'boolean'}, 'reason': {'type': 'string'}}, ['app', 'on'])
async def scale_app(a, ctx):
    await liara.scale(a['app'], bool(a['on']))
    return {'app': a['app'], 'on': bool(a['on'])}


@tool('resize_app', 'sensitive', 'Change the hardware plan of an app (vertical scaling, ~20s of 502 without zero-downtime). Use plans_and_prices for ids.',
      {**APP, 'plan': {'type': 'string', 'enum': plans.ORDER}, 'reason': {'type': 'string'}}, ['app', 'plan'])
async def resize_app(a, ctx):
    await liara.resize(a['app'], a['plan'])
    return {'app': a['app'], 'plan': a['plan']}


@tool('set_env', 'sensitive', 'Set (or restore the last known value of) an environment variable. Liara replaces the full env list and restarts the app.',
      {**APP, 'key': {'type': 'string', 'pattern': '^[A-Za-z_][A-Za-z0-9_]{0,63}$'}, 'value': {'type': 'string'},
       'restore': {'type': 'boolean', 'description': 'use the last value Negahban saw for this key (value stays hidden)'}}, ['app', 'key'])
async def set_env(a, ctx):
    cur = await liara.envs(a['app'])
    val = a.get('value')
    if a.get('restore') or val is None:
        snap = store.kv_get(f"envsnap:{a['app']}", {})
        if a['key'] not in snap:
            raise ToolError(f"no previous value known for {a['key']}; the owner must type the value")
        val = snap[a['key']]
    cur[a['key']] = val
    await liara.update_envs(a['app'], cur)
    return {'app': a['app'], 'key': a['key'], 'set': True}


@tool('unset_env', 'sensitive', 'Remove an environment variable (restarts the app).', {**APP, 'key': {'type': 'string'}}, ['app', 'key'])
async def unset_env(a, ctx):
    cur = await liara.envs(a['app'])
    if a['key'] not in cur:
        raise ToolError('key not present')
    cur.pop(a['key'])
    await liara.update_envs(a['app'], cur)
    return {'app': a['app'], 'removed': a['key']}


@tool('toggle_zero_downtime', 'sensitive', 'Enable/disable zero-downtime deploys (needs standard or pro bundle).', {**APP, 'enable': {'type': 'boolean'}}, ['app', 'enable'])
async def toggle_zero_downtime(a, ctx):
    info = await liara.app(a['app'])
    await liara.zero_downtime(info['_id'], bool(a['enable']))
    return {'app': a['app'], 'zero_downtime': bool(a['enable'])}


@tool('rollback_release', 'sensitive', 'Roll back to an earlier release: the previous healthy one by default, or a specific release_tag like "v3" (Liara rollback API; falls back to a Negahban source archive).',
      {**APP, 'release_id': {'type': 'string'}, 'release_tag': {'type': 'string', 'pattern': '^v?[0-9]{1,4}$'}}, ['app'])
async def rollback_release(a, ctx):
    from .deployer import rollback
    rid = a.get('release_id')
    if not rid and a.get('release_tag'):
        tag = 'v' + a['release_tag'].lstrip('v')
        rid = next((r['_id'] for r in (await liara.releases(a['app'], 30)).get('releases', []) if r.get('tag') == tag), None)
        if not rid:
            raise ToolError(f'release {tag} not found')
    return await rollback(a['app'], rid, ctx)


@tool('deploy_workspace', 'sensitive', 'Deploy the current source workspace of the app (demo app) as a new release.', {**APP, 'message': {'type': 'string'}}, ['app'])
async def deploy_workspace(a, ctx):
    from .deployer import deploy_workspace as dw
    return await dw(a['app'], a.get('message') or 'deploy from Negahban', ctx)


@tool('apply_code_fix', 'sensitive', 'Apply a tested patch prepared by propose_code_fix: commit, deploy, verify.', {**APP, 'fix_id': {'type': 'string'}}, ['app', 'fix_id'])
async def apply_code_fix(a, ctx):
    from .selffix import apply_fix
    return await apply_fix(a['app'], a['fix_id'], ctx)


@tool('redeploy_archive', 'sensitive', 'Redeploy a stored source archive (manual rollback).', {**APP, 'archive_id': {'type': 'string'}}, ['app', 'archive_id'])
async def redeploy_archive(a, ctx):
    from .deployer import redeploy_archive as ra
    return await ra(a['app'], a['archive_id'], ctx)


@tool('create_app', 'sensitive', 'Create a new Liara app (billing starts).',
      {'name': {'type': 'string', 'pattern': '^[a-z0-9][a-z0-9-]{2,30}$'}, 'plan': {'type': 'string', 'enum': plans.ORDER},
       'bundle': {'type': 'string', 'enum': ['basic', 'standard', 'pro']}, 'platform': {'type': 'string'}}, ['name', 'plan'])
async def create_app(a, ctx):
    r = await liara.create_app(a['name'], a['plan'], a.get('bundle') or 'basic', a.get('platform') or 'docker')
    store.upsert_app(a['name'], 'monitored')
    store.set_policy(a['name'], {})
    return {'created': a['name'], 'result': r}


@tool('deploy_project', 'sensitive', 'Deploy an analysed project (from the new-project wizard) to an existing app.',
      {**APP, 'project_id': {'type': 'string'}}, ['app', 'project_id'])
async def deploy_project(a, ctx):
    from .deployer import deploy_project as dp
    return await dp(a['app'], a['project_id'], ctx)


@tool('add_domain', 'sensitive', 'Attach a custom domain to an app; returns the DNS records the owner must set.',
      {**APP, 'domain': {'type': 'string', 'pattern': r'^(?=.{4,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,24}$'}}, ['app', 'domain'])
async def add_domain(a, ctx):
    from .domains import add
    return await add(a['app'], a['domain'])


@tool('enable_ssl', 'sensitive', 'Request a free SSL certificate for a verified domain.', {'domain': {'type': 'string'}}, ['domain'])
async def enable_ssl(a, ctx):
    return await liara.enable_ssl(a['domain'])


@tool('create_dns_zone', 'sensitive', 'Create a zone on Liara DNS (then the owner sets the returned nameservers at IRNIC).', {'zone': {'type': 'string'}}, ['zone'])
async def create_dns_zone(a, ctx):
    return await liara.create_zone(a['zone'])


@tool('delete_app', 'sensitive', 'DELETE an app permanently (irreversible). Creates an approval card on which the OWNER must type the app name; do not ask for the name yourself.', APP, ['app'])
async def delete_app(a, ctx):
    if a.get('confirm_name') != a['app']:
        raise ToolError('confirmation name does not match')
    await liara.delete_app(a['app'])
    store.remove_app(a['app'])
    return {'deleted': a['app']}


# extension modules register more tools with the same decorator
import importlib as _il
for _m in ('tools_templates', 'tools_extra', 'onboarding', 'tools_reports', 'tools_scaler'):
    try:
        _il.import_module(f'negahban.{_m}')
    except ModuleNotFoundError as _e:
        if _e.name != f'negahban.{_m}':
            raise
try:  # diagnostics toolkit package (exec, probes, liara_data, analytics, logintel, runbooks, websearch)
    from .diag import register as _diag_register
    _diag_register(tool)
except Exception as _e:  # a broken/partial extension must never take the core agent down
    import logging as _lg
    _lg.getLogger('negahban.tools').error('diag toolkit not loaded: %r', _e)

@tool('set_default_subdomain', 'sensitive', 'Enable or disable the free default subdomain https://<app>.liara.run of an app (POST /v1/projects/{id}/default-subdomain/{enable|disable}).',
      {**APP, 'enable': {'type': 'boolean'}}, ['app', 'enable'],
      title=lambda a: f"{'فعال' if a.get('enable') else 'غیرفعال'} کردن زیردامنه‌ی پیش‌فرض {a.get('app')}.liara.run",
      label_fa='تغییر زیردامنه‌ی پیش‌فرض…')
async def set_default_subdomain(a, ctx):
    info = await liara.app(a['app'])
    await liara.post(f"/v1/projects/{info['_id']}/default-subdomain/{'enable' if a['enable'] else 'disable'}")
    return {'app': a['app'], 'default_subdomain': bool(a['enable']), 'url': f"https://{a['app']}.liara.run" if a['enable'] else None}


async def _subdomain_impact(a):
    on = a.get('enable')
    return ({'downtime': 'ندارد' if on else f"آدرس {a.get('app')}.liara.run از دسترس خارج می‌شود (دامنه‌های اختصاصی کار می‌کنند)", 'cost': 'بدون تغییر هزینه',
             'risk': 'کم' if on else 'متوسط'}, 'با همین ابزار برعکس آن را انجام دهید.')
TOOLS['set_default_subdomain']['impact'] = _subdomain_impact


@tool('remember', 'safe', 'Save a long-term fact or preference the owner tells you (e.g. their name, main project, budget preferences). Persists across sessions.',
      {'text': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['fact', 'preference']}, 'app': {'type': 'string'}}, ['text'], label_fa='در حال ذخیره در حافظه‌ی بلندمدت…')
async def remember(a, ctx):
    from . import memory
    return {'saved': memory.add(a.get('kind') or 'fact', a['text'], a.get('app'), actor=ctx.get('actor', 'agent'))}


@tool('recall', 'read', 'Search long-term memory: owner facts/preferences and past incidents (root cause + fix). Use it when the owner refers to earlier conversations or before diagnosing a recurring problem.',
      {'query': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['fact', 'preference', 'incident']}, 'app': {'type': 'string'}}, [], label_fa='در حال جستجو در حافظه…')
async def recall(a, ctx):
    from . import memory
    return [{k: r[k] for k in ('id', 'kind', 'app', 'text', 'updated_at')} for r in memory.recall(a.get('query') or '', a.get('kind'), a.get('app'))]


@tool('forget', 'safe', 'Delete one long-term memory item by id (when the owner asks to forget something).', {'id': {'type': 'string'}}, ['id'], label_fa='در حال حذف از حافظه…')
async def forget(a, ctx):
    from . import memory
    memory.delete(a['id'], ctx.get('actor', 'agent'))
    return {'deleted': a['id']}


WRITE = {n for n, t in TOOLS.items() if t['risk'] == 'sensitive'}
ENV_TOOLS = {'set_env', 'unset_env'}


def openai_tools(include_sensitive=True):
    out = []
    for t in TOOLS.values():
        if t['risk'] == 'sensitive' and not include_sensitive:
            continue
        d = t['desc'] + (' [SENSITIVE: calling this only creates an approval card for the owner; it is NOT executed until approved]' if t['risk'] == 'sensitive' else '')
        out.append({'type': 'function', 'function': {'name': t['name'], 'description': d, 'parameters': t['schema']}})
    return out


def validate(name, args):
    t = TOOLS.get(name)
    if not t:
        raise ToolError(f'unknown tool {name}')
    if not isinstance(args, dict):
        raise ToolError('arguments must be an object')
    sch = t['schema']
    extra = set(args) - set(sch['properties'])
    if extra:
        raise ToolError(f'unexpected arguments: {sorted(extra)}')
    for r in sch['required']:
        if r not in args or args[r] in (None, ''):
            raise ToolError(f'missing argument {r}')
    import re
    for k, v in args.items():
        p = sch['properties'][k]
        if p.get('type') == 'string' and not isinstance(v, str):
            raise ToolError(f'{k} must be a string')
        if p.get('type') == 'string' and len(v) > 4000:
            raise ToolError(f'{k} too long')
        if p.get('enum') and v not in p['enum']:
            raise ToolError(f'{k} must be one of {p["enum"]}')
        if p.get('pattern') and isinstance(v, str) and not re.match(p['pattern'], v):
            raise ToolError(f'{k} has an invalid format')
        if p.get('type') == 'integer':
            if not isinstance(v, int):
                raise ToolError(f'{k} must be an integer')
            if 'maximum' in p and v > p['maximum'] or 'minimum' in p and v < p['minimum']:
                raise ToolError(f'{k} out of range')
        if p.get('type') == 'boolean' and not isinstance(v, bool):
            raise ToolError(f'{k} must be boolean')
    return t


async def run(name, args, ctx):
    """Run a read/safe tool. Sensitive tools are routed to approvals.propose() and never executed here."""
    t = validate(name, args)
    if t['risk'] == 'sensitive':
        from .approvals import propose
        p = await propose(name, args, ctx)
        if p.get('refused'):
            return {'error': p['error'], 'refused': True, 'note': p['note']}
        return {'proposal': p}
    try:
        res = await t['fn'](args, ctx)
    except LiaraError as e:
        return {'error': e.fa(), 'status': e.status}
    except ToolError as e:
        return {'error': str(e)}
    return redact_obj(res)
