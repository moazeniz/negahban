"""GitOps: read the public repos + CI/CD of the apps and release through their own "Deploy to Liara" pipeline.

SECURITY: the GitHub token we were given is far more powerful than needed (contents:write, admin). This client enforces a
CODE-LEVEL ALLOWLIST: only read endpoints (repo, commits, compare, tags, releases, workflows/runs/jobs/logs, check runs,
statuses, the deploy workflow file) and ONE write: POST .../actions/workflows/deploy.yml/dispatches. Everything else
(commits, pushes, file writes, branches, refs, PRs, issues, releases creation, settings, other repos/owners) is refused
before any network call. Negahban never authors commits in these public repos."""
import asyncio, os, re, time
from datetime import datetime
import httpx
from .redact import redact

API = 'https://api.github.com'
OWNER = os.environ.get('GITHUB_OWNER', 'moazeniz')
APP_REPO = {'bazargard-ai': 'bazargard', 'negahban-sre': 'negahban', 'meowmeow-wallex': 'wallex-ai-trader'}
REPOS = set(APP_REPO.values())
NEVER_DISPATCH = {'meowmeow-wallex'}  # real-money trading app: monitor-only, never released by Negahban
PIPELINE_SELF_OK = {'negahban-sre'}  # write-locked for direct changes, but may be released through its own CI pipeline (with approval)
DEPLOY_WF = 'deploy.yml'
CI_WF = 'ci.yml'
_R = r'/repos/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)'
ALLOW_GET = [re.compile(_R + p + '$') for p in (
    '', r'/commits', r'/commits/[\w./-]+', r'/compare/[\w./~^-]+\.\.\.[\w./~^-]+', r'/tags', r'/releases', r'/releases/latest', r'/releases/\d+',
    r'/branches/[\w./-]+', r'/actions/workflows', r'/actions/workflows/[\w.-]+', r'/actions/workflows/[\w.-]+/runs', r'/actions/runs',
    r'/actions/runs/\d+', r'/actions/runs/\d+/jobs', r'/actions/jobs/\d+', r'/actions/jobs/\d+/logs', r'/commits/[\w./-]+/check-runs',
    r'/commits/[\w./-]+/status', r'/commits/[\w./-]+/statuses', r'/contents/\.github/workflows/[\w.-]+\.ya?ml')]
ALLOW_POST = [re.compile(_R + r'/actions/workflows/' + re.escape(DEPLOY_WF) + r'/dispatches$')]


_sleep = asyncio.sleep  # patched in tests


class GitHubBlocked(Exception):
    pass


class GitHubError(Exception):
    pass


def allowed(method, path):
    path = path.split('?', 1)[0]
    rules = ALLOW_GET if method == 'GET' else ALLOW_POST if method == 'POST' else []
    for rx in rules:
        m = rx.match(path)
        if m and m['owner'] == OWNER and m['repo'] in REPOS:
            return True
    return False


class GitHub:
    def __init__(self, token=None, transport=None):
        self.token = token if token is not None else os.environ.get('NEGAHBAN_GITHUB_TOKEN', '')
        self.transport = transport

    @property
    def configured(self):
        return bool(self.token)

    async def req(self, method, path, params=None, json_body=None, raw=False):
        if not allowed(method, path):  # enforced BEFORE any network I/O
            from .obs import event
            event('github.blocked', 'warn', f'blocked {method} {path}', method=method, route=path[:160])
            raise GitHubBlocked(f'GitHub call not allowed by Negahban policy: {method} {path}')
        if not self.token:
            raise GitHubError('NEGAHBAN_GITHUB_TOKEN is not configured')
        h = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'negahban-sre'}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=25, follow_redirects=raw) as c:
                r = await c.request(method, API + path, params=params, json=json_body, headers=h)
        except httpx.TransportError as e:
            raise GitHubError(f'GitHub unreachable ({type(e).__name__})')
        if r.status_code >= 400:
            raise GitHubError(f'GitHub {r.status_code}: {redact(r.text[:200])}')
        if raw:
            return r.text
        return r.json() if r.content else None

    get = lambda self, p, **kw: self.req('GET', p, **kw)


gh = GitHub()


def repo_of(app):
    r = APP_REPO.get(app)
    if not r:
        raise GitHubError(f'no GitHub repo is mapped to app {app} (mapped: {", ".join(APP_REPO)})')
    return r


def _p(repo, suffix=''):
    return f'/repos/{OWNER}/{repo}{suffix}'


def _ts(iso):
    try:
        return datetime.fromisoformat((iso or '').replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0


def _run(r):
    return {'id': r['id'], 'name': r.get('name'), 'status': r.get('status'), 'conclusion': r.get('conclusion'), 'event': r.get('event'),
            'sha': (r.get('head_sha') or '')[:7], 'branch': r.get('head_branch'), 'created_at': r.get('created_at'), 'url': r.get('html_url'),
            'title': (r.get('display_title') or '')[:120]}


async def live_commit(app):
    """Which commit is live on Liara. Exact when the release carries gitInfo (the GitHub pipeline records it) or a sha in its
    message; otherwise ESTIMATED as the last main commit before the release finished (marked estimated=True)."""
    from .liara import liara
    data = await liara.releases(app, 5)
    cur = next((r for r in data.get('releases', []) if r['_id'] == data.get('currentRelease')), None) or (data.get('releases') or [{}])[0]
    gi = cur.get('gitInfo') or {}
    sha, how = gi.get('commit') or '', 'gitInfo'
    msg = cur.get('message') or ''
    if not sha:
        m = re.search(r'\b([0-9a-f]{7,40})\b', msg)
        sha, how = (m.group(1), 'message') if m else ('', None)
    out = {'release_tag': cur.get('tag'), 'release_id': cur.get('_id'), 'deployed_at': cur.get('finishedAt'), 'message': msg[:160],
           'commit': sha[:40] or None, 'source': how, 'estimated': False, 'branch': gi.get('branch')}
    if not sha and cur.get('finishedAt'):
        try:
            cs = await gh.get(_p(repo_of(app), '/commits'), params={'per_page': 1, 'sha': 'main', 'until': cur['finishedAt']})
            if cs:
                out.update(commit=cs[0]['sha'], source='time', estimated=True)
        except Exception:
            pass
    return out


_cache = {}


async def overview(app, fresh=False):
    hit = _cache.get(app)
    if hit and not fresh and time.time() - hit[0] < 60:  # the dashboard polls; keep GitHub calls well under the rate limit
        return hit[1]
    res = await _overview(app)
    _cache[app] = (time.time(), res)
    return res


async def _overview(app):
    repo = repo_of(app)
    meta, commits, tags, rels, ci, dep = await asyncio.gather(
        gh.get(_p(repo)), gh.get(_p(repo, '/commits'), params={'per_page': 8}), gh.get(_p(repo, '/tags'), params={'per_page': 5}),
        gh.get(_p(repo, '/releases'), params={'per_page': 3}), gh.get(_p(repo, f'/actions/workflows/{CI_WF}/runs'), params={'per_page': 5}),
        gh.get(_p(repo, f'/actions/workflows/{DEPLOY_WF}/runs'), params={'per_page': 5}), return_exceptions=True)
    ok = lambda v, d: d if isinstance(v, Exception) else v
    commits = ok(commits, [])
    head = commits[0]['sha'] if commits else None
    status = None
    if head:
        try:
            cr = await gh.get(_p(repo, f'/commits/{head}/check-runs'), params={'per_page': 30})
            runs = cr.get('check_runs', [])
            status = {'total': len(runs), 'success': sum(1 for x in runs if x.get('conclusion') == 'success'),
                      'failed': [x['name'] for x in runs if x.get('conclusion') in ('failure', 'timed_out', 'cancelled')],
                      'pending': sum(1 for x in runs if x.get('status') != 'completed')}
        except Exception:
            status = None
    live = {}
    try:
        live = await live_commit(app)
    except Exception as e:
        live = {'error': str(e)[:160]}
    behind = None
    if live.get('commit') and head:
        try:
            cmp = await gh.get(_p(repo, f"/compare/{live['commit']}...{head}"))
            behind = {'status': cmp.get('status'), 'behind_main': cmp.get('ahead_by'), 'live_only': cmp.get('behind_by'), 'estimated': live.get('estimated')}
        except Exception:
            behind = {'unknown': f"کامیت {live['commit'][:7]} در این مخزن نیست (ریلیز فعلی از خارج از این مخزن مستقر شده)"}
    elif 'error' not in live:
        behind = {'unknown': 'ریلیز فعلی از پایپ‌لاین گیت‌هاب نیامده و اطلاعات git ندارد؛ پس از اولین انتشار از پایپ‌لاین دقیق می‌شود'}
    m = ok(meta, {})
    return {'app': app, 'repo': f'{OWNER}/{repo}', 'url': m.get('html_url') or f'https://github.com/{OWNER}/{repo}', 'default_branch': m.get('default_branch', 'main'),
            'commits': [{'sha': c['sha'][:7], 'message': c['commit']['message'].split('\n')[0][:120], 'author': (c['commit'].get('author') or {}).get('name'),
                         'date': (c['commit'].get('author') or {}).get('date')} for c in commits],
            'tags': [t['name'] for t in ok(tags, [])], 'releases': [{'tag': r.get('tag_name'), 'name': r.get('name'), 'published_at': r.get('published_at')} for r in ok(rels, [])],
            'ci_runs': [_run(r) for r in ok(ci, {}).get('workflow_runs', [])] if isinstance(ok(ci, {}), dict) else [],
            'deploy_runs': [_run(r) for r in ok(dep, {}).get('workflow_runs', [])] if isinstance(ok(dep, {}), dict) else [],
            'head_checks': status, 'live': live, 'live_vs_main': behind}


async def what_changed(app, base=None, head=None):
    """Commits + files between `base` (default: the live commit/tag) and `head` (default: main)."""
    repo = repo_of(app)
    if not base:
        live = await live_commit(app)
        base = live.get('commit')
    head = head or 'main'
    if base:
        try:
            await gh.get(_p(repo, f'/commits/{base}'))
        except GitHubError:
            base = None  # the live commit is not part of this repo's history
    if not base:
        cs = await gh.get(_p(repo, '/commits'), params={'per_page': 10, 'sha': head})
        top = await gh.get(_p(repo, f"/commits/{cs[0]['sha']}")) if cs else {}
        files = top.get('files') or []
        return {'repo': f'{OWNER}/{repo}', 'base': None, 'head': head, 'status': 'unknown-base', 'ahead_by': len(cs), 'behind_by': None,
                'note': 'نسخه‌ی روی لیارا به کامیتی از این مخزن قابل ردیابی نیست؛ آخرین کامیت‌های main و فایل‌های آخرین کامیت نمایش داده شده است.',
                'commits': [{'sha': c['sha'][:7], 'message': c['commit']['message'].split('\n')[0][:120], 'author': (c['commit'].get('author') or {}).get('name')} for c in reversed(cs)],
                'files': [{'file': f['filename'], 'status': f['status'], 'additions': f.get('additions'), 'deletions': f.get('deletions')} for f in files[:60]],
                'files_total': len(files), 'additions': sum(f.get('additions') or 0 for f in files), 'deletions': sum(f.get('deletions') or 0 for f in files),
                'compare_url': top.get('html_url')}
    head = head or 'main'
    cmp = await gh.get(_p(repo, f'/compare/{base}...{head}'))
    files = cmp.get('files') or []
    return {'repo': f'{OWNER}/{repo}', 'base': base, 'head': head, 'status': cmp.get('status'), 'ahead_by': cmp.get('ahead_by'), 'behind_by': cmp.get('behind_by'),
            'commits': [{'sha': c['sha'][:7], 'message': c['commit']['message'].split('\n')[0][:120], 'author': (c['commit'].get('author') or {}).get('name')} for c in (cmp.get('commits') or [])[-30:]],
            'files': [{'file': f['filename'], 'status': f['status'], 'additions': f.get('additions'), 'deletions': f.get('deletions')} for f in files[:60]],
            'files_total': len(files), 'additions': sum(f.get('additions') or 0 for f in files), 'deletions': sum(f.get('deletions') or 0 for f in files),
            'compare_url': cmp.get('html_url')}


async def deploy_incidents(app, hours=72):
    """Deploy <-> incident correlation: incidents opened within 2 h after each Liara release, with that release's commit."""
    from . import store
    from .liara import liara
    rels = (await liara.releases(app, 10)).get('releases', [])
    incs = [i for i in store.incidents(200, app) if i['opened_at'] > time.time() - hours * 3600]
    out = []
    for r in rels:
        t = _ts(r.get('finishedAt') or r.get('createdAt'))
        after = [i for i in incs if t <= i['opened_at'] <= t + 7200]
        gi = r.get('gitInfo') or {}
        out.append({'release': r.get('tag'), 'type': r.get('type'), 'state': r.get('state'), 'at': r.get('finishedAt') or r.get('createdAt'),
                    'commit': (gi.get('commit') or '')[:7] or None, 'message': (r.get('message') or '')[:100],
                    'incidents_within_2h': [{'id': i['id'], 'title': i['title'], 'kind': i['kind'], 'status': i['status']} for i in after]})
    return out


def parse_inputs(txt):
    """Input names (+ choice options) of `workflow_dispatch` in a workflow file, without a YAML dependency."""
    m = re.search(r'^\s*workflow_dispatch:\s*\n\s*inputs:\s*\n((?:[ \t]+.*\n?)*)', txt, re.M)
    if not m:
        return {}
    block, out, cur, ind = m.group(1), {}, None, None
    for ln in block.splitlines():
        if not ln.strip():
            continue
        lead = len(ln) - len(ln.lstrip())
        if ind is None:
            ind = lead
        if lead < ind:
            break
        if lead == ind and re.match(r'\s*[\w-]+:\s*$', ln):
            cur = ln.strip()[:-1]; out[cur] = {}
        elif cur:
            kv = re.match(r'\s*(type|default|options):\s*(.+)$', ln)
            if kv:
                v = kv.group(2).strip()
                out[cur][kv.group(1)] = [x.strip(' "\'') for x in v.strip('[]').split(',')] if kv.group(1) == 'options' else v.strip('"\'')
    return out


async def workflow_inputs(repo):
    """The deploy workflow's workflow_dispatch inputs, read from the workflow file (read-only)."""
    import base64
    try:
        d = await gh.get(_p(repo, f'/contents/.github/workflows/{DEPLOY_WF}'))
        return parse_inputs(base64.b64decode(d.get('content', '')).decode('utf-8', 'replace'))
    except Exception:
        return {}


async def reachable():
    try:
        await gh.get(_p('negahban'))
        return True
    except Exception:
        return False


async def dispatch_and_follow(repo, ref='main', inputs=None, on_line=None, timeout=2400):
    if repo not in REPOS:
        raise GitHubBlocked(f'repo {repo} is not one of the mapped repos')
    t0 = time.time()
    await gh.req('POST', _p(repo, f'/actions/workflows/{DEPLOY_WF}/dispatches'), json_body={'ref': ref, 'inputs': inputs or {}})
    run = None
    for _ in range(30):  # the dispatch API returns 204 without the run id: find the new run
        await _sleep(4)
        runs = (await gh.get(_p(repo, f'/actions/workflows/{DEPLOY_WF}/runs'), params={'per_page': 5, 'event': 'workflow_dispatch'})).get('workflow_runs', [])
        run = next((r for r in runs if _ts(r.get('created_at')) >= t0 - 5), None)
        if run:
            break
    if not run:
        return {'dispatched': True, 'run': None, 'note': 'workflow dispatched but the run did not appear within 2 minutes'}
    seen = set()
    while time.time() - t0 < timeout:
        r = await gh.get(_p(repo, f"/actions/runs/{run['id']}"))
        jobs = (await gh.get(_p(repo, f"/actions/runs/{run['id']}/jobs"))).get('jobs', [])
        for j in jobs:
            for s in j.get('steps') or []:
                key = (j['id'], s['number'], s.get('status'), s.get('conclusion'))
                if key not in seen and s.get('status') in ('in_progress', 'completed'):
                    seen.add(key)
                    if on_line:
                        await on_line(f"{j['name']} › {s['name']}: {s.get('conclusion') or s.get('status')}")
        if r.get('status') == 'completed':
            return {'dispatched': True, 'run': _run(r), 'conclusion': r.get('conclusion'),
                    'jobs': [{'name': j['name'], 'conclusion': j.get('conclusion'), 'steps': [{'name': s['name'], 'conclusion': s.get('conclusion')} for s in j.get('steps') or []]} for j in jobs]}
        if r.get('status') == 'waiting' and on_line and ('waiting', run['id']) not in seen:
            seen.add(('waiting', run['id']))
            await on_line('منتظر تأیید محیط production در GitHub است')
        await _sleep(10)
    return {'dispatched': True, 'run': _run(run), 'conclusion': 'timeout'}
