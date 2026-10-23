"""GitOps: code-level GitHub allowlist (reads + deploy-workflow dispatch only), read tools over a mocked GitHub API,
the «release via pipeline» approval card and the dispatch-and-follow flow."""
import asyncio, base64, json, time
from datetime import datetime, timezone
import httpx
import pytest
from negahban import approvals, gitops, store, tools
from negahban.config import settings
from negahban.redact import redact

O = '/repos/moazeniz'


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------ allowlist
ALLOWED = [('GET', f'{O}/negahban'), ('GET', f'{O}/bazargard/commits?per_page=5'), ('GET', f'{O}/negahban/commits/abc1234'),
           ('GET', f'{O}/negahban/compare/v1.0...main'), ('GET', f'{O}/negahban/tags'), ('GET', f'{O}/negahban/releases'),
           ('GET', f'{O}/negahban/releases/latest'), ('GET', f'{O}/wallex-ai-trader/actions/workflows/ci.yml/runs'),
           ('GET', f'{O}/negahban/actions/runs/123'), ('GET', f'{O}/negahban/actions/runs/123/jobs'), ('GET', f'{O}/negahban/actions/jobs/9/logs'),
           ('GET', f'{O}/negahban/commits/abc1234/check-runs'), ('GET', f'{O}/negahban/commits/abc1234/status'),
           ('GET', f'{O}/negahban/contents/.github/workflows/deploy.yml'),
           ('POST', f'{O}/negahban/actions/workflows/deploy.yml/dispatches'), ('POST', f'{O}/bazargard/actions/workflows/deploy.yml/dispatches')]
BLOCKED = [('PUT', f'{O}/negahban/contents/README.md'), ('POST', f'{O}/negahban/git/refs'), ('POST', f'{O}/negahban/git/commits'),
           ('POST', f'{O}/negahban/git/trees'), ('POST', f'{O}/negahban/pulls'), ('POST', f'{O}/negahban/issues'), ('POST', f'{O}/negahban/releases'),
           ('PATCH', f'{O}/negahban'), ('DELETE', f'{O}/negahban'), ('DELETE', f'{O}/negahban/git/refs/heads/main'), ('POST', f'{O}/negahban/merges'),
           ('POST', f'{O}/negahban/actions/workflows/ci.yml/dispatches'), ('POST', f'{O}/negahban/actions/runs/1/rerun'),
           ('POST', f'{O}/negahban/actions/workflows/deploy.yml/enable'), ('PUT', f'{O}/negahban/actions/workflows/deploy.yml/dispatches'),
           ('POST', '/repos/someone-else/negahban/actions/workflows/deploy.yml/dispatches'), ('GET', '/repos/someone-else/negahban'),
           ('GET', f'{O}/private-other-repo/commits'), ('GET', '/user'), ('GET', '/user/repos'), ('POST', '/user/repos'),
           ('GET', f'{O}/negahban/contents/negahban/main.py'), ('GET', f'{O}/negahban/actions/secrets'), ('GET', f'{O}/negahban/hooks'),
           ('POST', f'{O}/negahban/dispatches'), ('GET', f'{O}/negahban/../negahban/collaborators'), ('POST', '/graphql')]


@pytest.mark.parametrize('method,path', ALLOWED)
def test_allowlist_permits_reads_and_deploy_dispatch(method, path):
    assert gitops.allowed(method, path)


@pytest.mark.parametrize('method,path', BLOCKED)
def test_allowlist_blocks_everything_else(method, path):
    assert not gitops.allowed(method, path)


def test_blocked_call_never_reaches_the_network():
    hits = []
    gh = gitops.GitHub('t0k', httpx.MockTransport(lambda r: hits.append(r) or httpx.Response(200, json={})))
    for m, p in BLOCKED:
        with pytest.raises(gitops.GitHubBlocked):
            run(gh.req(m, p, json_body={'message': 'x', 'content': 'eA=='}))
    assert hits == []


def test_no_write_helpers_exist():
    # the module exposes no commit/push/PR/file-write helpers; the only write is the deploy dispatch
    src = open(gitops.__file__, encoding='utf-8').read()
    for bad in ('/git/refs', '/pulls', "'PUT'", "'PATCH'", "'DELETE'", '/contents/README'):
        assert bad not in src.replace("method == 'GET'", '')
    assert src.count("req('POST'") == 1


def test_token_is_redacted(monkeypatch):
    monkeypatch.setenv('NEGAHBAN_GITHUB_TOKEN', 'github_pat_SECRETVALUE123456789')
    assert 'SECRETVALUE' not in redact('auth failed for github_pat_SECRETVALUE123456789')
    assert 'github_pat_SECRETVALUE123456789' in settings.secrets()


# ------------------------------------------------------------------ mocked GitHub
NOW = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _commit(sha, msg):
    return {'sha': sha, 'commit': {'message': msg, 'author': {'name': 'dev', 'date': NOW}}}


class FakeGH:
    def __init__(self):
        self.calls, self.dispatched, self.run_status = [], [], 'completed'

    def __call__(self, req):
        p, m = req.url.path, req.method
        self.calls.append((m, p, req.url.params.get('until')))
        assert req.headers['Authorization'] == 'Bearer test-gh-token'
        if m == 'POST':
            self.dispatched.append(json.loads(req.content))
            return httpx.Response(204)
        if p.endswith('/contents/.github/workflows/deploy.yml'):
            y = 'on:\n  workflow_dispatch:\n    inputs:\n      app:\n        type: choice\n        options: [negahban-sre, demo-shop]\n      dry_run:\n        type: boolean\n        default: true\n      auto_rollback:\n        type: boolean\npermissions:\n  contents: read\n'
            return httpx.Response(200, json={'content': base64.b64encode(y.encode()).decode()})
        if p.endswith('/commits') and req.url.params.get('until'):
            return httpx.Response(200, json=[_commit('a' * 40, 'live one')])
        if p.endswith('/commits'):
            return httpx.Response(200, json=[_commit('c' * 40, 'feat: newest'), _commit('b' * 40, 'fix: thing'), _commit('a' * 40, 'live one')])
        if '/commits/' in p and '/check-runs' not in p and '/status' not in p:
            sha = p.rsplit('/', 1)[1]
            if sha.startswith('dead'):
                return httpx.Response(422, json={'message': 'No commit found'})
            return httpx.Response(200, json={**_commit(sha, 'x'), 'html_url': 'https://github.com/c', 'files': [{'filename': 'README.md', 'status': 'added', 'additions': 3, 'deletions': 0}]})
        if '/check-runs' in p:
            return httpx.Response(200, json={'check_runs': [{'name': 'test', 'status': 'completed', 'conclusion': 'success'}, {'name': 'lint', 'status': 'completed', 'conclusion': 'failure'}]})
        if '/compare/' in p:
            return httpx.Response(200, json={'status': 'ahead', 'ahead_by': 2, 'behind_by': 0, 'html_url': 'https://github.com/x/compare',
                                             'commits': [_commit('b' * 40, 'fix: thing'), _commit('c' * 40, 'feat: newest')],
                                             'files': [{'filename': 'app.py', 'status': 'modified', 'additions': 5, 'deletions': 1}]})
        if p.endswith('/tags'):
            return httpx.Response(200, json=[{'name': 'v2'}, {'name': 'v1'}])
        if p.endswith('/releases'):
            return httpx.Response(200, json=[])
        if p.endswith('/runs') and 'workflows' in p:
            return httpx.Response(200, json={'workflow_runs': [{'id': 77, 'name': 'Deploy to Liara', 'status': self.run_status, 'conclusion': 'success', 'event': 'workflow_dispatch',
                                                                'head_sha': 'c' * 40, 'head_branch': 'main', 'created_at': _now(), 'html_url': 'https://github.com/run/77'}]})
        if p.endswith('/actions/runs/77'):
            return httpx.Response(200, json={'id': 77, 'status': 'completed', 'conclusion': 'success', 'head_sha': 'c' * 40, 'created_at': NOW, 'html_url': 'https://github.com/run/77'})
        if p.endswith('/actions/runs/77/jobs'):
            return httpx.Response(200, json={'jobs': [{'id': 5, 'name': 'deploy', 'conclusion': 'success', 'steps': [
                {'number': 1, 'name': 'Deploy + smoke test + automatic rollback', 'status': 'completed', 'conclusion': 'success'}]}]})
        if p == f'{O}/negahban' or p == f'{O}/bazargard':
            return httpx.Response(200, json={'html_url': 'https://github.com/moazeniz/x', 'default_branch': 'main'})
        return httpx.Response(404, json={'message': 'not found'})


async def _fp(app):
    return 'fp'


@pytest.fixture
def gh(monkeypatch, fresh_db):
    monkeypatch.setattr(approvals, 'state_fingerprint', _fp)  # no Liara calls from tests
    fake = FakeGH()
    monkeypatch.setattr(gitops, 'gh', gitops.GitHub('test-gh-token', httpx.MockTransport(fake)))
    gitops._cache.clear()

    async def releases(app, count=10):
        return {'currentRelease': 'r2', 'releases': [{'_id': 'r2', 'tag': 'v20', 'type': 'DEPLOY', 'state': 'READY', 'message': 'v1.9 guardrails',
                                                      'finishedAt': NOW, 'createdAt': NOW, 'gitInfo': None}]}
    from negahban.liara import liara
    monkeypatch.setattr(liara, 'releases', releases)

    async def nosleep(*a, **k):
        return None
    monkeypatch.setattr(gitops, '_sleep', nosleep)
    return fake


def test_overview_ci_status_and_commits_behind(gh):
    o = run(gitops.overview('negahban-sre'))
    assert o['repo'] == 'moazeniz/negahban' and o['commits'][0]['sha'] == 'ccccccc'
    assert o['head_checks'] == {'total': 2, 'success': 1, 'failed': ['lint'], 'pending': 0}
    assert o['live']['commit'] == 'a' * 40 and o['live']['estimated'] is True  # no gitInfo -> estimated from release time
    assert o['live_vs_main']['behind_main'] == 2 and o['deploy_runs'][0]['conclusion'] == 'success'
    assert all(m == 'GET' for m, _, _ in gh.calls)
    n = len(gh.calls)
    run(gitops.overview('negahban-sre'))  # cached for the dashboard poll
    assert len(gh.calls) == n


def test_live_commit_prefers_git_info(gh, monkeypatch):
    async def releases(app, count=10):
        return {'currentRelease': 'r3', 'releases': [{'_id': 'r3', 'tag': 'v21', 'message': 'v2', 'finishedAt': NOW, 'gitInfo': {'commit': 'd' * 40, 'branch': 'main'}}]}
    from negahban.liara import liara
    monkeypatch.setattr(liara, 'releases', releases)
    lc = run(gitops.live_commit('bazargard-ai'))
    assert lc['commit'] == 'd' * 40 and lc['source'] == 'gitInfo' and not lc['estimated']


def test_what_changed_tool(gh):
    r = run(tools.run('git_what_changed', {'app': 'negahban-sre'}, {}))
    assert r['ahead_by'] == 2 and r['files'][0]['file'] == 'app.py' and r['commits'][-1]['message'] == 'feat: newest'


def test_cicd_status_tool_unknown_app(gh):
    r = run(tools.run('cicd_status', {'app': 'nope'}, {}))
    assert 'no GitHub repo' in r['error']


def test_deploy_incident_correlation(gh):
    r = run(tools.run('deploy_incident_correlation', {'app': 'negahban-sre'}, {}))
    assert r[0]['release'] == 'v20' and r[0]['incidents_within_2h'] == []


def test_parse_inputs():
    y = 'on:\n  push:\n    tags: ["v*"]\n  workflow_dispatch:\n    inputs:\n      app:\n        type: choice\n        options: [bazargard-ai]\n        default: bazargard-ai\n      dry_run:\n        type: boolean\n        default: true\n\njobs:\n  x: {}\n'
    assert gitops.parse_inputs(y) == {'app': {'type': 'choice', 'options': ['bazargard-ai'], 'default': 'bazargard-ai'}, 'dry_run': {'type': 'boolean', 'default': 'true'}}


# ------------------------------------------------------------------ release via pipeline (approval card)
def test_release_creates_card_only(gh):
    r = run(tools.run('release_via_pipeline', {'app': 'negahban-sre', 'ref': 'main'}, {'origin': 'chat'}))
    p = store.get_proposal(r['proposal']['id'])
    assert p['status'] == 'pending' and gh.dispatched == []  # nothing dispatched before approval
    pipe = p['impact']['pipeline']
    assert pipe['repo'] == 'moazeniz/negahban' and pipe['changes']['n_commits'] == 2 and pipe['github_reachable']


def test_release_refused_for_wallex(gh):
    store.set_policy('meowmeow-wallex', {'write_lock': True})
    r = run(tools.run('release_via_pipeline', {'app': 'meowmeow-wallex'}, {}))
    assert r.get('refused') and not store.proposals('pending') and gh.dispatched == []
    assert approvals.guard('release_via_pipeline', {'app': 'meowmeow-wallex'})  # refused even without a policy row


def test_self_release_allowed_through_pipeline_only(gh):
    store.set_policy('negahban-sre', {'write_lock': True})
    assert approvals.guard('restart_app', {'app': 'negahban-sre'})  # direct changes stay locked
    assert approvals.guard('release_via_pipeline', {'app': 'negahban-sre'}) is None


def test_ref_validation(gh):
    with pytest.raises(tools.ToolError):
        tools.validate('release_via_pipeline', {'app': 'negahban-sre', 'ref': 'main; rm -rf /'})
    with pytest.raises(tools.ToolError):
        tools.validate('release_via_pipeline', {'app': 'some-other-app'})


def test_approved_release_dispatches_and_follows(gh, monkeypatch):
    store.upsert_app('negahban-sre', 'monitored')

    async def go():
        pid = (await tools.run('release_via_pipeline', {'app': 'demo-shop', 'dry_run': True}, {}))['proposal']['id']
        await approvals.execute(pid, 'owner')
        return store.get_proposal(pid)
    p = run(go())
    assert p['status'] == 'done', p
    assert gh.dispatched == [{'ref': 'main', 'inputs': {'app': 'demo-shop', 'dry_run': 'true', 'auto_rollback': 'true'}}]
    assert p['result']['conclusion'] == 'success' and p['result']['jobs'][0]['name'] == 'deploy'
    posts = [c for c in gh.calls if c[0] == 'POST']
    assert posts == [('POST', f'{O}/negahban/actions/workflows/deploy.yml/dispatches', None)]


def test_unreachable_github_reports_fallback(monkeypatch, fresh_db):
    monkeypatch.setattr(approvals, 'state_fingerprint', _fp)

    def boom(req):
        raise httpx.ConnectError('down')
    monkeypatch.setattr(gitops, 'gh', gitops.GitHub('test-gh-token', httpx.MockTransport(boom)))
    r = run(tools.run('release_via_pipeline', {'app': 'bazargard-ai'}, {}))
    imp = store.get_proposal(r['proposal']['id'])['impact']
    assert imp['pipeline']['github_reachable'] is False and 'استقرار مستقیم' in imp['pipeline']['fallback']


def test_github_token_shapes_redacted_even_if_unknown():
    assert 'ABCDEFGHIJKLMNOPQRSTUV' not in redact('x ghp_ABCDEFGHIJKLMNOPQRSTUVwxyz1234 y github_pat_11ABCDEFGHIJKLMNOPQRSTUV_xyz')


def test_what_changed_when_live_commit_not_in_repo(gh, monkeypatch):
    async def releases(app, count=10):
        return {'currentRelease': 'r9', 'releases': [{'_id': 'r9', 'tag': 'v13', 'message': 'meowmeow-wallex deadbee: old private history', 'finishedAt': NOW}]}
    from negahban.liara import liara
    monkeypatch.setattr(liara, 'releases', releases)
    r = run(gitops.what_changed('meowmeow-wallex'))
    assert r['base'] is None and r['note'] and r['files'][0]['file'] == 'README.md' and r['commits'][-1]['sha'] == 'ccccccc'
    o = run(gitops.overview('meowmeow-wallex', fresh=True))
    assert 'unknown' in o['live_vs_main'] or o['live_vs_main'].get('behind_main') is not None
