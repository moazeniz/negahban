"""Deterministic analysis helpers: HTTP probe, log parsing (tracebacks, error rate, OOM, crash loops, slow requests,
missing env vars, build failures) and metric trend analysis. Pure functions where possible so they are unit-testable."""
import re, statistics, time
import httpx
from .sanitize import clean

# ---------------------------------------------------------------- probe
async def probe(url, timeout=15, tries=1):
    out = None
    for _ in range(tries):
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={'User-Agent': 'negahban-probe/1.0'}) as c:
                r = await c.get(url)
            ms = (time.perf_counter() - t0) * 1000
            out = {'url': url, 'status': r.status_code, 'latency_ms': round(ms), 'ok': r.status_code < 500 and r.status_code != 404 or r.status_code in (401, 403),
                   'body_snippet': clean(r.text[:300]), 'error': None}
            if out['ok']:
                return out
        except Exception as e:
            out = {'url': url, 'status': 0, 'latency_ms': round((time.perf_counter() - t0) * 1000), 'ok': False, 'body_snippet': '', 'error': type(e).__name__}
    return out


# ---------------------------------------------------------------- logs
ACCESS = re.compile(r'"(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (?P<path>\S+) HTTP/[\d.]+"\s+(?P<status>\d{3})')
DURATION = re.compile(r'(?i)(?:took|duration|elapsed|time)[=: ]+(?P<v>\d+(?:\.\d+)?)\s*(?P<u>ms|s)\b')
TB_START = re.compile(r'^Traceback \(most recent call last\):')
TB_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
EXC_LINE = re.compile(r'^(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|KeyError|StopIteration))(?::\s*(?P<msg>.*))?$')
MISSING_ENV = re.compile(r"""(?i)(?:KeyError:\s*['"](?P<k1>[A-Z][A-Z0-9_]{2,})['"]|(?:missing|required|not set|undefined)[^\n]{0,40}?(?:env(?:ironment)?(?: variable)?)?[^\n]{0,20}?\b(?P<k2>[A-Z][A-Z0-9_]{2,})\b|(?P<k3>[A-Z][A-Z0-9_]{2,}) (?:is not set|environment variable is (?:missing|required)))""")
OOM = re.compile(r'(?i)(out of memory|MemoryError|OOMKilled|Killed process|cannot allocate memory|exit code 137|signal 9)')
STARTUP = re.compile(r'(?i)(Uvicorn running on|Listening on (port|http)|Booting worker|server started on)')
SHUTDOWN = re.compile(r'(?i)(Shutting down|Finished server process|worker .* exiting|exited with code)')
CONN_ERR = re.compile(r'(?i)(connection refused|could not connect|timed? ?out|ECONNREFUSED|Name or service not known|database is locked|OperationalError)')
SLOW_HINT = re.compile(r'(?i)slow (request|query)|took \d{4,}\s*ms')


def parse_logs(lines):
    """lines: [{ts,type,line}] oldest->newest. Returns structured findings."""
    tracebacks, cur = [], None
    status_counts, slow, errors5xx, req_total = {}, [], [], 0
    oom_hits, env_missing, conn_errs, startups, shutdowns, suspicious = [], {}, [], [], [], 0
    paths5xx = {}
    for i, e in enumerate(lines):
        ln = clean(e.get('line', ''), 2000)
        ts = e.get('ts')
        m = MISSING_ENV.search(ln)
        if m and not ln.startswith('    ') and not ln.lstrip().startswith('File "'):  # skip source excerpts inside tracebacks
            k = m['k1'] or m['k2'] or m['k3']
            if k and k not in ('GET', 'POST', 'HTTP', 'INFO', 'ERROR', 'WARNING'):
                env_missing[k] = env_missing.get(k, 0) + 1
        if TB_START.search(ln):
            cur = {'ts': ts, 'frames': [], 'exc': None, 'msg': None, 'text': [ln]}
            tracebacks.append(cur)
            continue
        if cur is not None:
            cur['text'].append(ln)
            m = TB_FRAME.search(ln)
            if m:
                cur['frames'].append({'file': m['file'], 'line': int(m['line']), 'func': m['func']})
                continue
            m = EXC_LINE.match(ln.strip())
            if m and cur['frames']:
                cur['exc'], cur['msg'] = m['exc'], (m['msg'] or '')[:300]
                cur = None
                continue
            if len(cur['text']) > 80:
                cur = None
            continue
        m = ACCESS.search(ln)
        if m:
            req_total += 1
            st = int(m['status'])
            status_counts[st] = status_counts.get(st, 0) + 1
            if st >= 500:
                errors5xx.append({'ts': ts, 'path': m['path'], 'status': st})
                paths5xx[m['path'].split('?')[0]] = paths5xx.get(m['path'].split('?')[0], 0) + 1
        d = DURATION.search(ln)
        if d:
            ms = float(d['v']) * (1000 if d['u'].lower() == 's' else 1)
            if ms >= 1500:
                slow.append({'ts': ts, 'ms': ms, 'line': ln[:200]})
        elif SLOW_HINT.search(ln):
            slow.append({'ts': ts, 'ms': None, 'line': ln[:200]})
        if OOM.search(ln):
            oom_hits.append({'ts': ts, 'line': ln[:200]})
        if CONN_ERR.search(ln):
            conn_errs.append({'ts': ts, 'line': ln[:200]})
        if STARTUP.search(ln) and (not startups or not ts or not startups[-1] or ts - startups[-1] > 5):
            startups.append(ts)
        if SHUTDOWN.search(ln):
            shutdowns.append(ts)
    # group tracebacks by signature
    sigs = {}
    for tb in tracebacks:
        app_frames = [f for f in tb['frames'] if '/site-packages/' not in f['file'] and '/lib/python' not in f['file'] and not f['file'].startswith('<')] or tb['frames']
        top = app_frames[-1] if app_frames else {}
        sig = f"{tb['exc']}@{top.get('file', '?')}:{top.get('line', '?')}"
        s = sigs.setdefault(sig, {'signature': sig, 'exc': tb['exc'], 'msg': tb['msg'], 'count': 0, 'first_ts': tb['ts'], 'last_ts': tb['ts'],
                                  'top_frame': top, 'frames': tb['frames'][-8:], 'sample': '\n'.join(tb['text'][-25:])})
        s['count'] += 1; s['last_ts'] = tb['ts']
    e5 = sum(v for k, v in status_counts.items() if k >= 500)
    return {
        'lines': len(lines), 'requests': req_total, 'status_counts': status_counts, 'error_rate': round(e5 / req_total, 3) if req_total else 0.0,
        'errors_5xx': len(errors5xx), 'paths_5xx': dict(sorted(paths5xx.items(), key=lambda x: -x[1])[:8]),
        'tracebacks': sorted(sigs.values(), key=lambda s: -s['count']), 'slow': slow[-20:], 'oom': oom_hits[-10:], 'missing_env': env_missing,
        'conn_errors': conn_errs[-10:], 'startups': len(startups), 'shutdowns': len(shutdowns),
        'restart_loop': len(startups) >= 3 and (startups[-1] - startups[-3] < 600 if startups[-1] and startups[-3] else False),
        'first_ts': lines[0]['ts'] if lines else None, 'last_ts': lines[-1]['ts'] if lines else None,
    }


def parse_build_logs(lines):
    """Build output lines -> failure summary (pip/npm/docker errors)."""
    text = [clean(l, 500) for l in lines]
    errs = [l for l in text if re.search(r'(?i)\b(error|failed|not found|no matching distribution|could not find|returned a non-zero)\b', l)]
    pkg = None
    for l in text:
        m = re.search(r'(?i)No matching distribution found for (\S+)|Could not find a version that satisfies the requirement (\S+)|npm ERR! 404.*?\'(\S+)\'', l)
        if m:
            pkg = next(g for g in m.groups() if g)
    return {'error_lines': errs[-15:], 'missing_package': pkg, 'tail': text[-25:]}


# ---------------------------------------------------------------- metrics
def trend(series, limit=None):
    """series: [(ts, value)]. Returns level, peak, slope/min, utilisation vs limit, projection to limit."""
    if not series:
        return {'n': 0}
    vals = [v for _, v in series]
    ts = [t for t, _ in series]
    n = len(vals)
    last = statistics.mean(vals[-3:])
    out = {'n': n, 'last': last, 'mean': statistics.mean(vals), 'peak': max(vals), 'min': min(vals)}
    if n >= 4:
        mt, mv = statistics.mean(ts), statistics.mean(vals)
        den = sum((t - mt) ** 2 for t in ts) or 1
        slope = sum((t - mt) * (v - mv) for t, v in zip(ts, vals)) / den  # per second
        out['slope_per_min'] = slope * 60
        rising = sum(1 for a, b in zip(vals, vals[1:]) if b > a) / (n - 1)
        out['monotonic_rise'] = round(rising, 2)
    if limit:
        out['limit'] = limit
        out['util_last'] = round(last / limit, 3)
        out['util_peak'] = round(out['peak'] / limit, 3)
        out['sustained_high'] = n >= 3 and all(v / limit >= 0.85 for v in vals[-3:])
        out['sustained_low'] = n >= 10 and max(vals[-10:]) / limit < 0.25
        s = out.get('slope_per_min', 0)
        if s > 0 and last < limit:
            out['minutes_to_limit'] = round((limit - last) / s, 1)
    return out


def restarts_from_events(events):
    """Docker events -> container restarts that were NOT caused by a deploy (2nd+ start of the same release), OOMs, dies."""
    starts = {}
    for e in events:
        if e.get('action') == 'start':
            starts[e.get('release') or '?'] = starts.get(e.get('release') or '?', 0) + 1
    acts = [e.get('action') for e in events]
    return {'restarts': sum(max(0, n - 1) for n in starts.values()), 'oom': acts.count('oom'), 'die': acts.count('die'), 'kill': acts.count('kill')}
