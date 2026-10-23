"""Liara data the core client does not wrap: release detail + paged build logs, event timeline, network metrics, disks/backups,
the disk file browser (read-only listing + small text downloads), DBaaS (list/status + read-only inspector queries), object
storage buckets, billing/invoices and DNS zones/domains with live TLS state. All calls go through the shared core client
(negahban.liara.liara: bearer, ?teamID=, retries, redacted errors).
Verified live 2026-10-22: GET /v1/projects/{_id}/disks -> disks[].filebrowserUrl (e.g. https://filebrowser-sabalan.liara.cloud);
GET {filebrowserUrl}/v1/projects/{_id}/disks/{disk}/list?path=./&page=1&count=20 -> {items:[{path,name,size,isDir,modifiedAt}]};
POST .../createlink?path=&expire=1h&type=download -> {link}; disk backups/metrics need the project _id (the name gives 404)."""
import asyncio, re, time
import httpx
from ..liara import LiaraError, liara
from ..redact import redact
from .common import DiagError, audit, spec, text

STORAGE = 'https://storage-service.liara.ir/api/v1'
APP = {'app': {'type': 'string', 'description': 'Liara app name'}}
_transport = None  # test hook for file downloads
_ids = {}


async def app_id(app):
    if app not in _ids:
        _ids[app] = (await liara.app(app)).get('_id')
        if not _ids[app]:
            raise DiagError(f'app {app} not found')
    return _ids[app]


def _iso(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts)) if ts else None


def _secs(a, b):
    from datetime import datetime
    try:
        f = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00'))
        return round((f(b) - f(a)).total_seconds())
    except (TypeError, ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- releases / build logs
async def find_release(app, release_id=None, tag=None):
    rels = await liara.releases(app, 30)
    lst = rels.get('releases') or []
    if release_id:
        return next((r for r in lst if r['_id'] == release_id), {'_id': release_id}), rels.get('currentRelease')
    if tag:
        t = 'v' + str(tag).lstrip('v')
        r = next((r for r in lst if r.get('tag') == t), None)
        if not r:
            raise DiagError(f'release {t} not found')
        return r, rels.get('currentRelease')
    if not lst:
        raise DiagError('no releases')
    return lst[0], rels.get('currentRelease')


async def build_log_page(release_id, since=''):
    d = await liara.get(f'/v2/releases/{release_id}/build-logs', params={'since': since or '1'})
    return d.get('release') or {}, d.get('buildOutput') or []


async def release_details(app, release_id=None, tag=None, max_lines=400):
    base, current = await find_release(app, release_id, tag)
    rid = base['_id']
    detail = {}
    try:
        detail = (await liara.get(f'/v1/releases/{rid}')).get('release') or {}
    except LiaraError as e:
        detail = {'_error': e.status}
    lines, since, stderr = [], '', []
    for _ in range(30):
        _, out = await build_log_page(rid, since)
        new = [x for x in out if x.get('createdAt') != since]
        if not new:
            break
        for x in new:
            lines.append(x.get('line', '').rstrip())
            if x.get('stream') == 'STDERR':
                stderr.append(x.get('line', '').rstrip())
        since = out[-1].get('createdAt')
        if len(lines) >= 4000:
            break
    from ..analysis import parse_build_logs
    r = {**base, **{k: v for k, v in detail.items() if not k.startswith('_')}}
    return {'release_id': rid, 'tag': r.get('tag'), 'type': r.get('type'), 'state': r.get('state') or r.get('status'), 'fail_reason': r.get('failReason'),
            'queue': r.get('queue'), 'is_current': rid == current, 'message': r.get('message'), 'created_at': r.get('createdAt'), 'finished_at': r.get('finishedAt'),
            'duration_s': _secs(r.get('createdAt'), r.get('finishedAt')), 'build_location': r.get('buildLocation'), 'port': r.get('port'),
            'image': (r.get('imageName') or '').split('/')[-1] or None, 'source_available': r.get('sourceAvailable'),
            'build_lines': len(lines), 'stderr_lines': [text(l, 300) for l in stderr[-20:]], 'build_summary': parse_build_logs(lines),
            'tail': [text(l, 300) for l in lines[-max_lines:][-60:]]}


# ---------------------------------------------------------------- events / network
async def events_timeline(app, hours=24):
    evs = await liara.events(app, start=time.time() - hours * 3600)
    counts, rows, execs = {}, [], 0
    for e in evs:
        act = str(e.get('action') or '')
        if act.startswith('exec_'):
            execs += 1
            continue
        k = act.split(':')[0]
        counts[k] = counts.get(k, 0) + 1
        rows.append({'time': _iso(e.get('time')), 'action': k, 'release': e.get('tag') or e.get('release')})
    starts_per_rel = {}
    for r in rows:
        if r['action'] == 'start':
            starts_per_rel[r['release']] = starts_per_rel.get(r['release'], 0) + 1
    return {'hours': hours, 'counts': counts, 'exec_sessions': execs, 'oom': counts.get('oom', 0), 'crashes': counts.get('die', 0),
            'unplanned_restarts': sum(max(0, n - 1) for n in starts_per_rel.values()), 'events': rows[-80:]}


async def network(app, hours=6):
    since = time.time() - hours * 3600
    rx, tx = await asyncio.gather(liara.metrics(app, 'network-receive', since), liara.metrics(app, 'network-transmit', since))
    s = lambda xs: {'now_kbs': round(xs[-1][1] / 1024, 2) if xs else None, 'avg_kbs': round(sum(v for _, v in xs) / len(xs) / 1024, 2) if xs else None,
                    'peak_kbs': round(max(v for _, v in xs) / 1024, 2) if xs else None, 'points': len(xs)}
    step = max(1, len(rx) // 100)
    return {'hours': hours, 'receive': s(rx), 'transmit': s(tx),
            '_attach': {'kind': 'chart', 'type': 'line', 'title': f'ترافیک شبکه‌ی {app}', 'unit': 'KB/s',
                        'series': [{'name': 'دریافت', 'points': [[t, round(v / 1024, 2)] for t, v in rx[::step]]},
                                   {'name': 'ارسال', 'points': [[t, round(v / 1024, 2)] for t, v in tx[::step]]}]}}


# ---------------------------------------------------------------- disks, backups, file browser
async def disks(app, with_backups=True):
    pid = await app_id(app)
    d = await liara.get(f'/v1/projects/{pid}/disks')
    mounts = {m.get('name'): m.get('mountedTo') for m in d.get('mounts') or []}
    out = []
    for dk in d.get('disks') or []:
        row = {'name': dk['name'], 'size_gb': dk.get('size'), 'mounted_to': mounts.get(dk['name']), 'created_at': dk.get('createdAt'), 'file_browser': bool(dk.get('filebrowserUrl'))}
        try:
            m = (await liara.get(f"/v1/projects/{pid}/disks/{dk['name']}/metrics")).get('result') or {}
            row['usage_pct'] = float(m.get('usage')) if m.get('usage') not in (None, '') else None
            row['reported_at'] = _iso(m.get('reportedAt'))
        except (LiaraError, ValueError, TypeError):
            pass
        if with_backups:
            try:
                b = (await liara.get(f"/v1/projects/{pid}/disks/{dk['name']}/backups")).get('backups') or []
                row['backups'] = [{'name': x.get('name'), 'at': x.get('lastModified'), 'mb': round((x.get('size') or 0) / 2 ** 20, 1)} for x in b][-10:]
                row['backup_count'] = len(b)
            except LiaraError as e:
                row['backups_error'] = e.status
        out.append(row)
    return {'app': app, 'disks': out, 'note': None if out else 'this app has no disks (container filesystem is ephemeral)'}


async def _fb_base(app, disk):
    pid = await app_id(app)
    d = await liara.get(f'/v1/projects/{pid}/disks')
    dk = next((x for x in d.get('disks') or [] if x.get('name') == disk), None)
    if not dk:
        raise DiagError(f'disk {disk} not found on {app}')
    if not dk.get('filebrowserUrl'):
        raise DiagError('file browser is not available for this disk')
    return pid, dk['filebrowserUrl'].rstrip('/')


_SECRETISH = re.compile(r'(?i)(^|/)\.env|\.pem$|\.key$|id_rsa|credential|secret|\.p12$|\.pfx$|\.npmrc|\.pypirc|\.netrc')


def _rel(path):
    p = '/' + re.sub(r'^(\./|/)+', '', (path or '').strip())
    if '..' in p.split('/'):
        raise DiagError('path must not contain ..')
    return '.' + p if p != '/' else './'


async def list_files(app, disk='data', path='/', page=1, sort='name'):
    pid, base = await _fb_base(app, disk)
    d = await liara.get(f'/v1/projects/{pid}/disks/{disk}/list', base=base,
                        params={'path': _rel(path), 'page': page, 'count': 50, 'sortType': sort, 'sortDirection': 'desc' if sort != 'name' else 'asc'})
    items = [{'name': x.get('name'), 'path': x.get('path'), 'dir': x.get('isDir'), 'kb': round((x.get('size') or 0) / 1024, 1), 'modified': x.get('modifiedAt')}
             for x in d.get('items') or []]
    return {'app': app, 'disk': disk, 'path': d.get('path') or path, 'dirs': d.get('numDirs'), 'files': d.get('numFiles'), 'page': page, 'items': items}


async def read_file(app, disk, path, max_kb=200):
    if _SECRETISH.search(path or ''):
        raise DiagError('this file looks like a secret store; it is not read by Negahban')
    pid, base = await _fb_base(app, disk)
    rel = _rel(path)
    ln = await liara.post(f'/v1/projects/{pid}/disks/{disk}/createlink', base=base, params={'path': rel, 'expire': '1h', 'type': 'download'})
    link = (ln or {}).get('link')
    if not link:
        raise DiagError('file browser did not return a download link')
    cap = max(1, min(int(max_kb), 200)) * 1024
    buf, size = b'', 0
    async with httpx.AsyncClient(timeout=30, transport=_transport, follow_redirects=True) as c:
        async with c.stream('GET', link) as r:
            if r.status_code >= 400:
                raise DiagError(f'download failed: HTTP {r.status_code}')
            total = int(r.headers.get('content-length') or 0)
            async for ch in r.aiter_bytes():
                size += len(ch)
                buf += ch[:cap + 1 - len(buf)] if len(buf) <= cap else b''
                if len(buf) > cap:
                    break
    if b'\x00' in buf[:8192]:
        raise DiagError('binary file; only text files are returned (use container_diag sqlite_check for databases)')
    s = buf[:cap].decode('utf-8', 'replace')
    return {'app': app, 'disk': disk, 'path': rel, 'bytes': total or size, 'truncated': len(buf) > cap or (total and total > cap),
            'content': redact(text(s, cap))}


# ---------------------------------------------------------------- DBaaS
async def databases(db_id=None):
    if db_id:
        d = (await liara.get(f'/v1/databases/{db_id}')).get('database') or {}
        out = {k: d.get(k) for k in ('_id', 'hostname', 'type', 'version', 'status', 'scale', 'planID', 'bundlePlanID', 'publicNetwork', 'node', 'created_at')}
        try:
            s = await liara.get(f'/v1/databases/{db_id}/metrics/summary')
            out['metrics'] = s
        except LiaraError as e:
            out['metrics_error'] = e.status
        try:
            out['backups'] = [{'name': b.get('name'), 'at': b.get('lastModified')} for b in (await liara.get(f'/v1/databases/{db_id}/backups')).get('backups') or []][-10:]
        except LiaraError as e:
            out['backups_error'] = e.status
        return out
    dbs = (await liara.get('/v1/databases')).get('databases') or []
    return {'count': len(dbs), 'databases': [{k: d.get(k) for k in ('_id', 'hostname', 'type', 'version', 'status', 'scale', 'planID', 'bundlePlanID', 'publicNetwork')} for d in dbs]}


_READONLY_SQL = re.compile(r'^\s*(select|show|explain|describe|desc|with|pragma\s+\w+\s*$|values)\b', re.I | re.S)
_WRITE_SQL = re.compile(r'\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|replace\s+into|call|exec|execute|copy|vacuum|reindex|attach|detach|lock|set|'
                        r'into\s+outfile|load_file|pg_sleep|sleep\s*\(|benchmark\s*\(|dblink|pg_read_file|pg_ls_dir|lo_import)\b', re.I)


def check_readonly_sql(q):
    s = re.sub(r"'(?:[^']|'')*'", "''", q or '')  # ignore literals when looking for keywords
    s = re.sub(r'--[^\n]*|/\*.*?\*/', ' ', s, flags=re.S)
    if ';' in s.strip().rstrip(';'):
        raise DiagError('only one statement per query')
    if not _READONLY_SQL.match(s) or _WRITE_SQL.search(s):
        raise DiagError('only read-only queries (SELECT/SHOW/EXPLAIN/DESCRIBE/WITH ... SELECT) are allowed')
    return q.strip().rstrip(';')


async def db_query(db_id, query, database=None, max_rows=100):
    q = check_readonly_sql(query)
    d = (await liara.get(f'/v1/databases/{db_id}')).get('database') or {}
    base = (d.get('databaseInspectorUrl') or '').rstrip('/')
    if not base:
        raise DiagError('the database inspector is not available for this database')
    db = database or d.get('defaultDatabase') or d.get('dbName') or 'postgres'
    if not re.match(r'^[\w.-]{1,64}$', db):
        raise DiagError('invalid database name')
    try:  # console form first, then the published spec form
        r = await liara.post(f'/databases/{db_id}/v1/{db}/queries', {'query': q}, base=base)
    except LiaraError as e:
        if e.status == 400 and 'not a supported database server' in str(e.message):  # verified live for postgres 18 (2026-10-23)
            raise DiagError(f"بازرس پایگاه‌داده‌ی لیارا موتور {d.get('type')} را پشتیبانی نمی‌کند؛ برای این دیتابیس کوئری از طریق نگهبان ممکن نیست.")
        if e.status != 404:
            raise
        r = await liara.post(f'/v1/databases/{db_id}/queries', {'query': q}, base=base)
    rows = r.get('data') if isinstance(r, dict) else r
    if isinstance(rows, dict):
        rows = rows.get('rows') or rows.get('data') or rows
    rows = rows if isinstance(rows, list) else [rows]
    return {'database': db, 'query': q, 'row_count': len(rows), 'rows': rows[:max_rows], 'truncated': len(rows) > max_rows}


# ---------------------------------------------------------------- storage / billing / dns / domains
async def buckets():
    b = (await liara.get('/buckets', base=STORAGE)).get('buckets') or []
    return {'count': len(b), 'buckets': [{k: x.get(k) for k in ('name', 'plan', 'permission', 'status', 'createdAt', 'region')} for x in b]}


async def billing():
    bill, usage = await asyncio.gather(liara.billing(), liara.usage_report())
    inv = {}
    try:
        inv = await liara.get('/v1/billing-invoices', params={'page': 1, 'count': 5})
    except LiaraError:
        pass
    acc = bill.get('team') or bill.get('user') or {}
    hourly = usage.get('totalHourlyPrice') or 0
    bal = acc.get('balance')
    return {'balance_toman': bal, 'min_credit_alert_toman': acc.get('minCreditAmount'), 'run_rate_toman_per_hour': round(hourly, 1),
            'run_rate_toman_per_day': round(hourly * 24), 'run_rate_toman_per_month': round(usage.get('totalMonthlyPrice') or hourly * 720),
            'hours_left': round(bal / hourly, 1) if bal and hourly else None,
            'days_left': round(bal / hourly / 24, 1) if bal and hourly else None,
            'apps': sorted([{'app': p.get('project_id'), 'plan': p.get('planID'), 'bundle': p.get('bundlePlanID'), 'toman_per_hour': p.get('totalHourlyPrice')}
                            for p in usage.get('projects') or []], key=lambda x: -(x['toman_per_hour'] or 0)),
            'other_services': {k: len(usage.get(k) or []) for k in ('databases', 'buckets', 'mailservers', 'VMs')},
            'invoices': [{k: i.get(k) for k in ('_id', 'createdAt', 'amount', 'totalAmount', 'status', 'from', 'to')} for i in inv.get('billingInvoices') or []],
            'invoice_count': inv.get('totalBillingInvoices')}


async def dns_zones(zone=None):
    if zone:
        z, recs = await asyncio.gather(liara.zone(zone), liara.dns_records(zone))
        return {'zone': (z or {}).get('data') or z, 'records': [{k: r.get(k) for k in ('name', 'type', 'ttl', 'contents')} for r in ((recs or {}).get('data') or [])]}
    zs = await liara.zones()
    return {'zones': [{k: z.get(k) for k in ('name', 'status', 'nameServers', 'currentNameServers', 'lastCheckAt')} for z in (zs or {}).get('data') or []]}


async def domains_health(app=None, tls=True):
    doms = await liara.domains(app)
    rows = [{k: d.get(k) for k in ('_id', 'name', 'project', 'status', 'certificatesStatus', 'type', 'redirectTo')} for d in doms]
    if tls:
        from .probes import tls_check
        live = [r for r in rows if r.get('status') == 'ACTIVE'][:6]
        res = await asyncio.gather(*[tls_check(r['name']) for r in live], return_exceptions=True)
        for r, t in zip(live, res):
            if isinstance(t, dict):
                r['tls'] = {'ok': t.get('ok'), 'days_left': (t.get('cert') or {}).get('days_left'), 'issuer': (t.get('cert') or {}).get('issuer'),
                            'error': t.get('verify_error') or t.get('error')}
    problems = [r['name'] for r in rows if r.get('status') != 'ACTIVE' or r.get('certificatesStatus') not in (None, 'ACTIVE')
                or (r.get('tls') and (not r['tls']['ok'] or (r['tls']['days_left'] or 99) < 14))]
    return {'count': len(rows), 'domains': rows, 'problems': problems,
            'note': 'default *.liara.run subdomains are not listed here; probe them with tls_check/http_probe' if not rows else None}


# ---------------------------------------------------------------- tools
@spec('release_details', 'در حال خواندن جزئیات نسخه و لاگ بیلد…', 'read',
      'Full detail of one release (default: latest; or release_id / tag like v12): state, failReason, queue, duration, image, whether it is current, '
      'STDERR lines and a build-failure summary from the complete paged build log.',
      {**APP, 'release_id': {'type': 'string'}, 'tag': {'type': 'string', 'pattern': '^v?[0-9]{1,5}$'}}, ['app'], timeout=60)
async def release_details_tool(a, ctx):
    return await release_details(a['app'], a.get('release_id'), a.get('tag'))


@spec('events_timeline', 'در حال بررسی رویدادهای کانتینر…', 'read',
      'Container lifecycle timeline (start/die/oom/kill/restart) with counts, OOM kills, crashes and unplanned restarts (2nd+ start of the same release). '
      'Exec sessions are counted but hidden.', {**APP, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 168}}, ['app'])
async def events_timeline_tool(a, ctx):
    return await events_timeline(a['app'], int(a.get('hours') or 24))


@spec('network_metrics', 'در حال خواندن ترافیک شبکه…', 'read', 'Network receive/transmit (KB/s now, average, peak) for the last N hours, with a chart.',
      {**APP, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 168}}, ['app'])
async def network_metrics_tool(a, ctx):
    return await network(a['app'], int(a.get('hours') or 6))


@spec('disks_info', 'در حال بررسی دیسک‌ها و پشتیبان‌ها…', 'read', 'Disks of an app: size, mount path, usage %, and the list of backups (read-only).', APP, ['app'])
async def disks_info_tool(a, ctx):
    return await disks(a['app'])


@spec('disk_files', 'در حال فهرست کردن فایل‌های دیسک…', 'read', 'List files and folders on an app disk via the Liara file browser (read-only). sort: name|size|modTime.',
      {**APP, 'disk': {'type': 'string'}, 'path': {'type': 'string'}, 'page': {'type': 'integer', 'minimum': 1, 'maximum': 100},
       'sort': {'type': 'string', 'enum': ['name', 'size', 'modTime']}}, ['app'])
async def disk_files_tool(a, ctx):
    return await list_files(a['app'], a.get('disk') or 'data', a.get('path') or '/', int(a.get('page') or 1), a.get('sort') or 'name')


@spec('disk_file_read', 'در حال خواندن فایل متنی از دیسک…', 'read',
      'Read a small TEXT file (<= 200 KB, e.g. a log or config) from an app disk via the file browser. Secrets are redacted; binary files and secret stores are refused.',
      {**APP, 'disk': {'type': 'string'}, 'path': {'type': 'string'}, 'max_kb': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['app', 'path'])
async def disk_file_read_tool(a, ctx):
    r = await read_file(a['app'], a.get('disk') or 'data', a['path'], a.get('max_kb') or 200)
    audit(ctx, 'disk_file_read', a['app'], {'path': r['path'], 'bytes': r['bytes']})
    return r


@spec('databases_status', 'در حال بررسی دیتابیس‌ها…', 'read', 'Managed databases (DBaaS) of the team: type, version, status, plan; with db_id: metrics summary and backups.',
      {'db_id': {'type': 'string'}})
async def databases_status_tool(a, ctx):
    return await databases(a.get('db_id'))


def _dbq_title(a):
    return f"اجرای کوئری فقط‌خواندنی روی دیتابیس {a.get('db_id', '')[:12]}"


async def _dbq_impact(a):
    check_readonly_sql(a.get('query', ''))
    return ({'downtime': 'ندارد', 'cost': 'بدون هزینه', 'risk': 'کم (فقط SELECT) اما ممکن است داده‌ی کاربران را نشان دهد', 'query': a.get('query', '')[:400]},
            'کوئری فقط‌خواندنی است و چیزی را تغییر نمی‌دهد.')


@spec('db_query', 'ساخت کارت تأیید برای کوئری دیتابیس…', 'sensitive',
      'Run ONE read-only SQL query (SELECT/SHOW/EXPLAIN/WITH) on a managed database through the Liara database inspector; rows are capped at 100. '
      'Needs approval because results can contain user data.',
      {'db_id': {'type': 'string'}, 'database': {'type': 'string'}, 'query': {'type': 'string', 'maxLength': 2000}, 'reason': {'type': 'string'}},
      ['db_id', 'query'], title=_dbq_title, impact=_dbq_impact, timeout=45)
async def db_query_tool(a, ctx):
    r = await db_query(a['db_id'], a['query'], a.get('database'))
    audit(ctx, 'db_query', None, {'db': a['db_id'], 'query': a['query'][:300], 'rows': r['row_count']})
    return r


@spec('storage_buckets', 'در حال بررسی فضای ذخیره‌سازی ابری…', 'read', 'Object storage buckets of the team (name, plan, permission, status).')
async def storage_buckets_tool(a, ctx):
    return await buckets()


@spec('billing_details', 'در حال بررسی اعتبار و صورتحساب…', 'read',
      'Team credit balance, low-credit alert threshold, run-rate (hour/day/month), hours/days of credit left, per-app hourly cost and recent invoices.')
async def billing_details_tool(a, ctx):
    return await billing()


@spec('dns_zones', 'در حال خواندن ناحیه‌های DNS…', 'read', 'Liara DNS zones (status, nameservers) or, with zone, all its records (read-only).', {'zone': {'type': 'string'}})
async def dns_zones_tool(a, ctx):
    return await dns_zones(a.get('zone'))


@spec('domains_health', 'در حال بررسی دامنه‌ها و SSL…', 'read',
      'Custom domains (all, or of one app) with verification and certificate status from Liara plus a live TLS check (days left, issuer, errors).',
      {'app': {'type': 'string'}, 'tls': {'type': 'boolean'}}, timeout=60)
async def domains_health_tool(a, ctx):
    return await domains_health(a.get('app'), a.get('tls', True))


# ---------------------------------------------------------------- balance precheck before creating anything
async def week_precheck(extra_hourly):
    """Liara refuses new resources (HTTP 429) unless the credit covers about one week of the team's run-rate including the
    new resource. Returns {ok, balance, need, run_rate_after, message_fa}; never raises for API errors (ok=None)."""
    try:
        bill, usage = await asyncio.gather(liara.billing(), liara.usage_report())
    except LiaraError as e:
        return {'ok': None, 'message_fa': f'بررسی موجودی ممکن نشد ({e.status})'}
    bal = (bill.get('team') or bill.get('user') or {}).get('balance')
    rate = (usage.get('totalHourlyPrice') or 0) + (extra_hourly or 0)
    need = round(rate * 24 * 7)
    ok = bal is None or bal >= need
    from ..teams import label
    tl = label()['label_fa']  # the precheck is per team: that team's balance and run-rate
    msg = None if ok else (f'{tl}: موجودی ({bal:,.0f} تومان) کمتر از هزینه‌ی یک هفته‌ی همه‌ی سرویس‌های این تیم با این مورد جدید ({need:,.0f} تومان) است؛ '
                           f'لیارا ساخت را با خطای ۴۲۹ رد می‌کند. ابتدا حساب این تیم را شارژ کنید یا در تیم دیگری بسازید.')
    return {'ok': ok, 'balance': bal, 'need': need, 'run_rate_after': round(rate, 1), 'message_fa': msg, 'team': tl}
