#!/usr/bin/env python3
"""External watchdog for Negahban ("who watches the watchman"). Runs on a server OUTSIDE Liara (systemd timer, every
2 minutes). If Negahban's /healthz fails (or its orchestrator stalls) on 2 consecutive runs, it repairs Negahban through
the Liara API: scale back on if it was turned off, otherwise restart. Cooldown 15 min. Every decision is logged.
Secrets: /root/.liara.env (LIARA_*), /root/.negahban-admin.env (WATCHDOG_TOKEN). Nothing secret is printed."""
import json, os, socket, sys, time, urllib.error, urllib.request

APP = os.environ.get('NEGAHBAN_APP', 'negahban-sre')
URL = os.environ.get('NEGAHBAN_URL', f'https://{APP}.liara.run')
STATE = '/var/lib/negahban-watchdog/state.json'
LOG = '/var/log/negahban-watchdog.log'
COOLDOWN = 15 * 60


def envfile(p):
    out = {}
    try:
        for line in open(p):
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                out[k] = v.strip('"\'')
    except OSError:
        pass
    return out


ENV = {**envfile('/root/.liara.env'), **envfile('/root/.negahban-admin.env')}


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + msg
    print(line)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def http(method, url, body=None, headers=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**({'Content-Type': 'application/json'} if data else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b'{', b'[') else raw[:200].decode(errors='replace'))
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode(errors='replace')
    except Exception as e:
        return 0, type(e).__name__


def liara(method, path, body=None):
    base = ENV.get('LIARA_API_BASE', 'https://api.iran.liara.ir')
    return http(method, f"{base}{path}?teamID={ENV.get('LIARA_TEAM_ID', '')}", body, {'Authorization': 'Bearer ' + ENV.get('LIARA_API_TOKEN', '')}, timeout=60)


def main():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    try:
        st = json.load(open(STATE))
    except (OSError, ValueError):
        st = {'fails': 0, 'last_action': 0}
    code, body = http('GET', URL + '/healthz')
    lag = body.get('orchestrator_last_cycle_s') if isinstance(body, dict) else None
    healthy = code == 200 and isinstance(body, dict) and body.get('ok') and (lag is None or lag < 300)
    if healthy:
        if st['fails']:
            log(f'recovered after {st["fails"]} failed checks')
        st['fails'] = 0
        tok = ENV.get('WATCHDOG_TOKEN')
        if tok:
            http('POST', URL + '/api/watchdog/ping', {'host': socket.gethostname(), 'note': f'healthz ok, orchestrator lag {lag}s'}, {'Authorization': 'Bearer ' + tok})
    else:
        st['fails'] += 1
        log(f'health check failed ({st["fails"]}): HTTP {code} {str(body)[:120]!r} lag={lag}')
        if st['fails'] >= 2 and time.time() - st.get('last_action', 0) > COOLDOWN:
            c, proj = liara('GET', f'/v1/projects/{APP}')
            p = proj.get('project', {}) if isinstance(proj, dict) else {}
            if c != 200:
                log(f'cannot read project from Liara API (HTTP {c}); no action')
            elif p.get('scale') == 0:
                c, _ = liara('POST', f'/v1/projects/{APP}/actions/scale', {'scale': 1})
                log(f'ACTION scale {APP} back on -> HTTP {c}')
                st['last_action'] = time.time()
            else:
                c, _ = liara('POST', f'/v1/projects/{APP}/actions/restart')
                log(f'ACTION restart {APP} (status={p.get("status")}) -> HTTP {c}')
                st['last_action'] = time.time()
    json.dump(st, open(STATE, 'w'))


if __name__ == '__main__':
    main()
