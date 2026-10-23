import asyncio, json
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from negahban import security, store, tools
from negahban.liara import Liara
from negahban.diag import addons, api as dapi, dbaas, iaas, liara_data, register, storage
from negahban.diag.common import DiagError

SECRET = 'SeCrEt-KeY-0123456789abcdef'
ME = {'plans': {'objectStorage': {'free': {'available': False, 'price': 0, 'space': 2.5}, '20g-g2': {'available': True, 'price': 659.72, 'space': 20},
                                  '40g-g2': {'available': True, 'price': 1145.83, 'space': 40}},
                'databases': {'small-g2': {'available': True, 'price': 659.72, 'volume': 5, 'RAM': {'amount': 0.5}, 'CPU': {'amount': 0.5}, 'supports': ['postgres', 'redis']}},
                'databaseBundlePlans': {'small-g2': {'basic': {'available': True, 'price': 381.94}}}},
      'databaseVersions': {'postgres': [{'value': '17.10'}]}}


def run(c):
    return asyncio.run(c)


class Fake:
    def __init__(self):
        self.buckets, self.keys, self.objects, self.seen = {}, {}, {}, []
        self.envs = {'demo-shop': {'APP_VERSION': '1'}}
        self.balance = 5_000_000

    def __call__(self, req):
        assert req.url.params['teamID'] == 'team1'
        m, p = req.method, req.url.path
        body = json.loads(req.content) if req.content else None
        self.seen.append((req.url.host, m, p, body))
        if req.url.host == 'storage.test':
            p = p.removeprefix('/api/v1')
            parts = p.strip('/').split('/')
            if p == '/buckets' and m == 'GET':
                return httpx.Response(200, json={'status': 'success', 'buckets': list(self.buckets.values())})
            if p == '/buckets' and m == 'POST':
                self.buckets[body['name']] = {'name': body['name'], 'plan': body['plan'], 'permission': body['permission'], 'status': 'ACTIVE', 'id': 'B' + body['name']}
                return httpx.Response(201, json={'status': 'success'})
            if p == '/keys' and m == 'GET':
                return httpx.Response(200, json={'status': 'success', 'keys': [{k: v for k, v in x.items() if k != 'secretKey'} for x in self.keys.values()]})
            if p == '/keys' and m == 'POST':
                ak = f'AK{len(self.keys) + 1:04d}XYZQ'
                self.keys[ak] = {'id': 'K' + ak, 'accessKey': ak, 'secretKey': SECRET, 'description': body['description'], 'buckets': [{'name': b} for b in body['buckets']],
                                 'permissions': body['permissions'], 'status': 'ACTIVE'}
                return httpx.Response(201, json={'status': 'success', 'data': {'accessKey': ak, 'secretKey': SECRET}})
            if parts[0] == 'keys' and len(parts) == 3 and parts[2] == 'revoke':
                return httpx.Response(200, json={'accessKey': parts[1], 'secretKey': SECRET + '-2'})
            if parts[0] == 'keys' and m == 'DELETE':
                self.keys.pop(parts[1])
                return httpx.Response(204)
            if parts[0] == 'buckets' and len(parts) >= 2:
                b = parts[1]
                if parts[2:] == ['check-availability']:
                    return httpx.Response(409 if b == 'taken-name' else 200, json={})
                if b not in self.buckets:
                    return httpx.Response(404, json={'message': 'nope'})
                if parts[2:3] == ['objects'] and m == 'GET':
                    objs = self.objects.get(b, [])
                    return httpx.Response(200, json={'data': {'objects': {'Contents': [{'Key': k, 'Size': 2048, 'LastModified': 'x'} for k in objs[:int(req.url.params.get('number', 50))]],
                                                                          'CommonPrefixes': [], 'IsTruncated': False, 'KeyCount': len(objs)}}})
                if parts[2:3] == ['type']:
                    self.buckets[b]['permission'] = parts[3]
                    return httpx.Response(200, json={})
                if parts[2:3] == ['upgrade']:
                    self.buckets[b]['plan'] = parts[3]
                    return httpx.Response(200, json={})
                if parts[2:3] == ['download']:
                    return httpx.Response(200, json={'data': {'url': 'https://b.storage/obj?X-Amz-Signature=SIGNEDSECRET'}})
                if parts[2:] == ['metrics', 'summary']:
                    return httpx.Response(200, json={'data': {'metrics': {'total_bytes': [0, 3 * 2 ** 20], 'totol_objects': [0, len(self.objects.get(b, []))]}}})
                if len(parts) == 2 and m == 'DELETE':
                    self.buckets.pop(b)
                    return httpx.Response(204)
            return httpx.Response(404, json={'message': f'no route {m} {p}'})
        if p == '/v1/me':
            return httpx.Response(200, json=ME)
        if p == '/v1/billing':
            return httpx.Response(200, json={'team': {'balance': self.balance}})
        if p == '/v1/usage-report':
            return httpx.Response(200, json={'totalHourlyPrice': 8000})
        if p.startswith('/v1/projects/') and m == 'GET':
            return httpx.Response(200, json={'project': {'envs': [{'key': k, 'value': v} for k, v in self.envs.get(p.split('/')[3], {}).items()]}})
        if p == '/v1/projects/update-envs':
            self.envs[body['project']] = {v['key']: v['value'] for v in body['variables']}
            return httpx.Response(200, json={})
        return httpx.Response(404, json={'message': f'no route {m} {p}'})


@pytest.fixture
def api(monkeypatch, fresh_db):
    f = Fake()
    cl = Liara(token='tok-xyz-123456', team='team1', base='https://api.test', transport=httpx.MockTransport(f))
    for mod in (storage, dbaas, liara_data):
        monkeypatch.setattr(mod, 'liara', cl)
    monkeypatch.setattr(storage, 'BASE', 'https://storage.test/api/v1')
    monkeypatch.setattr(storage, '_cache', {'me': (0, None)})
    monkeypatch.setattr(dbaas, '_cache', {'me': (0, None)})
    monkeypatch.setattr(dbaas.settings, 'liara_network', 'NET1')
    store.x('delete from kv')
    return f


def leak(*objs):
    s = json.dumps(objs, default=str) + json.dumps(store.audit_list(200), default=str) + json.dumps(store.proposals(limit=200), default=str)
    return any(x in s for x in (SECRET, 'SIGNEDSECRET'))


def test_plans_create_list_usage(api):
    assert [p['plan'] for p in run(storage.plans())] == ['20g-g2', '40g-g2']
    r = run(storage.create_bucket('shop-files', ctx={'actor': 't'}))
    assert r['toman_per_hour'] == 659.72 and api.buckets['shop-files']['permission'] == 'private'
    api.objects['shop-files'] = ['a.png', 'b.png']
    lst = run(storage.list_buckets())
    assert lst['buckets'][0]['used_mb'] == 3.0 and lst['buckets'][0]['objects'] == 2 and lst['endpoint'].startswith('https://')
    assert run(storage.list_objects('shop-files', limit=1))['objects'][0]['key'] == 'a.png'
    with pytest.raises(DiagError, match='taken'):
        run(storage.create_bucket('taken-name'))
    with pytest.raises(DiagError):
        run(storage.create_bucket('Bad_Name'))


def test_precheck_refuses(api):
    api.balance = 1000
    with pytest.raises(DiagError, match='۴۲۹'):
        run(storage._create_impact({'name': 'x-files'}))
    with pytest.raises(DiagError, match='۴۲۹'):
        run(storage.create_bucket('x-files'))
    assert not any(m == 'POST' and p.endswith('/buckets') for h, m, p, b in api.seen)


def test_keys_secret_once_and_connect_names_only(api):
    run(storage.create_bucket('shop-files'))
    out, ak, sk = run(storage.create_key(['shop-files'], 'ci', ['read', 'list']))
    tok = out['secret_once'].rsplit('/', 1)[1]
    assert out['secret_key'] == 'hidden' and SECRET in iaas.reveal_secret(tok)['value'] and iaas.reveal_secret(tok) is None
    ks = run(storage.keys())
    assert ks['keys'][0]['access_key'] == 'AK00…ZQ' and SECRET not in json.dumps(ks)
    c = run(storage.connect('shop-files', 'demo-shop', 'aws'))
    env = api.envs['demo-shop']
    assert env['AWS_SECRET_ACCESS_KEY'] == SECRET and env['S3_BUCKET'] == 'shop-files' and env['APP_VERSION'] == '1'
    assert c['env_names'] == sorted(['AWS_ENDPOINT_URL_S3', 'S3_BUCKET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_DEFAULT_REGION'])
    imp, _ = run(storage._connect_impact({'bucket': 'shop-files', 'app': 'demo-shop'}))
    assert imp['env_names'] == ['LIARA_ENDPOINT', 'LIARA_BUCKET_NAME', 'LIARA_ACCESS_KEY', 'LIARA_SECRET_KEY']
    r = run(storage.revoke_key(ks['keys'][0]['id']))
    assert r['secret_key'] == 'hidden' and 'secret_once' in r
    assert run(storage.delete_key(ks['keys'][0]['id']))['deleted']
    assert not leak(out, ks, c, r)
    with pytest.raises(DiagError, match='locked'):
        run(storage.connect('shop-files', 'negahban-sre'))


def test_permission_resize_link_and_typed_empty_delete(api):
    run(storage.create_bucket('shop-files'))
    assert run(storage.set_permission('shop-files', 'public'))['permission'] == 'public'
    assert run(storage.resize_bucket('shop-files', '40g-g2'))['to'] == '40g-g2'
    with pytest.raises(DiagError, match='upgraded'):
        run(storage.resize_bucket('shop-files', '20g-g2'))
    url, exp = run(storage.temp_link('shop-files', 'a.png', 30))
    assert exp == 60 and 'SIGNEDSECRET' in url
    register(tools.tool)
    assert 'confirm_name' not in tools.TOOLS['delete_bucket']['schema']['properties']
    r = run(tools.TOOLS['bucket_file_link']['fn']({'bucket': 'shop-files', 'key': 'a.png'}, {'actor': 't'}))
    assert r['link'] == 'hidden' and not leak(r)
    api.objects['shop-files'] = ['a.png']
    with pytest.raises(DiagError, match='not empty'):
        run(storage._del_impact({'bucket': 'shop-files'}))
    with pytest.raises(DiagError, match='not empty'):
        run(storage.delete_bucket('shop-files', 'shop-files'))
    api.objects['shop-files'] = []
    with pytest.raises(DiagError, match='does not match'):
        run(storage.delete_bucket('shop-files', 'shop'))
    assert run(storage.delete_bucket('shop-files', 'shop-files'))['deleted'] == 'shop-files'


def test_addons_validate_suggest_and_propose(api, monkeypatch):
    assert addons.suggest({'databases': ['PostgreSQL', 'SQLite'], 'env_vars': ['DATABASE_URL', 'S3_BUCKET']}) == {
        'database': {**addons.DB_DEFAULT, 'engine': 'postgres'}, 'storage': addons.ST_DEFAULT}
    assert addons.suggest({'databases': ['SQLite'], 'env_vars': []}) == {}
    with pytest.raises(DiagError):
        addons.validate({'database': {'engine': 'oracle'}})
    calls = []

    async def propose(tool, args, ctx, why=None, **kw):
        calls.append((tool, args))
        return {'id': 'p' + tool, 'status': 'pending_approval'}
    import negahban.approvals as ap
    monkeypatch.setattr(ap, 'propose', propose)
    cards = run(addons.propose_addons('demo-shop', {'database': {'engine': 'postgres'}, 'storage': {}}, {'actor': 't'}))
    assert [c[0] for c in calls] == ['create_database', 'create_bucket'] and len(cards) == 2
    assert calls[0][1] == {'name': 'demo-shop-db', 'engine': 'postgres', 'bundle': 'basic', 'prefix': 'DB', 'connect_app': 'demo-shop'}
    assert calls[1][1]['connect_app'] == 'demo-shop' and calls[1][1]['name'] == 'demo-shop-files'


def test_dashboard_routes_secret_once_and_typed_approval(api, monkeypatch):
    register(tools.tool)
    app = FastAPI(); app.include_router(dapi.router)
    c = TestClient(app)
    tok, _ = security.make_session()
    c.cookies.set(security.COOKIE, tok); c.cookies.set(security.CSRF_COOKIE, 'csrf-1')
    h = {'X-CSRF-Token': 'csrf-1'}
    run(storage.create_bucket('shop-files'))
    assert c.get('/api/diag/storage').json()['count'] == 1
    stok = iaas.stash_secret('top-secret-value', 'label')
    assert c.post(f'/api/diag/secret/{stok}', headers=h).json()['value'] == 'top-secret-value'
    assert c.post(f'/api/diag/secret/{stok}', headers=h).status_code == 404
    pid = store.uid('prp_')
    store.add_proposal({'id': pid, 'tool': 'delete_bucket', 'args': {'bucket': 'shop-files'}, 'title': 't', 'created_at': 0, 'expires_at': 9e12, 'status': 'pending'})
    assert c.post(f'/api/diag/approve-typed/{pid}', json={'confirm_name': 'shop'}, headers=h).status_code == 400
    seen = []

    async def approve(p, actor, extra=None):
        seen.append((p, extra))
        return {'id': p, 'status': 'executing'}
    import negahban.approvals as ap
    monkeypatch.setattr(ap, 'approve', approve)
    assert c.post(f'/api/diag/approve-typed/{pid}', json={'confirm_name': 'shop-files'}, headers=h).status_code == 200
    assert seen == [(pid, {'confirm_name': 'shop-files'})]
    assert any('ذخیره‌سازی' in x['title'] for x in c.get('/api/diag/limitations').json())



def test_list_objects_never_sends_page_1(monkeypatch):
    # verified live: storage-service answers 500 to ?page=1 (this blocked the empty-check before delete_bucket)
    seen = []

    async def fake_get(path, **kw):
        seen.append(kw.get('params'))
        return {'data': {'objects': {'Contents': [], 'KeyCount': 0}}}

    async def fake_find(name):
        return {'name': name}
    monkeypatch.setattr(storage, 'get', fake_get)
    monkeypatch.setattr(storage, 'find', fake_find)
    run(storage.list_objects('b1', limit=1))
    run(storage.list_objects('b1', limit=500, page=2))
    assert seen == [{'number': 1}, {'number': 50, 'page': 2}]


def test_bucket_endpoint_from_cluster(monkeypatch):
    async def fake_get(path, **kw):
        return {'bucket': {'name': 'b1', 'cluster': {'endPoint': 'storage.iran.liara.site'}}}
    monkeypatch.setattr(storage, 'get', fake_get)
    assert run(storage.bucket_endpoint('b1')) == 'https://storage.iran.liara.site'
