"""GitOps tools: read the apps' GitHub repos and CI/CD (automatic), and release through the repo's own "Deploy to Liara"
pipeline (sensitive approval card). Every GitHub call goes through gitops.GitHub, whose allowlist permits only reads plus
the deploy workflow dispatch; Negahban never commits, pushes or opens PRs in these repos."""
from . import bus, gitops
from .tools import ToolError, tool

PIPE_APPS = {**gitops.APP_REPO, 'demo-shop': 'negahban'}  # demo-shop is a choice of the negahban repo's deploy workflow
REF = {'type': 'string', 'pattern': r'^[A-Za-z0-9._/-]{1,100}$', 'description': 'branch or tag, default main'}


def _wrap(e):
    if isinstance(e, gitops.GitHubBlocked):
        return ToolError(str(e))
    return ToolError(f'GitHub: {e}')


@tool('cicd_status', 'read', 'GitOps status of an app (or all mapped apps): its GitHub repo, latest commits/tags/releases, CI status of the newest commit, '
      'last CI and "Deploy to Liara" workflow runs, which commit is live on Liara and how many commits it is behind main. '
      'Mapped apps: bazargard-ai->moazeniz/bazargard, negahban-sre->moazeniz/negahban, meowmeow-wallex->moazeniz/wallex-ai-trader.',
      {'app': {'type': 'string', 'description': 'Liara app; omit for all mapped apps'}}, [], label_fa='در حال خواندن وضعیت CI/CD از گیت‌هاب…')
async def cicd_status(a, ctx):
    apps = [a['app']] if a.get('app') else list(gitops.APP_REPO)
    out = {}
    for app in apps:
        try:
            out[app] = await gitops.overview(app)
        except Exception as e:
            out[app] = {'error': str(_wrap(e))}
    return out if len(apps) > 1 else out[apps[0]]


@tool('git_what_changed', 'read', 'What changed between two versions of an app\'s repo: commit list + changed files (+/- lines). '
      'Default base = the commit live on Liara, default head = main. Use it for "what is in the next release" or "what changed in the last deploy" '
      '(pass base=<previous tag/sha>, head=<live sha/tag>).',
      {'app': {'type': 'string'}, 'base': REF, 'head': REF}, ['app'], label_fa='در حال مقایسه‌ی نسخه‌ها در گیت‌هاب…')
async def git_what_changed(a, ctx):
    try:
        return await gitops.what_changed(a['app'], a.get('base'), a.get('head'))
    except Exception as e:
        raise _wrap(e)


@tool('deploy_incident_correlation', 'read', 'Correlate the app\'s recent Liara releases (with their git commit when known) with incidents opened within 2 hours '
      'after each release — answers "did a deploy cause this?".', {'app': {'type': 'string'}, 'hours': {'type': 'integer', 'minimum': 1, 'maximum': 336}}, ['app'],
      label_fa='در حال تطبیق استقرارها با رخدادها…')
async def deploy_incident_correlation(a, ctx):
    return await gitops.deploy_incidents(a['app'], a.get('hours') or 72)


@tool('ci_run_details', 'read', 'Jobs and steps of one GitHub Actions run of the app\'s repo (default: latest CI run); for a failed job also the last log lines.',
      {'app': {'type': 'string'}, 'run_id': {'type': 'integer', 'minimum': 1}, 'workflow': {'type': 'string', 'enum': ['ci', 'deploy']}}, ['app'],
      label_fa='در حال خواندن جزئیات اجرای CI…')
async def ci_run_details(a, ctx):
    from .sanitize import clean
    repo = gitops.repo_of(a['app'])
    try:
        rid = a.get('run_id')
        if not rid:
            wf = gitops.DEPLOY_WF if a.get('workflow') == 'deploy' else gitops.CI_WF
            runs = (await gitops.gh.get(gitops._p(repo, f'/actions/workflows/{wf}/runs'), params={'per_page': 1})).get('workflow_runs', [])
            if not runs:
                return {'note': 'no runs yet'}
            rid = runs[0]['id']
        run = await gitops.gh.get(gitops._p(repo, f'/actions/runs/{rid}'))
        jobs = (await gitops.gh.get(gitops._p(repo, f'/actions/runs/{rid}/jobs'))).get('jobs', [])
        out = {'run': gitops._run(run), 'jobs': []}
        for j in jobs:
            jj = {'name': j['name'], 'status': j.get('status'), 'conclusion': j.get('conclusion'),
                  'steps': [{'name': s['name'], 'conclusion': s.get('conclusion') or s.get('status')} for s in j.get('steps') or []]}
            if j.get('conclusion') == 'failure':
                try:
                    txt = await gitops.gh.req('GET', gitops._p(repo, f"/actions/jobs/{j['id']}/logs"), raw=True)
                    jj['log_tail'] = [clean(x, 220) for x in txt.splitlines()[-40:]]
                except Exception as e:
                    jj['log_tail'] = [f'(log unavailable: {str(e)[:80]})']
            out['jobs'].append(jj)
        return out
    except Exception as e:
        raise _wrap(e)


async def _release_impact(a):
    app, ref = a['app'], a.get('ref') or 'main'
    repo = PIPE_APPS[app]
    dry = bool(a.get('dry_run'))
    pipe = {'repo': f'{gitops.OWNER}/{repo}', 'workflow': f'Deploy to Liara ({gitops.DEPLOY_WF})', 'ref': ref,
            'inputs': {'app': app, 'dry_run': dry, 'auto_rollback': a.get('auto_rollback', True) is not False}}
    reach = await gitops.reachable()
    pipe['github_reachable'] = reach
    if not reach:
        pipe['fallback'] = ('گیت‌هاب از سرور نگهبان در دسترس نیست؛ این کارت اجرا نمی‌شود. جایگزین: استقرار مستقیم با بارگذاری کد '
                            '(«استقرار پروژه» یا «استقرار نسخه‌ی جدید» از فضای کار) — بدون CI و بدون تست دود پایپ‌لاین.')
    else:
        inputs = await gitops.workflow_inputs(repo)
        if inputs:
            opts = (inputs.get('app') or {}).get('options') or []
            if opts and app not in opts:
                pipe['blocked'] = f'workflow این مخزن برنامه‌ی {app} را نمی‌شناسد (گزینه‌ها: {", ".join(opts)})'
            pipe['inputs'] = {k: v for k, v in pipe['inputs'].items() if k in inputs}
        if app in gitops.APP_REPO:
            try:
                ch = await gitops.what_changed(app, None, ref)
                pipe['changes'] = {'commits': ch['commits'][-8:], 'n_commits': ch['ahead_by'], 'note': ch.get('note'), 'files_total': ch['files_total'],
                                   'additions': ch['additions'], 'deletions': ch['deletions'], 'base': (ch['base'] or '')[:12] or None, 'compare_url': ch['compare_url']}
            except Exception as e:
                pipe['changes'] = {'error': str(e)[:160]}
    imp = {'downtime': 'ندارد اگر استقرار بدون قطعی فعال باشد؛ وگرنه چند ثانیه هنگام جابه‌جایی کانتینر' if not dry else 'ندارد (اجرای آزمایشی؛ چیزی مستقر نمی‌شود)',
           'cost': 'بدون تغییر پلن؛ یک واحد از سهمیه‌ی روزانه‌ی استقرار لیارا' if not dry else 'بدون هزینه',
           'risk': 'کم — پایپ‌لاین CI را اجرا می‌کند، بعد از استقرار تست دود می‌زند و در صورت شکست خودکار به نسخه‌ی قبلی برمی‌گردد'
           + ('' if pipe['inputs'].get('auto_rollback', True) else ' (بازگشت خودکار خاموش است!)'),
           'deploys': 'اجرای آزمایشی' if dry else '۱ استقرار', 'pipeline': pipe,
           'command': f"POST /repos/{gitops.OWNER}/{repo}/actions/workflows/{gitops.DEPLOY_WF}/dispatches ref={ref} inputs={pipe['inputs']}",
           'gh_env_note': 'job استقرار در محیط production گیت‌هاب است؛ اگر آن محیط بازبین داشته باشد، اجرا تا تأیید در GitHub منتظر می‌ماند.'}
    if pipe.get('blocked'):
        imp['blocked'] = pipe['blocked']
    if pipe.get('fallback'):
        imp['blocked'] = pipe['fallback']
    return imp, 'پایپ‌لاین در صورت شکست تست دود خودش به ریلیز قبلی برمی‌گردد؛ در غیر این صورت «بازگشت به نسخه‌ی قبلی» در نگهبان (rollback لیارا) در دسترس است.'


@tool('release_via_pipeline', 'sensitive', 'Release a new version of an app THROUGH ITS GITHUB PIPELINE: dispatch the repo\'s "Deploy to Liara" workflow '
      '(CI -> deploy -> smoke test -> automatic rollback) for a branch/tag, then follow the run live and report jobs/steps. Preferred over direct upload '
      'for apps with a repo (bazargard-ai, negahban-sre, demo-shop via the negahban repo). meowmeow-wallex is monitor-only and always refused. '
      'If GitHub is unreachable the card says so and the fallback is a direct upload deploy.',
      {'app': {'type': 'string', 'enum': sorted(PIPE_APPS)}, 'ref': REF, 'dry_run': {'type': 'boolean', 'description': 'package + preflight only, deploy nothing'},
       'auto_rollback': {'type': 'boolean'}, 'reason': {'type': 'string'}}, ['app'],
      title=lambda a: f"انتشار {a['app']} از گیت‌هاب ({a.get('ref') or 'main'})" + (' — آزمایشی' if a.get('dry_run') else ''),
      impact=_release_impact, label_fa='ساخت کارت انتشار از پایپ‌لاین…')
async def release_via_pipeline(a, ctx):
    app, ref = a['app'], a.get('ref') or 'main'
    repo = PIPE_APPS[app]
    if app in gitops.NEVER_DISPATCH:  # defense in depth: approvals.guard already refuses monitor-only apps
        raise ToolError(f'{app} is monitor-only; Negahban never releases it')
    if not await gitops.reachable():
        raise ToolError('GitHub در دسترس نیست؛ پایپ‌لاین اجرا نشد. جایگزین: استقرار مستقیم با بارگذاری کد (کارت «استقرار پروژه»/«استقرار نسخه‌ی جدید»).')
    want = {'app': app, 'dry_run': bool(a.get('dry_run')), 'auto_rollback': a.get('auto_rollback', True) is not False}
    known = await gitops.workflow_inputs(repo)
    inputs = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in want.items() if not known or k in known}
    pid = ctx.get('proposal_id')
    lines = []

    async def on_line(text):
        lines.append(text)
        bus.publish('op', proposal_id=pid, app=app, status='running', text='⚙️ پایپ‌لاین: ' + text[:180], build_lines=lines[-6:])

    try:
        res = await gitops.dispatch_and_follow(repo, ref, inputs, on_line)
    except Exception as e:
        raise _wrap(e)
    gitops._cache.pop(app, None)
    if res.get('conclusion') not in ('success', None):
        failed = [f"{j['name']} › {s['name']}" for j in res.get('jobs', []) for s in j['steps'] if s.get('conclusion') == 'failure']
        url = (res.get('run') or {}).get('url') or ''
        raise ToolError(f"اجرای پایپ‌لاین {res.get('conclusion')} شد" + (f"؛ گام ناموفق: {', '.join(failed[:3])}" if failed else '') + (f' — {url}' if url else '')
                        + ('. بازگشت خودکار پایپ‌لاین فعال بود.' if inputs.get('auto_rollback') == 'true' else ''))
    return {**res, 'repo': f'{gitops.OWNER}/{repo}', 'ref': ref, 'inputs': inputs, 'run_url': (res.get('run') or {}).get('url'),
            'next_steps': ['نتیجه‌ی تست دود در خلاصه‌ی اجرای GitHub', 'وضعیت برنامه در داشبورد نگهبان (کارت CI/CD)']}
