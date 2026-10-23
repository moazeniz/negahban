import asyncio, io, json, tarfile
import pytest
from negahban import approvals, store, tools
from negahban import templates as T
from negahban.liara import LiaraError, liara

run = asyncio.run
SEEDS = {'fastapi', 'node-express', 'nextjs-static', 'django-postgres', 'laravel', 'go', 'static-site', 'worker-cron', 'docker-generic'}


def _custom(name, **kw):
    return T.clone('fastapi', name, overrides=kw)


@pytest.fixture
def offline(monkeypatch, fresh_db):
    """No network: Liara reads fail fast, fingerprint/impact helpers are deterministic (like test_approvals_policy)."""
    async def no_plans():
        raise LiaraError(503, 'offline')

    async def fp(app):
        return 'fp1'

    async def impact(tool, a):
        return {'downtime': 'x', 'cost': 'y', 'risk': 'z'}, 'rb'

    async def app_404(name):
        raise LiaraError(404, 'not found')
    monkeypatch.setattr(liara, 'plans', no_plans)
    monkeypatch.setattr(liara, 'app', app_404)
    monkeypatch.setattr(approvals, 'state_fingerprint', fp)
    monkeypatch.setattr(approvals, 'impact', impact)


def test_seeds_load_and_are_valid():
    assert SEEDS <= set(T.BUILTINS)
    for tid in SEEDS:
        t = T.get(tid)
        assert t['builtin'] and 'Dockerfile' in t['files'] and t['plan'] and t['autoscale']['max_toman_day'] > 0
        files = T.render(t, app='demo-app')
        lj = json.loads(files['liara.json'])
        assert lj['platform'] == 'docker' and lj['app'] == 'demo-app' and lj['port'] == t['port']
        assert not any('{{app_name}}' in c or '{{port}}' in c or '{{health_path}}' in c for c in files.values()), tid
        assert all(not e['default'] for e in t['env'] if e['secret'])
    assert T.get('worker-cron')['disk']['mountTo'] == '/data' and json.loads(T.render(T.get('worker-cron'))['liara.json'])['cron']
    assert any(e['name'] == 'DATABASE_URL' and e['secret'] and e['required'] for e in T.get('django-postgres')['env'])


def test_render_substitutes_params_and_keeps_other_braces():
    t = T.get('fastapi')
    f = T.render(t, {'port': '9000'}, app='my-api')
    assert 'EXPOSE 9000' in f['Dockerfile'] and '--port 9000' in f['Dockerfile'] and "'my-api'" in f['main.py']
    assert json.loads(f['liara.json'])['port'] == 9000 and '9000/health' in f['healthcheck.py']
    assert json.loads(f['liara.json'])['healthCheck']['command'] == ['CMD-SHELL', 'python healthcheck.py']  # Liara needs the array form
    jsx = T.render(T.get('nextjs-static'), app='site-x')['pages/index.js']
    assert '<h1>site-x</h1>' in jsx and "style={{ fontFamily" in jsx  # JSX double braces untouched
    assert T.sub('{{ unknown }} {{port}}', {'port': '1'}) == '{{ unknown }} 1'
    dj = T.render(T.get('django-postgres'), {'wsgi_module': 'shop.wsgi'}, app='shop-x')['Dockerfile']
    assert 'gunicorn shop.wsgi:application --bind 0.0.0.0:8000' in dj


def test_render_rejects_bad_params():
    t = T.get('fastapi')
    for bad in ({'port': '70000'}, {'port': 'abc'}, {'nope': '1'}, {'port': '80\nRUN rm -rf /'}):
        with pytest.raises(T.TemplateError):
            T.render(t, bad, app='ok-app')
    with pytest.raises(T.TemplateError):
        T.render(t, app='Bad_Name!')


def test_validation_rejects_bad_input():
    base = {k: T.get('fastapi')[k] for k in T.EDITABLE}
    cases = [{'port': 0}, {'port': 'x'}, {'port': 70000}, {'name': ''}, {'plan': 'galaxy'}, {'bundle': 'gold'},
             {'files': {'main.py': 'x'}}, {'files': {'../etc/passwd': 'x', 'Dockerfile': 'FROM x'}}, {'files': {'/abs': 'x', 'Dockerfile': 'FROM x'}},
             {'files': {'liara.json': '{}', 'Dockerfile': 'FROM x'}}, {'files': {'.env': 'A=1', 'Dockerfile': 'FROM x'}},
             {'env': [{'name': '1BAD'}]}, {'env': [{'name': 'TOKEN', 'secret': True, 'default': 'leak'}]},
             {'env': [{'name': 'A'}, {'name': 'A'}]}, {'autoscale': {'min_plan': 'medium-g2', 'max_plan': 'small-g2', 'max_toman_day': 5000}},
             {'disk': {'name': 'Data!', 'mountTo': 'data', 'size_gb': 0}}, {'cron': ['every minute']}, {'health_check': {'command': 'x', 'interval': 0}},
             {'params': [{'name': 'port', 'default': '1'}]}, {'unexpected_key': 1}]
    for c in cases:
        with pytest.raises(T.TemplateError) as e:
            T.validate({**base, **c})
        assert e.value.errors, c
    with pytest.raises(T.TemplateError):
        T.validate('not an object')
    assert T.validate(base)['port'] == 8000


def test_versions_and_restore(fresh_db):
    t = _custom('ورژن‌دار')
    assert t['version'] == 1 and not t['builtin'] and t['origin'] == 'clone:fastapi'
    t2 = T.update(t['id'], {'port': 8100, 'plan': 'medium-g2', 'autoscale': {'min_plan': 'small-g2', 'max_plan': 'medium-g2', 'max_toman_day': 70000}})
    assert t2['version'] == 2 and t2['port'] == 8100
    with pytest.raises(T.TemplateError):
        T.update(t['id'], {'port': -1})
    assert T.get(t['id'])['version'] == 2  # invalid edit stored nothing
    vs = T.versions(t['id'])
    assert [v['version'] for v in vs] == [2, 1] and vs[0]['current']
    t3 = T.restore(t['id'], 1)
    assert t3['version'] == 3 and t3['port'] == 8000 and t3['plan'] == 'small-g2'
    assert len(T.versions(t['id'])) == 3
    with pytest.raises(T.TemplateError):
        T.restore(t['id'], 99)
    acts = {a['action'] for a in store.audit_list(50)}
    assert {'template_create', 'template_update', 'template_restore', 'template_clone'} <= acts


def test_delete_needs_confirm_and_builtins_immutable(fresh_db):
    t = _custom('قالب حذفی')
    with pytest.raises(T.TemplateError):
        T.delete(t['id'], 'wrong')
    assert T.get(t['id'])
    T.delete(t['id'], 'قالب حذفی')
    assert T.get(t['id']) is None
    for fn in (lambda: T.update('fastapi', {'port': 1234}), lambda: T.delete('fastapi', T.get('fastapi')['name']), lambda: T.restore('fastapi', 1)):
        with pytest.raises(T.TemplateError):
            fn()
    assert T.get('fastapi')['port'] == 8000
    c = T.clone('go')
    assert c['id'] != 'go' and c['files'] == T.get('go')['files']


def test_list_contains_builtins_and_custom(fresh_db):
    t = _custom('لیستی')
    ids = {x['id'] for x in T.list_templates()}
    assert SEEDS <= ids and t['id'] in ids
    s = next(x for x in T.list_templates() if x['id'] == 'django-postgres')
    assert s['price']['hour'] > 0 and s['price']['month'] == round(s['price']['hour'] * 720) and 'DATABASE_URL' in s['secrets']


def test_chat_tools_list_show_save(offline):
    lst = run(tools.run('list_templates', {}, {}))
    assert {x['id'] for x in lst} >= SEEDS
    sh = run(tools.run('show_template', {'template_id': 'fastapi', 'app': 'my-api'}, {}))
    paths = [f['path'] for f in sh['files']]
    assert 'Dockerfile' in paths and 'liara.json' in paths and sh['env_schema']
    assert 'error' in run(tools.run('show_template', {'template_id': 'nope'}, {}))
    sv = run(tools.run('save_template', {'from_id': 'fastapi', 'name': 'my fastapi', 'plan': 'medium-g2'}, {'actor': 'agent'}))
    t = T.get(sv['saved'])
    assert t['plan'] == 'medium-g2' and plans_within(t)


def plans_within(t):
    from negahban import plans
    return plans.within(t['plan'], t['autoscale']['min_plan'], t['autoscale']['max_plan'])


def test_deploy_template_creates_pending_proposal(offline):
    r = run(tools.run('deploy_template', {'template_id': 'django-postgres', 'app': 'shop-new', 'plan': 'medium-g2', 'envs': {'DJANGO_DEBUG': '0'}}, {'origin': 'chat'}))
    p = store.get_proposal(r['proposal']['id'])
    assert p['status'] == 'pending' and p['tool'] == 'deploy_template' and p['app'] == 'shop-new'
    im = p['impact']
    assert 'تومان' in im['cost'] and im['monthly'] > 0 and 'Dockerfile' in im['diff'] and 'DATABASE_URL' in im['secrets_needed']
    assert any(e['name'] == 'DATABASE_URL' and e['secret'] for e in im['env_schema'])
    # secrets are refused in tool args (they would land in the proposal row)
    with pytest.raises(tools.ToolError):
        run(tools.run('deploy_template', {'template_id': 'django-postgres', 'app': 'shop-new', 'envs': {'DATABASE_URL': 'postgres://u:p@h/db'}}, {}))
    with pytest.raises(tools.ToolError):
        run(tools.run('deploy_template', {'template_id': 'nope', 'app': 'shop-new'}, {}))
    assert 'deploy_template' in approvals.VERIFY


def test_executor_creates_app_deploys_and_registers(offline, monkeypatch):
    calls = []

    async def create_app(name, plan, bundle='basic', platform='docker', network=None):
        calls.append(('create_app', name, plan, bundle, platform))
        return {'ok': True}

    async def create_disk(name, disk, size_gb):
        calls.append(('create_disk', name, disk, size_gb))

    async def deploy_tar(name, tb, port, envs=None, disks=None, message='', health_check=None):
        names = tarfile.open(fileobj=io.BytesIO(tb)).getnames()
        calls.append(('deploy_tar', name, port, dict(envs or {}), disks, health_check, sorted(names)))
        return 'rel_1'

    async def wait_release(name, rid, timeout=600, on_state=None):
        await on_state('BUILDING')
        return 'READY', {'_id': rid}
    for n, f in (('create_app', create_app), ('create_disk', create_disk), ('deploy_tar', deploy_tar), ('wait_release', wait_release)):
        monkeypatch.setattr(liara, n, f)
    pid = store.uid('prp_')
    store.kv_set(f'tplsecret:{pid}', {'API_TOKEN': 'typed-on-card-123'})
    fn = tools.TOOLS['deploy_template']['fn']
    res = run(fn({'template_id': 'worker-cron', 'app': 'cron-box', 'envs': {'JOB_INTERVAL_SECONDS': '60'}}, {'proposal_id': pid, 'actor': 'owner'}))
    assert [c[0] for c in calls] == ['create_app', 'create_disk', 'deploy_tar']
    assert calls[0][1:] == ('cron-box', 'small-g2', 'basic', 'docker')
    _, app, port, envs, disks, hc, names = calls[2]
    assert app == 'cron-box' and port == 8080 and disks == [{'name': 'data', 'mountTo': '/data'}] and hc['command'] == ['CMD-SHELL', 'python healthcheck.py']
    assert envs['JOB_INTERVAL_SECONDS'] == '60' and envs['INTERNAL_SCHEDULER'] == '1' and envs['API_TOKEN'] == 'typed-on-card-123'
    assert {'Dockerfile', 'liara.json', 'worker.py', 'job.py'} <= set(names)
    assert res['release_id'] == 'rel_1' and res['release_state'] == 'READY' and res['url'] == 'https://cron-box.liara.run' and res['created_app']
    reg = store.get_app('cron-box')
    assert reg and reg['port'] == 8080 and reg['health_path'] == '/health'
    pol = store.policy('cron-box')
    assert pol['min_plan'] == 'small-g2' and pol['max_plan'] == 'medium-g2' and pol['max_toman_day'] == 50000
    assert store.kv_get(f'tplsecret:{pid}') is None  # typed secret consumed
    assert 'typed-on-card-123' not in json.dumps(store.audit_list(50), ensure_ascii=False)
    assert store.archives('cron-box', 5)[0]['release_id'] == 'rel_1'


def test_executor_existing_app_keeps_envs_and_skips_create(offline, monkeypatch):
    calls = []

    async def app_ok(name):
        return {'name': name, 'envs': [{'key': 'OLD', 'value': 'keep'}]}

    async def envs(name):
        return {'OLD': 'keep', 'SECRET_KEY': 'existing-secret'}

    async def create_app(*a, **k):
        calls.append('create_app')

    async def deploy_tar(name, tb, port, envs=None, disks=None, message='', health_check=None):
        calls.append(('deploy', envs))
        return 'rel_2'

    async def wait_release(name, rid, timeout=600, on_state=None):
        return 'READY', {}
    for n, f in (('app', app_ok), ('envs', envs), ('create_app', create_app), ('deploy_tar', deploy_tar), ('wait_release', wait_release)):
        monkeypatch.setattr(liara, n, f)
    res = run(tools.TOOLS['deploy_template']['fn']({'template_id': 'fastapi', 'app': 'old-app'}, {}))
    assert 'create_app' not in calls and res['created_app'] is False
    sent = calls[0][1]
    assert sent['OLD'] == 'keep' and sent['SECRET_KEY'] == 'existing-secret' and sent['APP_ENV'] == 'production'


def test_save_from_project_strips_secrets(fresh_db, monkeypatch, tmp_path):
    from negahban import analyzer
    pid = 'proj_' + 'a' * 12
    d = tmp_path / pid / 'src'
    (d / 'app').mkdir(parents=True)
    monkeypatch.setattr(analyzer, 'project_dir', lambda p: d)
    (d / 'main.py').write_text("import os\nDB = os.environ['DATABASE_URL']\nKEY = os.getenv('STRIPE_API_KEY')\nMODE = os.getenv('APP_MODE')\n"
                               "FALLBACK = 'postgres://admin:hunter2pass@db.local/x'\nTOKEN = 'typed-value-xyz123'\n")
    (d / 'requirements.txt').write_text('fastapi\nuvicorn\n')
    (d / 'Dockerfile').write_text('FROM python:3.12-slim\nCOPY . .\nEXPOSE 8000\nCMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]\n')
    (d / '.env').write_text('DATABASE_URL=postgres://u:supersecret@h/db\n')
    (d / 'id_rsa').write_text('-----BEGIN PRIVATE KEY-----')
    (d / 'app' / 'credentials.json').write_text('{"k": "v"}')
    store.kv_set(f'project:{pid}', {'port': 8000, 'health_path': '/', 'envs': {'TOKEN': 'typed-value-xyz123'}})
    a = analyzer.analyze_dir(d)
    t = T.save_from_project(pid, 'pay-api', a)
    blob = json.dumps(t, ensure_ascii=False)
    for leak in ('supersecret', 'hunter2pass', 'typed-value-xyz123', 'PRIVATE KEY'):
        assert leak not in blob
    assert '.env' not in t['files'] and 'id_rsa' not in t['files'] and 'app/credentials.json' not in t['files']
    env = {e['name']: e for e in t['env']}
    assert env['DATABASE_URL']['secret'] and env['STRIPE_API_KEY']['secret'] and not env['APP_MODE']['secret']
    assert all(e['default'] == '' for e in t['env'])
    assert 'EXPOSE {{port}}' in t['files']['Dockerfile'] and '"--port","{{port}}"' in t['files']['Dockerfile']
    assert t['origin'] == f'project:{pid}' and t['stack'] == 'docker'
    assert T.render(t, app='pay-api')['Dockerfile'].count('8000') == 2
    again = T.save_from_project(pid, 'pay-api', a)  # redeploy of the same project -> same template (new version only if changed)
    assert again['id'] == t['id']


def test_save_from_project_borrows_dockerfile(fresh_db, monkeypatch, tmp_path):
    from negahban import analyzer
    pid = 'proj_' + 'b' * 12
    d = tmp_path / pid / 'src'
    d.mkdir(parents=True)
    monkeypatch.setattr(analyzer, 'project_dir', lambda p: d)
    (d / 'package.json').write_text(json.dumps({'name': 'web-shop', 'dependencies': {'express': '^4'}, 'scripts': {'start': 'node index.js'}}))
    (d / 'index.js').write_text("require('express')().listen(process.env.PORT || 3000)\nconst s = process.env.SESSION_SECRET\n")
    t = T.save_from_project(pid, 'web-shop', None)
    assert t and 'node:20-alpine' in t['files']['Dockerfile'] and 'healthcheck.js' in t['files'] and t['health_check']
    assert '{{app_name}}' in t['files']['package.json'] and t['stack'] == 'node'


def test_api_router(offline, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from negahban import security
    from negahban.templates_api import router
    app = FastAPI()
    app.include_router(router)
    cl = TestClient(app)
    assert cl.get('/api/templates').status_code == 401
    tok, _ = security.make_session()
    cl.cookies.set(security.COOKIE, tok)
    cl.cookies.set(security.CSRF_COOKIE, 'csrf-x')
    h = {'X-CSRF-Token': 'csrf-x'}
    r = cl.get('/api/templates')
    assert r.status_code == 200 and SEEDS <= {t['id'] for t in r.json()['templates']} and r.json()['plans']
    v = cl.get('/api/templates/fastapi').json()
    assert any(f['path'] == 'liara.json' for f in v['rendered']) and v['versions'] and v['editable']['port'] == 8000
    assert cl.post('/api/templates/fastapi/clone', json={'name': 'x'}).status_code == 403  # csrf
    c = cl.post('/api/templates/fastapi/clone', json={'name': 'api clone'}, headers=h).json()
    bad = cl.put(f"/api/templates/{c['id']}", json={'port': 99999}, headers=h)
    assert bad.status_code == 400 and 'پورت' in bad.json()['detail']
    assert cl.put('/api/templates/fastapi', json={'port': 9000}, headers=h).status_code == 400
    ok = cl.put(f"/api/templates/{c['id']}", json={'port': 9001, 'note': 'پورت جدید'}, headers=h).json()
    assert ok['version'] == 2
    assert cl.post(f"/api/templates/{c['id']}/restore/1", headers=h).json()['port'] == 8000
    assert cl.post('/api/templates/validate', json={'name': 'x'}, headers=h).json()['ok'] is False
    assert cl.request('DELETE', f"/api/templates/{c['id']}", json={'confirm_name': 'nope'}, headers=h).status_code == 400
    assert cl.request('DELETE', f"/api/templates/{c['id']}", json={'confirm_name': 'api clone'}, headers=h).status_code == 200
    assert cl.get(f"/api/templates/{c['id']}").status_code == 404
    dep = cl.post('/api/templates/django-postgres/deploy', json={'app': 'dj-shop', 'envs': {'DJANGO_DEBUG': '0'}}, headers=h)
    assert dep.status_code == 200 and dep.json()['proposal']['status'] == 'pending_approval'
    pid = dep.json()['proposal']['id']
    assert cl.post('/api/templates/django-postgres/deploy', json={'app': 'dj-shop', 'envs': {'DATABASE_URL': 'x'}}, headers=h).status_code == 400
    assert cl.post(f'/api/templates/proposals/{pid}/secrets', json={'values': {'NOT_ALLOWED': 'x'}}, headers=h).status_code == 400
    s = cl.post(f'/api/templates/proposals/{pid}/secrets', json={'values': {'DATABASE_URL': 'postgres://u:pw-123456@h/db'}}, headers=h)
    assert s.status_code == 200 and s.json()['stored'] == ['DATABASE_URL']
    assert 'pw-123456' not in json.dumps(store.get_proposal(pid), ensure_ascii=False)
