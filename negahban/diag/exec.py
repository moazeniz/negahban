"""Run commands inside a Liara app container through the exec websocket
(wss://api.iran.liara.ir/v1/exec?token=&cmd=&project_id=<app>&teamID=; send {"action":"start",...}; read raw frames until close).

Verified live 2026-10-22 on demo-shop:
* the cmd is NOT shell-parsed: it is split on spaces AND on '+' (form decoding), quotes are literal; no pipes/redirects;
* stdout+stderr arrive merged through a PTY (\\r\\n); there is no exit code; the socket closes when the process exits;
* a missing binary returns 'OCI runtime exec failed ... executable file not found'; python:slim images have no ps/top/free/ss/curl,
  so the diagnostics run as vetted stdlib-only Python snippets (snippets.py) that travel zlib+hex encoded;
* URLs over ~12 KB are rejected with HTTP 414; every exec shows up as exec_start/exec_die in the app events.

Risk model: `container_diag` (vetted checks) and `container_exec` (read-only allowlist) run automatically; `container_exec_any`
is sensitive (approval card) and refused on locked apps; a hard blocklist is never executed, approved or not."""
import asyncio, json, os, re, time
from urllib.parse import urlencode
from ..config import settings
from ..liara import liara
from ..redact import redact
from ..sanitize import clean
from . import snippets
from .common import DiagError, audit, locked, spec

MAX_URL = 7500
DEFAULT_CAP = 8000
_connect = None  # test hook: async context-manager factory replacing websockets.connect
# The Liara exec gateway refuses sessions that originate from Iranian networks, including Liara's own runtime and Arvan
# ('Sorry, connection failed', verified 2026-10-22). Sessions from Finland work. Inside Liara, point LIARA_EXEC_URL at a
# websocket relay outside Iran (deploy/exec_relay.py), authenticated with LIARA_EXEC_RELAY_TOKEN (sent as X-Relay-Token).
GATEWAY_REFUSED = 'Sorry, connection failed'


def _ws_connect(url):
    if _connect:
        return _connect(url)
    import websockets
    hdr = {'X-Relay-Token': os.environ['LIARA_EXEC_RELAY_TOKEN']} if os.environ.get('LIARA_EXEC_RELAY_TOKEN') else None
    return websockets.connect(url, max_size=2 ** 22, open_timeout=15, close_timeout=3, ping_interval=None, additional_headers=hdr)


def exec_url(app, argv):
    base = os.environ.get('LIARA_EXEC_URL') or settings.liara_base.replace('https://', 'wss://').replace('http://', 'ws://') + '/v1/exec'
    # Through the relay the real token is attached outside Iran, so it never crosses the plain-HTTP Arvan hop.
    token = 'via-relay' if os.environ.get('LIARA_EXEC_URL') else liara.token
    q = urlencode({'token': token, 'cmd': ' '.join(argv), 'project_id': app, 'teamID': liara.team or ''})
    return f'{base.rstrip("?")}?{q}'


async def ws_exec(app, argv, timeout=25, max_bytes=262144):
    """Run argv in the app container. Returns {output, ms, timed_out, truncated, closed, not_found}. Output is raw (unredacted):
    callers must pass it through finish()."""
    if any((not a) or ' ' in a or '+' in a or '\n' in a for a in argv):
        raise DiagError("arguments cannot contain spaces, '+' or newlines (the Liara exec API splits on them)")
    url = exec_url(app, argv)
    if len(url) > MAX_URL:
        raise DiagError('command too long for the Liara exec API')
    t0, buf, timed_out, truncated, closed = time.perf_counter(), bytearray(), False, False, None
    for attempt in range(2):
        try:
            async with _ws_connect(url) as ws:
                await ws.send(json.dumps({'action': 'start', 'cols': 200, 'rows': 50}))
                deadline = t0 + timeout
                while True:
                    left = deadline - time.perf_counter()
                    if left <= 0:
                        timed_out = True
                        break
                    try:
                        m = await asyncio.wait_for(ws.recv(), left)
                    except asyncio.TimeoutError:
                        timed_out = True
                        break
                    buf += m.encode() if isinstance(m, str) else m
                    if len(buf) > max_bytes:
                        truncated = True
                        break
        except asyncio.TimeoutError:
            raise DiagError('exec websocket did not open in time')
        except Exception as e:
            name = type(e).__name__
            if 'ConnectionClosed' in name:
                closed = getattr(getattr(e, 'rcvd', None), 'code', None) or 'closed'
            elif 'InvalidStatus' in name or 'InvalidHandshake' in name:
                raise DiagError(f'exec websocket rejected: {redact(str(e))[:160]}')
            elif not buf:
                raise DiagError(f'exec websocket error: {name}')
        out = buf.decode('utf-8', 'replace')
        if GATEWAY_REFUSED in out and attempt == 0 and len(out) < 120:  # occasionally transient: retry once
            buf.clear()
            await asyncio.sleep(1)
            continue
        break
    out = buf.decode('utf-8', 'replace')
    if GATEWAY_REFUSED in out and len(out) < 120:
        raise DiagError('the Liara exec gateway refused the session (it rejects connections from Iranian networks, including Liara itself); '
                        'set LIARA_EXEC_URL to the exec relay outside Iran')
    nf = 'executable file not found' in out or ('OCI runtime exec failed' in out and 'no such file' in out)
    return {'output': out, 'ms': round((time.perf_counter() - t0) * 1000), 'timed_out': timed_out, 'truncated': truncated,
            'closed': closed, 'not_found': nf}


def finish(raw, cap=DEFAULT_CAP, names_only=False):
    """PTY output -> clean, redacted, capped text. names_only strips VAR=value to VAR (printenv/env)."""
    s = clean(raw.replace('\r\n', '\n').replace('\r', '\n'))
    if names_only:
        s = '\n'.join(l.split('=', 1)[0] for l in s.splitlines() if l.strip())
    s = redact(s)
    if len(s) > cap:
        s = f'…[{len(s) - cap} earlier chars cut]\n' + s[-cap:]
    return s


# ---------------------------------------------------------------- command policy
_NORM = [(re.compile(r'\$\{?IFS\}?'), ' '), (re.compile(r'[\t\n]'), ' ')]
BLOCK = [(re.compile(p, re.I), why) for p, why in [
    (r'\brm\s+(-\w*[rf]|--recursive|--force|--no-preserve-root)', 'recursive/forced rm'),
    (r'\bmkfs|\bwipefs\b|\bfdisk\b|\bparted\b|\bshred\b', 'disk formatting / wiping'),
    (r'\b(shutdown|reboot|halt|poweroff|telinit)\b|\binit\s+[06]\b', 'shutdown/reboot'),
    (r'\bkill(all5)?\s+(-\S+\s+)*-?1(\s|$)|\bkillall5\b', 'killing PID 1 / every process'),
    (r':\s*\(\s*\)\s*\{', 'fork bomb'),
    (r'(^|[\s/;|&])dd(\s|$)', 'dd'),
    (r'\bch(mod|own|grp)\s+(\S+\s+)*(-\w*R|--recursive)', 'recursive chmod/chown'),
    (r'\b(curl|wget)\b.*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b|\b(ba|z|da)?sh\s+<\(', 'pipe-to-shell'),
    (r'(>|>>)\s*/(etc|proc|sys|boot|usr|bin|sbin|lib)\b', 'writing to a system path'),
    (r'\b(tee(\s+-a)?|cp|mv|ln|install|truncate|sed\s+-i\S*)\s+(\S+\s+)*/(etc|boot|usr|bin|sbin|lib)/', 'writing to a system path'),
    (r'\b(iptables|ip6tables|nft|sysctl\s+-w|mount|umount|swapoff|modprobe|insmod)\b', 'host/network configuration'),
    (r'/dev/(sd|nvme|vd|xvd|mapper|mem|kmem)', 'raw device access'),
    (r'\bcrontab\s+-r\b|(^|\s-c\s+|[;&|]\s*)(sudo\s+)?(passwd|useradd|userdel|usermod|visudo|chpasswd)\b', 'account/cron tampering'),
    (r'\bfind\b.*\s-(delete|exec\s+rm)\b', 'find -delete'),
    (r'\bnc\b.*\s-[ec]\s|/dev/tcp/|\bsocat\b.*exec', 'reverse shell'),
    (r'bytes\.fromhex|b64decode|base64\s+-d|\bdecompress\(|\\x[0-9a-f]{2}', 'obfuscated payload (write readable commands so the owner can review them)'),
]]
_SECRET_PATH = re.compile(r'(?i)(^|/)\.env|id_rsa|id_ed25519|\.pem$|\.key$|\.p12$|\.pfx$|/environ$|/etc/(g?shadow|sudoers)|/\.ssh/|\.netrc|credential|secret|\.git-credentials|\.pgpass|\.npmrc|\.pypirc')
_PATH = re.compile(r'^/[\w.@:%=,/-]*$')
_PROC_CAT = re.compile(r'^/proc/(meminfo|loadavg|uptime|cpuinfo|stat|vmstat|version|mounts|pressure/(cpu|memory|io)|net/(tcp6?|udp6?|sockstat6?|dev)|'
                       r'(self|1|\d{1,7})/(status|limits|cmdline|io|stat|statm|oom_score|oom_score_adj|smaps_rollup|cgroup))$|'
                       r'^/sys/fs/cgroup/((memory|cpu|io|pids)\.[a-z_.]+|memory/memory\.[a-z_.]+|cpu(acct)?(,cpu(acct)?)?/[a-z_.]+)$|^/etc/(os-release|resolv\.conf|hosts|hostname|timezone)$')
_INT = re.compile(r'^\d{1,4}$')
_LOCAL_URL = re.compile(r'^https?://(localhost|127\.0\.0\.1|\[::1\]):\d{2,5}(/[\w./%=&?-]*)?$')


_CURL_FLAGS = {'-s', '-S', '-sS', '-i', '-I', '-v', '-f', '-sf', '-fsS', '-L', '-m', '--max-time', '-w%{http_code}'}
_FIND_ARG = {'-maxdepth', '-mindepth', '-type', '-name', '-iname', '-size', '-mmin', '-mtime', '-newer'}


def _find_ok(rest):
    i = 0
    while i < len(rest):
        if rest[i] in _FIND_ARG and i + 1 < len(rest) and re.match(r'^[\w.*?\-/]+$', rest[i + 1]):
            i += 2
        elif rest[i] in ('-ls', '-print'):
            i += 1
        else:
            return False
    return True


def _path_ok(p):
    return bool(_PATH.match(p)) and not _SECRET_PATH.search(p)


def _flags(args, allowed):
    return all(a in allowed for a in args if a.startswith('-'))


def classify(command):
    """-> (verdict, reason, argv). verdict: 'read' (auto), 'sensitive' (needs approval) or 'blocked' (never)."""
    s = command.strip()
    for rx, rep in _NORM:
        s = rx.sub(rep, s)
    argv = s.split()
    if not argv:
        return 'blocked', 'empty command', []
    for rx, why in BLOCK:
        if rx.search(s):
            return 'blocked', why, argv
    if '+' in s:
        return 'blocked', "'+' cannot be sent through the Liara exec API (it is decoded as a space)", argv
    c, rest = argv[0].rsplit('/', 1)[-1], argv[1:]
    paths = [a for a in rest if not a.startswith('-')]
    sens = lambda why: ('sensitive', why, argv)
    ok = lambda why='read-only allowlist': ('read', why, argv)
    if c in ('uptime', 'id', 'whoami', 'hostname', 'date', 'nproc', 'pwd') and not rest:
        return ok()
    if c == 'uname' and _flags(rest, {'-a', '-r', '-m', '-s'}) and not paths:
        return ok()
    if c == 'ps' and all(re.match(r'^-?[a-zA-Z,=%:]+$', a) for a in rest):
        return ok()
    if c == 'top' and rest in (['-b', '-n1'], ['-b', '-n', '1'], ['-bn1']):
        return ok()
    if c == 'free' and _flags(rest, {'-m', '-h', '-b', '-k', '-g', '-w'}) and not paths:
        return ok()
    if c == 'df' and _flags(rest, {'-h', '-i', '-k', '-m', '-T', '-P'}) and all(_path_ok(p) for p in paths):
        return ok()
    if c == 'du' and rest and all(a in ('-s', '-h', '-sh', '-hs', '-sk', '-k', '-m', '-c', '-x', '-d1', '-d2', '--max-depth=1', '--max-depth=2') for a in rest if a.startswith('-')) \
            and paths and all(_path_ok(p) for p in paths) and len(paths) <= 4:
        return ok()
    if c == 'cat' and len(paths) == len(rest) and 1 <= len(paths) <= 4:
        return ok() if all(_PROC_CAT.match(p) for p in paths) else sens('cat outside the /proc, cgroup and system-info allowlist')
    if c in ('ls', 'stat', 'wc', 'file') and all(_path_ok(p) for p in paths):
        allowed = {'ls': {'-l', '-a', '-la', '-al', '-lah', '-lh', '-alh', '-ltr', '-lt', '-latr', '-1', '-h', '-R', '-lR', '-d', '-ld'},
                   'stat': {'-c', '-L'}, 'wc': {'-l', '-c', '-w'}, 'file': {'-b'}}[c]
        if _flags(rest, allowed) and (c != 'stat' or '-c' not in rest):
            return ok()
    if c in ('tail', 'head'):
        n = None
        if len(rest) == 3 and rest[0] == '-n' and _INT.match(rest[1]):
            n, p = int(rest[1]), rest[2]
        elif len(rest) == 2 and re.match(r'^-n?\d{1,4}$', rest[0]):
            n, p = int(rest[0].lstrip('-n')), rest[1]
        elif len(rest) == 1:
            n, p = 10, rest[0]
        if n is not None and n <= 2000 and _path_ok(p):
            return ok()
        if n is not None and _PATH.match(p) and _SECRET_PATH.search(p):
            return sens('reads a file that may hold secrets')
    if c in ('printenv', 'env') and not rest:
        return ok('environment variable NAMES only (values stripped)')
    if c in ('printenv',) and rest:
        return sens('printenv VAR reveals a value')
    if c in ('pip', 'pip3') and (rest in (['list'], ['freeze'], ['--version'], ['-V'], ['check']) or (len(rest) == 2 and rest[0] == 'show' and re.match(r'^[\w.-]+$', rest[1]))):
        return ok()
    if c in ('python', 'python3', 'node', 'npm', 'ruby', 'php', 'java', 'go', 'dotnet') and rest in (['-V'], ['--version'], ['-v'], ['version']):
        return ok()
    if c == 'npm' and rest in (['ls', '--depth=0'], ['ls'], ['list', '--depth=0'], ['ls', '--depth=0', '--json']):
        return ok()
    if c in ('ss', 'netstat') and all(re.match(r'^-[tulnpsaex]+$', a) for a in rest):
        return ok()
    if c == 'curl':
        urls = [x for x in rest if _LOCAL_URL.match(x)]
        if len(urls) == 1 and all(x in _CURL_FLAGS or x in urls or re.match(r'^\d{1,3}$', x) for x in rest):
            return ok('curl to localhost only')
    if c == 'wget' and len(rest) == 2 and rest[0] in ('-qO-', '-q', '-O-') and _LOCAL_URL.match(rest[1]):
        return ok('wget to localhost only')
    if c == 'getent' and len(rest) == 2 and rest[0] in ('hosts', 'ahosts') and re.match(r'^[\w.-]+$', rest[1]):
        return ok()
    if c == 'find' and rest and _path_ok(rest[0]) and _find_ok(rest[1:]):
        return ok()
    return sens('not on the read-only allowlist')


# ---------------------------------------------------------------- high-level helpers (also used by runbooks)
async def run_checks(app, checks, args=None, timeout=45):
    """Run vetted snippet checks (one exec). Falls back to `python` when `python3` is missing; returns {check: result}."""
    for py in ('python3', 'python'):
        r = await ws_exec(app, snippets.command(list(checks), args, py), timeout=timeout, max_bytes=1 << 20)
        if r['not_found']:
            continue
        res = snippets.parse(r['output'])
        if res is None:
            tail = finish(r['output'], 1200)
            raise DiagError('diagnostic snippet produced no result' + (' (timed out)' if r['timed_out'] else '') + (f': {tail}' if tail else ''))
        res['_exec_ms'] = r['ms']
        return res
    return await _fallback(app, checks)


async def _fallback(app, checks):
    """No Python in the image (node/go/static apps): read the same facts with plain `cat` and parse them here."""
    want = {'memory': ['/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory.events', '/proc/pressure/memory'],
            'cpu': ['/sys/fs/cgroup/cpu.max', '/sys/fs/cgroup/cpu.stat', '/proc/loadavg'], 'ports': ['/proc/net/tcp', '/proc/net/tcp6']}
    files = sorted({f for c in checks for f in want.get(c, [])})
    outs = await asyncio.gather(*[ws_exec(app, ['cat', f], timeout=15) for f in files], return_exceptions=True)
    raw = {f: (o['output'].replace('\r', '') if isinstance(o, dict) and not o['not_found'] else None) for f, o in zip(files, outs)}
    num = lambda s: int(s.strip()) if s and s.strip().isdigit() else None
    kv = lambda s: {p[0]: int(p[1]) for p in (l.split() for l in (s or '').splitlines()) if len(p) == 2 and p[1].isdigit()}
    res = {'_fallback': 'no python in the image; limited /proc+cgroup view'}
    for c in checks:
        if c == 'memory':
            lim, cur = num(raw.get('/sys/fs/cgroup/memory.max')), num(raw.get('/sys/fs/cgroup/memory.current'))
            ev = kv(raw.get('/sys/fs/cgroup/memory.events'))
            res[c] = {'limit_mb': round(lim / 2 ** 20, 1) if lim else None, 'usage_mb': round(cur / 2 ** 20, 1) if cur else None,
                      'util': round(cur / lim, 3) if cur and lim else None, 'oom_kills': ev.get('oom_kill'), 'limit_hits': ev.get('max'),
                      'psi': (raw.get('/proc/pressure/memory') or '').strip()}
        elif c == 'cpu':
            mx, st = (raw.get('/sys/fs/cgroup/cpu.max') or '').split(), kv(raw.get('/sys/fs/cgroup/cpu.stat'))
            res[c] = {'limit_cores': round(int(mx[0]) / int(mx[1]), 2) if len(mx) == 2 and mx[0].isdigit() else None, 'nr_throttled': st.get('nr_throttled'),
                      'throttle_ratio': round(st['nr_throttled'] / st['nr_periods'], 3) if st.get('nr_periods') else None, 'host_loadavg': (raw.get('/proc/loadavg') or '').strip()}
        elif c == 'ports':
            listen = []
            for f in ('/proc/net/tcp', '/proc/net/tcp6'):
                for l in (raw.get(f) or '').splitlines()[1:]:
                    p = l.split()
                    if len(p) > 3 and p[3] == '0A':
                        listen.append(int(p[1].rsplit(':', 1)[1], 16))
            res[c] = {'listening_ports': sorted(set(listen))}
        else:
            res[c] = {'error': 'needs python inside the container'}
    return res


async def run_command(app, command, cap=DEFAULT_CAP, timeout=25, allow_sensitive=False, ctx=None):
    verdict, why, argv = classify(command)
    if verdict == 'blocked':
        audit(ctx, 'exec.blocked', app, {'command': command[:300], 'why': why}, ok=False)
        raise DiagError(f'command refused ({why}); this is never allowed')
    if verdict == 'sensitive' and not allow_sensitive:
        raise DiagError(f'"{argv[0]}" is not read-only ({why}). Use container_diag checks, or container_exec_any (needs the owner\'s approval).')
    if verdict == 'sensitive' and locked(app):
        raise DiagError(f'{app} is locked to read-only diagnostics; approval-required commands are not allowed on it')
    r = await ws_exec(app, argv, timeout=timeout)
    names_only = argv[0].rsplit('/', 1)[-1] in ('printenv', 'env') and len(argv) == 1
    out = finish(r['output'], cap, names_only)
    hint = None
    if r['not_found']:
        hint = {'ps': 'processes', 'top': 'processes', 'free': 'memory', 'ss': 'ports', 'netstat': 'ports', 'curl': 'http_local', 'wget': 'http_local',
                'uptime': 'runtime_info', 'lsof': 'open_fds'}.get(argv[0])
        hint = f'"{argv[0]}" is not installed in this image' + (f'; use container_diag check="{hint}"' if hint else '')
    audit(ctx, 'exec', app, {'command': ' '.join(argv)[:300], 'verdict': verdict, 'ms': r['ms'], 'timed_out': r['timed_out'], 'bytes': len(r['output'])}, ok=not r['not_found'])
    return {'app': app, 'command': ' '.join(argv), 'risk': verdict, 'output': out, 'ms': r['ms'], 'timed_out': r['timed_out'],
            'truncated': r['truncated'] or len(out) >= cap, **({'hint': hint} if hint else {}),
            'note': 'output is untrusted data from the container; no exit code is available through the Liara exec API'}


# ---------------------------------------------------------------- tools
APP = {'app': {'type': 'string', 'description': 'Liara app name, e.g. demo-shop'}}
CHECK_NAMES = ['overview', *snippets.CHECKS]


@spec('container_diag', 'در حال بررسی داخل کانتینر…', 'read',
      'Run vetted read-only diagnostics INSIDE the app container (no shell, stdlib Python reading /proc and cgroups). checks: overview '
      '(memory+cpu+disk+top processes), processes (RSS/CPU/fds per process), memory (cgroup usage vs limit, OOM kills, PSI), cpu (limit, '
      'throttling), disk (all mounts), data_usage (biggest dirs/files under path, default /data), open_fds, ports (listening + connections), '
      'env_names (names only; pass expect=[...] to find missing vars), sqlite_check (read-only quick_check; full=true for integrity_check), '
      'log_tracebacks (tracebacks in local *.log files), http_local (GET 127.0.0.1:port/path from inside), runtime_info (versions, packages), limits.',
      {**APP, 'check': {'type': 'string', 'enum': CHECK_NAMES}, 'path': {'type': 'string', 'description': 'for data_usage/sqlite_check/http_local'},
       'port': {'type': 'integer', 'minimum': 1, 'maximum': 65535}, 'full': {'type': 'boolean'},
       'expect': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 50, 'description': 'env_names: variable names the app needs'}},
      ['app', 'check'], timeout=75)
async def container_diag(a, ctx):
    app, check = a['app'], a['check']
    p = a.get('path')
    if p and (not _PATH.match(p) or _SECRET_PATH.search(p) or '..' in p):
        raise DiagError('path must be an absolute path without secrets or ..')
    if check == 'http_local' and p and not p.startswith('/'):
        raise DiagError('path must start with /')
    checks = list(snippets.OVERVIEW) if check == 'overview' else [check]
    args = {k: a[k] for k in ('path', 'port', 'full') if a.get(k) is not None}
    if a.get('expect'):
        args['expect'] = [str(x)[:64] for x in a['expect'] if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', str(x))]
    if check == 'overview':
        args['limit'] = 8
    res = await run_checks(app, checks, args)
    audit(ctx, 'exec.diag', app, {'check': check, 'ms': res.get('_exec_ms')})
    return {'app': app, 'check': check, **res}


@spec('container_exec', 'در حال اجرای فرمان فقط‌خواندنی در کانتینر…', 'read',
      'Run ONE read-only command inside the app container. Only an allowlist runs here: ps, top -b -n1, free, df, du -sh <path>, uptime, '
      'cat of /proc (meminfo, loadavg, pressure/*, net/tcp) and cgroup memory/cpu files, ss/netstat, ls/stat/wc, tail|head -n N <file>, '
      'printenv (names only), pip list/show, npm ls --depth=0, curl/wget to localhost, find (no -exec). No shell: no pipes, quotes or '
      'redirects; arguments are split on spaces. Many slim images lack ps/free/curl: prefer container_diag.',
      {**APP, 'command': {'type': 'string', 'maxLength': 400}, 'max_kb': {'type': 'integer', 'minimum': 1, 'maximum': 64}}, ['app', 'command'], timeout=40)
async def container_exec(a, ctx):
    return await run_command(a['app'], a['command'], cap=1024 * int(a.get('max_kb') or 8), ctx=ctx)


def _exec_title(a):
    return f"اجرای فرمان در کانتینر {a.get('app')}: {a.get('command', '')[:60]}"


async def _exec_impact(a):
    verdict, why, argv = classify(a.get('command', ''))
    if verdict == 'blocked':
        raise DiagError(f'command refused ({why}); this is never allowed, not even with approval')
    if locked(a.get('app')):
        raise DiagError(f"{a.get('app')} is locked to read-only diagnostics")
    risky = bool(re.search(r'\b(rm|kill|pkill|mv|cp|chmod|chown|truncate|sqlite3|psql|mysql|redis-cli|pip|npm|apt|sh|bash|python3?|node)\b', ' '.join(argv)))
    return ({'downtime': 'ندارد، مگر خود فرمان سرویس را متوقف کند', 'cost': 'بدون هزینه', 'risk': 'زیاد' if risky else 'متوسط',
             'command': ' '.join(argv), 'why_approval': why},
            'فرمان‌های داخل کانتینر برگشت خودکار ندارند. ری‌استارت برنامه کانتینر را از ایمیج نسخه‌ی فعلی از نو می‌سازد '
            '(تغییرات بیرون از دیسک پاک می‌شود؛ تغییرات روی دیسک می‌ماند).')


@spec('container_exec_any', 'ساخت کارت تأیید برای اجرای فرمان در کانتینر…', 'sensitive',
      'Run an arbitrary (non-allowlisted) command inside the app container after the owner approves. No shell (split on spaces). '
      'Destructive commands (rm -rf, mkfs, dd, reboot, kill 1, curl|sh, writes to /etc, obfuscated payloads) are refused outright. '
      'Not available on locked apps (meowmeow-wallex, negahban-sre).',
      {**APP, 'command': {'type': 'string', 'maxLength': 400}, 'reason': {'type': 'string'}}, ['app', 'command'],
      title=_exec_title, impact=_exec_impact, timeout=60)
async def container_exec_any(a, ctx):
    return await run_command(a['app'], a['command'], cap=16000, timeout=45, allow_sensitive=True, ctx=ctx)
