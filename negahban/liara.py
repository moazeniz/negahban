"""Async Liara API client. Every endpoint used by Negahban lives here, with retries, timeouts and redacted errors.
Verified live against https://api.iran.liara.ir (team mode: ?teamID= on every call). See docs/LIARA_API.md."""
import asyncio, io, json, logging, os, tarfile, time
import httpx
from .config import settings
from .redact import redact

log = logging.getLogger('negahban.liara')
TERMINAL = {'READY', 'FAILED', 'CANCELED', 'TIMEDOUT', 'UNHEALTHY'}
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.next', '.idea', '.vscode', 'dist', '.negahban'}


class LiaraError(Exception):
    def __init__(self, status, message, path='', data=None):
        self.status, self.message, self.path, self.data = status, message, path, data
        super().__init__(f'Liara {status} {path}: {message}')

    def fa(self):
        m = {401: 'توکن لیارا نامعتبر است', 403: 'دسترسی تیم کافی نیست', 404: 'برنامه یا منبع پیدا نشد', 409: 'تعارض (احتمالاً وضعیت فعلی همین است)',
             413: 'حجم سورس بیش از حد مجاز پلن است', 428: 'محدودیت پلن/باندل لیارا', 402: 'اعتبار حساب کافی نیست'}
        return m.get(self.status, 'خطای لیارا') + f' — {self.message}'


def make_tarball(src, extra_skip=()):
    """gzip tar of a source dir, skipping VCS/venv/secrets. Returns bytes."""
    buf = io.BytesIO()
    skip = SKIP_DIRS | set(extra_skip)
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for root, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d not in skip)
            for f in sorted(files):
                if f.endswith(('.pyc', '.db', '.sqlite', '.log')) or f.startswith('.env'):
                    continue
                p = os.path.join(root, f)
                ti = tf.gettarinfo(p, arcname=os.path.relpath(p, src))
                ti.uid = ti.gid = 0; ti.uname = ti.gname = ''
                ti.mtime = 1700000000  # deterministic archive => stable fingerprints
                with open(p, 'rb') as fh:
                    tf.addfile(ti, fh)
    return buf.getvalue()


class Liara:
    def __init__(self, token=None, team=None, base=None, transport=None, dns_base=None):
        self.token = token if token is not None else settings.liara_token
        self.team = team if team is not None else settings.liara_team
        self.base = (base or settings.liara_base).rstrip('/')
        self.dns_base = (dns_base or settings.liara_dns_base).rstrip('/')
        self._transport = transport
        self._client = None
        self.calls = 0

    def client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(30, connect=10),
                                             headers={'Authorization': f'Bearer {self.token}', 'User-Agent': 'negahban-sre/1.0'})
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def req(self, method, path, *, json_body=None, params=None, files=None, base=None, timeout=None, retries=None):
        url = (base or self.base) + path
        params = dict(params or {})
        if self.team:
            params['teamID'] = self.team
        retries = (3 if method == 'GET' else 1) if retries is None else retries
        from .resilience import CircuitOpen, backoff, breaker, log_call
        br = breaker('liara-dns' if base else 'liara', threshold=6, cooldown=30)
        last = None
        for attempt in range(retries):
            try:
                br.check()
            except CircuitOpen as e:
                raise LiaraError(503, f'Liara API circuit open (retry in {e.retry_in:.0f}s)', path)
            t0 = time.perf_counter()
            try:
                self.calls += 1
                r = await self.client().request(method, url, json=json_body, params=params, files=files,
                                                timeout=timeout or (300 if files else 30))
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
                br.fail()
                log_call('liara', method, path, 0, (time.perf_counter() - t0) * 1000, attempt + 1, retries, type(e).__name__)
                last = LiaraError(0, f'network: {type(e).__name__}', path)
                # a POST whose request may already have been received is not retried (not idempotent)
                if method != 'GET' and not isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                    raise last
                if attempt < retries - 1:
                    await asyncio.sleep(backoff(attempt))
                continue
            log_call('liara', method, path, r.status_code, (time.perf_counter() - t0) * 1000, attempt + 1, retries)
            if r.status_code >= 500:
                br.fail()
            else:
                br.ok()
            if (r.status_code >= 500 or r.status_code == 429) and method == 'GET' and attempt < retries - 1:
                last = LiaraError(r.status_code, r.text[:200], path)
                await asyncio.sleep(backoff(attempt, retry_after=r.headers.get('retry-after')))
                continue
            data = None
            if r.content:
                try:
                    data = r.json()
                except ValueError:
                    data = r.text[:2000]
            if r.status_code >= 400:
                msg = data.get('message') if isinstance(data, dict) else str(data)[:300]
                if isinstance(data, dict) and isinstance(data.get('data'), dict) and data['data'].get('code'):
                    msg = f"{msg} ({data['data']['code']})"
                raise LiaraError(r.status_code, redact(msg or r.reason_phrase), path, data)
            return data
        raise last or LiaraError(0, 'unknown', path)

    get = lambda self, p, **kw: self.req('GET', p, **kw)
    post = lambda self, p, body=None, **kw: self.req('POST', p, json_body=body, **kw)
    delete = lambda self, p, **kw: self.req('DELETE', p, **kw)

    # ---- account / plans -------------------------------------------------
    async def me(self):
        return await self.get('/v1/me')

    async def plans(self):
        me = await self.me()
        return me.get('plans', {}), me.get('team') or {}, me.get('user') or {}

    async def billing(self):
        return await self.get('/v1/billing')

    async def usage_report(self):
        return await self.get('/v1/usage-report')

    # ---- apps --------------------------------------------------------------
    async def list_apps(self):
        return (await self.get('/v1/projects')).get('projects', [])

    async def app(self, name):
        return (await self.get(f'/v1/projects/{name}')).get('project', {})

    async def create_app(self, name, plan, bundle='basic', platform='docker', network=None):
        body = {'name': name, 'planID': plan, 'bundlePlanID': bundle, 'platform': platform}
        if 'g2' in plan:
            body['network'] = network or settings.liara_network
        return await self.post('/v1/projects', body)

    async def delete_app(self, name):
        return await self.delete(f'/v1/projects/{name}')

    async def restart(self, name):
        return await self.post(f'/v1/projects/{name}/actions/restart')

    async def scale(self, name, on: bool):
        return await self.post(f'/v1/projects/{name}/actions/scale', {'scale': 1 if on else 0})

    async def resize(self, name, plan):
        return await self.post(f'/v1/projects/{name}/resize', {'planID': plan})

    async def resize_bundle(self, name, bundle):
        return await self.post(f'/v1/projects/{name}/resize-bundle-plan', {'bundlePlanID': bundle})

    async def zero_downtime(self, app_id, enable: bool):
        return await self.post(f"/v1/projects/{app_id}/zero-downtime/{'enable' if enable else 'disable'}")

    async def update_envs(self, name, variables: dict):
        """Full replacement of the env list (Liara semantics). Creates an ENV_UPDATE release and restarts the app."""
        return await self.post('/v1/projects/update-envs', {'project': name, 'variables': [{'key': k, 'value': str(v)} for k, v in variables.items()]})

    async def envs(self, name):
        return {e['key']: e.get('value', '') for e in (await self.app(name)).get('envs', [])}

    # ---- deploy -----------------------------------------------------------
    async def upload_source(self, name, tar_bytes):
        files = {'file': ('source.tar.gz', tar_bytes, 'application/gzip')}
        return (await self.req('POST', f'/v2/projects/{name}/sources', files=files, retries=2))['sourceID']

    async def create_release(self, name, source_id, port, envs=None, disks=None, message='negahban deploy', health_check=None):
        body = {'sourceID': source_id, 'port': int(port), 'type': 'docker', 'message': message[:200]}
        if envs is not None:
            body['envs'] = {k: str(v) for k, v in envs.items()}
        if disks:
            body['disks'] = disks
        if health_check:
            body['healthCheck'] = health_check
        return (await self.post(f'/v2/projects/{name}/releases', body)).get('releaseID')

    async def releases(self, name, count=10):
        return await self.get(f'/v1/projects/{name}/releases', params={'page': 1, 'count': count})

    async def release(self, release_id):
        return (await self.get(f'/v1/releases/{release_id}')).get('release', {})

    async def build_logs(self, release_id, since=''):
        return await self.get(f'/v2/releases/{release_id}/build-logs', params={'since': since or '1'})

    async def build_log_lines(self, release_id, max_pages=40):
        """All build output lines (paged by `since` = createdAt of the last line) + the release object."""
        since, lines, rel = '', [], {}
        for _ in range(max_pages):
            d = await self.build_logs(release_id, since)
            rel = d.get('release') or rel
            out = d.get('buildOutput') or []
            new = [x for x in out if x.get('createdAt') != since or x.get('line') not in lines[-5:]]
            if not out or (since and out[-1].get('createdAt') == since):
                break
            lines.extend(x.get('line', '') for x in new)
            since = out[-1].get('createdAt')
        return lines, rel

    async def cancel_release(self, release_id):
        return await self.post(f'/v2/releases/{release_id}/cancel')

    async def rollback(self, release_id):
        """Console-only endpoint (not in the public spec): re-activates an earlier READY release."""
        return await self.post(f'/v1/releases/{release_id}/rollback')

    async def wait_release(self, name, release_id, timeout=600, on_state=None):
        t0, last = time.time(), None
        while time.time() - t0 < timeout:
            try:
                rel = await self.release(release_id)
                st = rel.get('state') or rel.get('status')
            except LiaraError as e:
                if e.status == 404:
                    st = None
                else:
                    raise
            if st is None:  # fall back to the list endpoint
                lst = (await self.releases(name, 5)).get('releases', [])
                st = next((r['state'] for r in lst if r['_id'] == release_id), 'UNKNOWN')
                rel = next((r for r in lst if r['_id'] == release_id), {})
            if st != last and on_state:
                await on_state(st)
            last = st
            if st in TERMINAL:
                return st, rel
            await asyncio.sleep(5)
        return 'WAIT_TIMEOUT', {}

    async def deploy_dir(self, name, src, port, envs=None, disks=None, message='negahban deploy', health_check=None):
        tb = make_tarball(src)
        sid = await self.upload_source(name, tb)
        rid = await self.create_release(name, sid, port, envs, disks, message, health_check)
        return rid, tb

    async def deploy_tar(self, name, tar_bytes, port, envs=None, disks=None, message='negahban deploy', health_check=None):
        sid = await self.upload_source(name, tar_bytes)
        return await self.create_release(name, sid, port, envs, disks, message, health_check)

    # ---- observability ------------------------------------------------------
    async def logs(self, name, start=None, end=None, search=None, release_id=None, max_lines=600):
        """v2 logs, flattened and sorted oldest->newest: [{ts(float s), type, line, release}].
        The API returns at most 100 lines per call (the newest in the window), so older lines are paged by moving `end`."""
        end = int(end or time.time())
        start = int(start or end - 900)
        out, seen = [], set()
        cur_end = end
        for _ in range(max(1, max_lines // 100)):
            params = {'start': start, 'end': cur_end}
            if search and len(search) >= 3:
                params['search'] = search
            if release_id:
                params['releaseId'] = release_id
            data = await self.get(f'/v2/projects/{name}/logs', params=params, timeout=30)
            page, n_raw = [], 0
            for grp in (data or {}).get('data', []):
                rel = (grp.get('metaData') or {}).get('releaseId')
                for ts, raw in grp.get('values', []):
                    n_raw += 1
                    if (ts, raw) in seen:
                        continue
                    seen.add((ts, raw))
                    try:
                        j = json.loads(raw); line, typ = j.get('_entry', ''), j.get('type', 'stdout')
                    except (ValueError, TypeError):
                        line, typ = str(raw), 'stdout'
                    page.append({'ts': int(ts) / 1e9, 'type': typ, 'line': line, 'release': rel})
            out.extend(page)
            if n_raw < 100:  # the API returned everything left in the window
                break
            if not page:  # nothing new on this page: done (also guards against >100 lines inside one second)
                break
            oldest = min(x['ts'] for x in page)
            if oldest <= start + 1:
                break
            # `end` has second resolution: include the boundary second again (+1) so older lines of that second are not
            # lost; the `seen` set drops the duplicates
            cur_end = int(oldest) + 1
        out.sort(key=lambda x: x['ts'])
        return out

    async def events(self, name, start=None, end=None):
        end = int(end or time.time()); start = int(start or end - 3600)
        data = await self.get(f'/v1/projects/{name}/events', params={'start': start, 'end': end})
        evs = []
        for item in (data or {}).get('data', []):
            if isinstance(item, dict) and 'action' in item:
                try:
                    t = int(float(item.get('time') or 0))
                except (TypeError, ValueError):
                    t = 0
                evs.append({'time': t, 'action': item.get('action'), 'id': item.get('id'), 'release': item.get('release'), 'tag': item.get('tag')})
            elif isinstance(item, dict) and 'values' in item:  # spec shape (loki-like)
                for v in item['values']:
                    try:
                        j = json.loads(v[1]) if len(v) > 1 else {}
                        evs.append({'time': int(v[0]) / 1e9 if len(str(v[0])) > 12 else int(v[0]), 'action': j.get('action') or j.get('status') or str(j)[:60], 'id': j.get('id')})
                    except (ValueError, TypeError, IndexError):
                        pass
        evs.sort(key=lambda e: e.get('time') or 0)
        return evs

    async def metrics(self, name, kind='cpu', since=None):
        since = int(since or time.time() - 3600)
        data = await self.get(f'/v1/projects/{name}/metrics/{kind}', params={'since': since})
        agg = {}  # several applets (e.g. old+new container during a deploy) => sum per timestamp
        for r in (data or {}).get('result', []):
            for t, v in r.get('values', []):
                agg[int(t)] = agg.get(int(t), 0.0) + float(v)
        return sorted(agg.items())

    async def metrics_summary(self, name):
        d = await self.get(f'/v1/projects/{name}/metrics/summary')
        f = lambda key: sum(float(x['value'][1]) for x in d.get(key, []) if x.get('value'))
        return {'cpu': f('cpuUsage'), 'mem': f('memoryUsage'), 'disks': d.get('disksUsage', []), 'instances': len(d.get('cpuUsage', []))}

    async def disks(self, app_id):
        return await self.get(f'/v1/projects/{app_id}/disks')

    async def create_disk(self, name, disk, size_gb):
        return await self.post(f'/v1/projects/{name}/disks', {'name': disk, 'size': str(size_gb)})

    # ---- domains / DNS -------------------------------------------------------
    async def domains(self, app=None):
        return (await self.get('/v1/domains', params={'project': app} if app else None)).get('domains', [])

    async def add_domain(self, domain, app):
        return await self.post('/v1/domains', {'name': domain, 'type': 'PROJECT', 'project': app})

    async def domain(self, domain_id):
        return (await self.get(f'/v1/domains/{domain_id}')).get('domain', {})

    async def check_domain(self, domain_id):
        return (await self.get(f'/v1/domains/{domain_id}/check', retries=1)).get('domain', {})

    async def enable_ssl(self, domain):
        return await self.post('/v1/domains/provision-ssl-certs', {'domain': domain})

    async def delete_domain(self, domain_id):
        return await self.delete(f'/v1/domains/{domain_id}')

    async def zones(self):
        return await self.get('/zones', base=self.dns_base)

    async def create_zone(self, name):
        return await self.post('/zones', {'name': name}, base=self.dns_base)

    async def zone(self, name):
        return await self.get(f'/zones/{name}', base=self.dns_base)

    async def check_zone(self, name):
        return await self.req('PUT', f'/zones/{name}/check', base=self.dns_base)

    async def dns_records(self, zone):
        return await self.get(f'/zones/{zone}/dns-records', base=self.dns_base)

    async def add_dns_record(self, zone, name, rtype, contents, ttl=3600):
        return await self.post(f'/zones/{zone}/dns-records', {'name': name, 'type': rtype, 'ttl': ttl, 'contents': contents}, base=self.dns_base)


liara = Liara()
