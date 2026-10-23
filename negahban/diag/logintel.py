"""Log intelligence for Liara runtime logs. Understands the team's structured JSON standard (event, level, request_id, trace_id,
route, status, duration_ms, provider, model, tokens_in/out, cost, error_type, error, stack) with a plain-text fallback
(Python tracebacks, Node stacks, uvicorn access lines, nginx combined lines, OOM/killed messages).
Produces error groups (fingerprint = error_type + top app frame), per-route latency, LLM token/cost usage, request traces,
and compact summaries sized for the LLM context. Pure functions, unit-tested with fixture logs."""
import json, re, statistics, time
from ..liara import liara
from .common import DiagError, spec, text

LEVELS = {'debug': 10, 'info': 20, 'warn': 30, 'warning': 30, 'error': 40, 'err': 40, 'critical': 50, 'fatal': 50, 'panic': 50}
UVICORN = re.compile(r'(?:(?P<lvl>INFO|WARNING|ERROR):\s+)?(?P<ip>[\d.:a-f\[\]]+):\d+ - "(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d.]+" (?P<status>\d{3})')
NGINX = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+) [^"]*" (?P<status>\d{3}) (?P<bytes>\d+|-)(?: "[^"]*" "[^"]*")?(?: (?P<rt>\d+\.\d+))?')
PY_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
NODE_FRAME = re.compile(r'^\s+at (?:(?P<func>[^\s(]+) )?\(?(?P<file>[^():\s]+):(?P<line>\d+):\d+\)?$')
EXC = re.compile(r'^(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Timeout|Refused|NotFound|Failure))(?::\s*(?P<msg>.*))?$')
OOM = re.compile(r'(?i)(out of memory|MemoryError|OOMKilled|Killed process|\bKilled\b$|cannot allocate memory|exit code 137|signal 9|heap out of memory|ENOMEM)')
STARTUP = re.compile(r'(?i)(Uvicorn running on|Application startup complete|Listening on|server started|Booting worker)')
PRELUDE = re.compile(r'(?i)exception in (asgi|wsgi) application|exception in callback|unhandled exception|^Traceback|during handling of the above|direct cause of the following')
LEVEL_WORD = re.compile(r'\b(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL|PANIC)\b')
VENDOR = ('/site-packages/', '/dist-packages/', '/lib/python', 'node_modules/', 'node:internal', '<frozen', '<string>')


def norm_route(path):
    p = (path or '').split('?')[0]
    p = re.sub(r'/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '/:uuid', p, flags=re.I)
    p = re.sub(r'/[0-9a-f]{16,}', '/:hex', p, flags=re.I)
    p = re.sub(r'/\d+(?=/|$)', '/:id', p)
    return p[:120] or '/'


def norm_msg(m):
    m = re.sub(r'0x[0-9a-f]+|\b[0-9a-f]{12,}\b', '<hex>', m or '', flags=re.I)
    m = re.sub(r"'[^']{24,}'|\"[^\"]{24,}\"", '<str>', m)
    return re.sub(r'\d+(\.\d+)?', '<n>', m)[:200]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_line(line, ts=None):
    """One raw log line -> normalised event dict (kind: json | access | nginx | text)."""
    s = (line or '').rstrip()
    ev = {'ts': ts, 'raw': s}
    if s.startswith('{') and s.endswith('}'):
        try:
            j = json.loads(s)
        except ValueError:
            j = None
        if isinstance(j, dict):
            g = lambda *ks: next((j[k] for k in ks if j.get(k) not in (None, '')), None)
            ev.update(kind='json', level=str(g('level', 'severity', 'levelname', 'lvl') or 'info').lower(), event=g('event', 'evt'), msg=g('msg', 'message'),
                      request_id=g('request_id', 'requestId', 'req_id'), trace_id=g('trace_id', 'traceId'), route=g('route', 'path', 'url'),
                      method=g('method'), status=_num(g('status', 'status_code', 'statusCode')), duration_ms=_num(g('duration_ms', 'latency_ms', 'elapsed_ms', 'duration')),
                      provider=g('provider'), model=g('model'), tokens_in=_num(g('tokens_in', 'prompt_tokens', 'input_tokens')),
                      tokens_out=_num(g('tokens_out', 'completion_tokens', 'output_tokens')), cost=_num(g('cost')), error_type=g('error_type', 'exc_type', 'exception'),
                      error=g('error', 'err'), stack=g('stack', 'traceback', 'exc_info'), app=g('app'), release=g('release'), upstream=g('upstream'), attempt=g('attempt'))
            if j.get('ts') and not ts:
                ev['ts_text'] = j['ts']
            return ev
    m = UVICORN.search(s)
    if m:
        st = int(m['status'])
        ev.update(kind='access', method=m['method'], route=m['path'], status=st, level='error' if st >= 500 else 'warn' if st >= 400 else 'info')
        return ev
    m = NGINX.match(s)
    if m:
        st = int(m['status'])
        ev.update(kind='nginx', method=m['method'], route=m['path'], status=st, duration_ms=round(float(m['rt']) * 1000, 1) if m['rt'] else None,
                  level='error' if st >= 500 else 'warn' if st >= 400 else 'info')
        return ev
    lw = LEVEL_WORD.search(s[:80])
    ev.update(kind='text', level=(lw.group(1).lower() if lw else ('error' if s.startswith('Traceback') or EXC.match(s.strip()) else 'info')), msg=s[:500])
    if OOM.search(s):
        ev.update(level='critical', oom=True)
    if STARTUP.search(s):
        ev['startup'] = True
    return ev


def _frames_from_stack(stack):
    fr = []
    for l in str(stack or '').splitlines():
        m = PY_FRAME.search(l) or NODE_FRAME.match(l)
        if m:
            fr.append({'file': m['file'], 'line': int(m['line']), 'func': m['func'] or '?'})
    return fr


def _top_app_frame(frames, node=False):
    app = [f for f in frames if not any(v in f['file'] for v in VENDOR)] or frames
    if not app:
        return None
    return app[0] if node else app[-1]  # python: innermost is last; node: innermost is first


def analyze(lines, top=10):
    """lines: [{ts, line, type?}] oldest->newest (Liara logs) or plain strings. Returns a full structured summary."""
    rows = [{'ts': None, 'line': l} if isinstance(l, str) else l for l in lines]
    levels, kinds, groups, routes, llm, oom, startups, per_min = {}, {}, {}, {}, {}, [], [], {}
    reqs = status5 = 0
    tb = None  # open multi-line traceback / stack

    def add_error(err_type, msg, frames, ts, sample, rid=None, route=None, node=False):
        f = _top_app_frame(frames, node)
        fp = f"{err_type or 'Error'}@{f['file'].rsplit('/', 1)[-1]}:{f['func']}" if f else f"{err_type or 'Error'}:{norm_msg(msg)[:60]}"
        g = groups.setdefault(fp, {'fingerprint': fp, 'error_type': err_type or 'Error', 'message': text(msg or '', 300), 'top_frame': f, 'count': 0,
                                   'first_seen': ts, 'last_seen': ts, 'request_ids': [], 'routes': {}, 'sample': sample[-14:]})
        g['count'] += 1
        g['last_seen'] = ts or g['last_seen']
        g['first_seen'] = g['first_seen'] or ts
        if rid and rid not in g['request_ids'] and len(g['request_ids']) < 5:
            g['request_ids'].append(rid)
        if route:
            r = norm_route(route)
            g['routes'][r] = g['routes'].get(r, 0) + 1
        if ts:
            k = int(ts // 60 * 60)
            per_min[k] = per_min.get(k, 0) + 1

    def close_tb():
        nonlocal tb
        if tb and (tb['frames'] or tb['node']):  # a bare 'XError: msg' line without a stack still counts once
            add_error(tb['exc'], tb['msg'], tb['frames'], tb['ts'], tb['text'], node=tb['node'])
        tb = None

    for r in rows:
        ts, s = r.get('ts'), str(r.get('line') or '')
        st = s.strip()
        # ---- multi-line tracebacks (python) and stacks (node)
        if st.startswith('Traceback (most recent call last)'):
            close_tb()
            tb = {'ts': ts, 'frames': [], 'exc': None, 'msg': None, 'text': [s], 'node': False}
            continue
        if tb is None and s.startswith('  File "') and PY_FRAME.search(s):  # traceback whose header fell outside the fetched window
            tb = {'ts': ts, 'frames': [], 'exc': None, 'msg': None, 'text': ['Traceback (partial)'], 'node': False}
        if tb is not None:
            m = PY_FRAME.search(s) if not tb['node'] else NODE_FRAME.match(s)
            if m:
                tb['frames'].append({'file': m['file'], 'line': int(m['line']), 'func': m['func'] or '?'})
                tb['text'].append(s)
                continue
            if not tb['node']:
                em = EXC.match(st)
                if em and tb['frames']:
                    tb['exc'], tb['msg'] = em['exc'], em['msg']
                    tb['text'].append(s)
                    close_tb()
                    continue
                if (s.startswith((' ', '\t')) or not st or st.startswith(('^', '~'))) and len(tb['text']) < 400:
                    tb['text'].append(s)
                    continue
            close_tb()
        em = EXC.match(st)
        if em and not st.startswith('{'):
            tb = {'ts': ts, 'frames': [], 'exc': em['exc'], 'msg': em['msg'], 'text': [s], 'node': True}  # maybe a node stack follows
            ev = parse_line(s, ts)
        else:
            ev = parse_line(s, ts)
        lv = ev.get('level') or 'info'
        lv = 'warn' if lv == 'warning' else 'error' if lv == 'err' else lv
        levels[lv] = levels.get(lv, 0) + 1
        kinds[ev['kind']] = kinds.get(ev['kind'], 0) + 1
        if ev.get('oom'):
            oom.append({'ts': ts, 'line': text(s, 200)})
        if ev.get('startup') or ev.get('event') == 'app.start':
            startups.append(ts)
        if ev.get('status') is not None and ev.get('route') and (ev['kind'] in ('access', 'nginx') or ev.get('event') in (None, 'http.request', 'http.response')):
            reqs += 1
            rr = routes.setdefault(f"{ev.get('method') or ''} {norm_route(ev['route'])}".strip(), {'count': 0, 'errors': 0, 'statuses': {}, 'durations': []})
            rr['count'] += 1
            code = int(ev['status'])
            rr['statuses'][code] = rr['statuses'].get(code, 0) + 1
            if code >= 500:
                rr['errors'] += 1
                status5 += 1
            if ev.get('duration_ms') is not None:
                rr['durations'].append(ev['duration_ms'])
        if ev.get('model') or ev.get('event') == 'llm.call':
            k = f"{ev.get('provider') or '?'}/{ev.get('model') or '?'}"
            u = llm.setdefault(k, {'calls': 0, 'tokens_in': 0, 'tokens_out': 0, 'cost': 0.0, 'errors': 0, 'durations': []})
            u['calls'] += 1
            u['tokens_in'] += int(ev.get('tokens_in') or 0)
            u['tokens_out'] += int(ev.get('tokens_out') or 0)
            u['cost'] += float(ev.get('cost') or 0)
            u['errors'] += 1 if (LEVELS.get(lv, 20) >= 40 or ev.get('error_type')) else 0
            if ev.get('duration_ms') is not None:
                u['durations'].append(ev['duration_ms'])
        if ev['kind'] == 'json' and (ev.get('error_type') or LEVELS.get(lv, 20) >= 40):
            frames = _frames_from_stack(ev.get('stack'))
            node = any(':' in f['file'] and f['file'].endswith(('.js', '.ts', '.mjs')) for f in frames)
            add_error(ev.get('error_type') or (ev.get('event') or 'error'), ev.get('error') or ev.get('msg'), frames, ts,
                      [text(s, 400)], ev.get('request_id'), ev.get('route'), node)
        elif ev['kind'] == 'text' and LEVELS.get(lv, 20) >= 40 and tb is None and not PRELUDE.search(st):
            add_error(None, ev.get('msg'), [], ts, [text(s, 400)])
    close_tb()
    # finalize
    def pct(xs, p):
        if not xs:
            return None
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(round((len(xs) - 1) * p)))], 1)
    route_rows = []
    for k, v in routes.items():
        d = v.pop('durations')
        route_rows.append({'route': k, **v, 'p50_ms': pct(d, .5), 'p95_ms': pct(d, .95), 'max_ms': max(d) if d else None})
    llm_rows = []
    for k, v in llm.items():
        d = v.pop('durations')
        llm_rows.append({'model': k, **v, 'cost': round(v['cost'], 4), 'p95_ms': pct(d, .95)})
    gl = sorted(groups.values(), key=lambda g: (-g['count'], -(g['last_seen'] or 0)))
    return {
        'lines': len(rows), 'levels': levels, 'kinds': kinds, 'structured_ratio': round(kinds.get('json', 0) / len(rows), 2) if rows else 0,
        'requests': reqs, 'errors_5xx': status5, 'error_rate': round(status5 / reqs, 3) if reqs else 0.0,
        'error_groups': gl[:top], 'error_group_count': len(gl), 'errors_total': sum(g['count'] for g in gl),
        'slowest_routes': sorted([r for r in route_rows if r['p95_ms'] is not None], key=lambda r: -r['p95_ms'])[:top],
        'busiest_routes': sorted(route_rows, key=lambda r: -r['count'])[:top],
        'llm': sorted(llm_rows, key=lambda r: -(r['tokens_in'] + r['tokens_out'])), 'oom': oom[-10:], 'startups': len(startups),
        'restart_loop': len(startups) >= 3 and all(startups[-3:]) and (startups[-1] - startups[-3] < 600),
        'errors_per_minute': sorted(per_min.items()), 'first_ts': rows[0].get('ts') if rows else None, 'last_ts': rows[-1].get('ts') if rows else None}


def compact(summary, groups=6, routes=5):
    """Shrink analyze() output for the LLM: no per-minute series, short samples."""
    s = dict(summary)
    s['error_groups'] = [{**{k: g[k] for k in ('fingerprint', 'error_type', 'message', 'count', 'request_ids')}, 'top_frame': g['top_frame'],
                          'first_seen': _hm(g['first_seen']), 'last_seen': _hm(g['last_seen']), 'routes': dict(list(g['routes'].items())[:3]),
                          'sample': '\n'.join(g['sample'][-8:])[:900]} for g in summary['error_groups'][:groups]]
    s['slowest_routes'] = summary['slowest_routes'][:routes]
    s['busiest_routes'] = summary['busiest_routes'][:routes]
    s.pop('errors_per_minute', None)
    s['first_ts'], s['last_ts'] = _hm(summary.get('first_ts')), _hm(summary.get('last_ts'))
    return s


def _hm(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else None


def trace(lines, request_id):
    """All events of one request (request_id or trace_id match, or the id appearing in a plain line), in order."""
    rid = request_id.strip()
    out = []
    for r in lines:
        ts, s = (None, r) if isinstance(r, str) else (r.get('ts'), str(r.get('line') or ''))
        if rid not in s:
            continue
        ev = parse_line(s, ts)
        out.append({'t': _hm(ts), 'level': ev.get('level'), 'event': ev.get('event'), 'route': ev.get('route'), 'status': ev.get('status'),
                    'duration_ms': ev.get('duration_ms'), 'msg': text(ev.get('msg') or ev.get('error') or s, 300), 'error_type': ev.get('error_type'),
                    'span_or_trace': ev.get('trace_id')})
    t0 = next((r.get('ts') for r in lines if not isinstance(r, str) and rid in str(r.get('line'))), None)
    t1 = next((r.get('ts') for r in reversed(lines) if not isinstance(r, str) and rid in str(r.get('line'))), None)
    return {'request_id': rid, 'events': out[:200], 'count': len(out), 'span_ms': round((t1 - t0) * 1000) if t0 and t1 else None,
            'errors': sum(1 for e in out if LEVELS.get(str(e['level']), 20) >= 40)}


async def fetch(app, minutes=60, search=None, max_lines=1500, end=None):
    end = end or time.time()
    return await liara.logs(app, start=end - minutes * 60, end=end, search=search, max_lines=max_lines)


# ---------------------------------------------------------------- tools
APP = {'app': {'type': 'string', 'description': 'Liara app name'}}


@spec('log_insights', 'در حال تحلیل هوشمند لاگ‌ها…', 'read',
      'Deep log analysis (structured JSON + plain text): error groups by fingerprint (type + top app frame) with counts, first/last seen, '
      'request ids and samples; 5xx rate; slowest and busiest routes with p50/p95; LLM tokens/cost per model; OOM and restart loops.',
      {**APP, 'minutes': {'type': 'integer', 'minimum': 5, 'maximum': 1440}, 'search': {'type': 'string', 'minLength': 3, 'maxLength': 100}}, ['app'], timeout=90)
async def log_insights_tool(a, ctx):
    lines = await fetch(a['app'], int(a.get('minutes') or 60), a.get('search'), 2000)
    s = analyze(lines)
    out = compact(s)
    if s['errors_per_minute']:
        out['_attach'] = {'kind': 'chart', 'type': 'bar', 'title': f"خطاها در دقیقه — {a['app']}", 'unit': 'خطا',
                          'series': [{'name': 'خطا', 'points': [[t, n] for t, n in s['errors_per_minute'][-120:]]}]}
    return out


@spec('log_trace', 'در حال بازسازی مسیر یک درخواست…', 'read', 'Reconstruct one request from the logs by request_id (or trace_id): every event in order, status, durations, errors.',
      {**APP, 'request_id': {'type': 'string', 'minLength': 6, 'maxLength': 100}, 'minutes': {'type': 'integer', 'minimum': 5, 'maximum': 1440}}, ['app', 'request_id'], timeout=60)
async def log_trace_tool(a, ctx):
    if not re.match(r'^[\w.:-]+$', a['request_id']):
        raise DiagError('invalid request id')
    lines = await fetch(a['app'], int(a.get('minutes') or 180), a['request_id'], 600)
    return trace(lines, a['request_id'])


@spec('llm_usage', 'در حال محاسبه‌ی مصرف توکن مدل‌ها…', 'read', 'LLM calls of an app from its structured logs: calls, tokens in/out, cost, errors and p95 latency per provider/model.',
      {**APP, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 24}}, ['app'], timeout=90)
async def llm_usage_tool(a, ctx):
    lines = await fetch(a['app'], 60 * int(a.get('hours') or 6), 'llm', 3000)
    s = analyze(lines)
    return {'app': a['app'], 'hours': int(a.get('hours') or 6), 'models': s['llm'], 'note': None if s['llm'] else 'no llm.call events found (the app may not log in the structured standard)'}
