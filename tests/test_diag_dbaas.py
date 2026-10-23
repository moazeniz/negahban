import asyncio, json
import httpx
import pytest
from negahban import store, tools
from negahban.liara import Liara
from negahban.diag import dbaas, iaas, liara_data, register
from negahban.diag.common import DiagError

ROOT_PW = 'R00t-Pa55-xyz-987'
ME = {'plans': {
    'databases': {'small-g2': {'available': True, 'price': 659.72, 'volume': 5, 'RAM': {'amount': 0.5}, 'CPU': {'amount': 0.5}, 'supports': ['mysql', 'mariadb', 'mongodb', 'postgres', 'redis']},
                  'medium-g2': {'available': True, 'price': 1145.83, 'volume': 10, 'RAM': {'amount': 1}, 'CPU': {'amount': 1}, 'supports': ['mysql', 'postgres', 'redis', 'mongodb']},
                  'standard-base-g2': {'available': True, 'price': 2048.61, 'volume': 20, 'RAM': {'amount': 2}, 'CPU': {'amount': 1}, 'supports': ['postgres', 'mssql']},
                  'nano': {'available': False, 'price': 50, 'supports': ['postgres']}},
    'databaseBundlePlans': {'small-g2': {'basic': {'available': True, 'price': 381.94, 'backupRetention': {'manual': 0}}, 'standard': {'available': True, 'price': 659.72, 'backupRetention': {'manual': 2, 'daily': 7}}},
                            'medium-g2': {'basic': {'available': True, 'price': 659.72, 'backupRetention': {}}},
                            'standard-base-g2': {'basic': {'available': True, 'price': 1145.83, 'backupRetention': {}}}}},
    'databaseVersions': {'postgres': [{'label': '17', 'value': '17.10'}, {'label': '16', 'value': '16.14'}], 'redis': [{'label': '8', 'value': '8.0.2'}], 'mysql': [{'value': '8.0.30'}]}}


def run(c):
    return asyncio.run(c)


class Fake:
    def __init__(self, balance=5_000_000, run_rate=8000):
        self.dbs, self.seen, self.envs, self.balance, self.run_rate, self.reset_calls = {}, [], {'demo-shop': {'APP_VERSION': '1', 'DB_HOST': 'old'}}, balance, run_rate, 0
        self.backups = [{'name': 'b-2026-10-23', 'lastModified': 'x', 'size': 2 ** 21}]

    def __call__(self, req):
        assert req.url.params['teamID'] == 'team1'
        m, p = req.method, req.url.path
        body = json.loads(req.content) if req.content else None
        self.seen.append((m, p, body))
        if p == '/v1/me':
            return httpx.Response(200, json=ME)
        if p == '/v1/billing':
            return httpx.Response(200, json={'team': {'balance': self.balance}})
        if p == '/v1/usage-report':
            return httpx.Response(200, json={'totalHourlyPrice': self.run_rate, 'projects': []})
        if p == '/v1/databases' and m == 'GET':
            return httpx.Response(200, json={'databases': [{k: v for k, v in d.items() if k not in ('root_password',)} for d in self.dbs.values()]})
        if p == '/v1/databases' and m == 'POST':
            i = f'D{len(self.dbs) + 1}'
            self.dbs[i] = {'_id': i, 'hostname': body['hostname'], 'type': body['type'], 'version': body['version'], 'planID': body['planID'], 'bundlePlanID': body['bundlePlanID'],
                           'status': 'RUNNING', 'scale': 1, 'publicNetwork': body['publicNetwork'], 'root_password': ROOT_PW, 'username': 'root', 'dbName': 'app',
                           'internalPort': 5432, 'port': 31234, 'node': {'host': 'pub.liara.ir'}, 'metaData': {'privateNetwork': True},
                           'connectionString': f"postgresql://root:{ROOT_PW}@{body['hostname']}:5432/app"}
            return httpx.Response(200, json={'databaseID': i})
        if p.startswith('/v1/projects/') and m == 'GET':
            app = p.split('/')[3]
            return httpx.Response(200, json={'project': {'envs': [{'key': k, 'value': v} for k, v in self.envs.get(app, {}).items()]}})
        if p == '/v1/projects/update-envs':
            self.envs[body['project']] = {v['key']: v['value'] for v in body['variables']}
            return httpx.Response(200, json={})
        parts = p.strip('/').split('/')
        if parts[:2] in (['v1', 'databases'], ['v2', 'databases']) and len(parts) >= 3:
            d = self.dbs.get(parts[2])
            if not d:
                return httpx.Response(404, json={'message': 'not found'})
            tail = parts[3:]
            if not tail and m == 'GET':
                return httpx.Response(200, json={'database': dict(d)})
            if not tail and m == 'DELETE':
                self.dbs.pop(parts[2])
                return httpx.Response(200, json={})
            if tail == ['actions', 'scale']:
                d['status'] = 'RUNNING' if body['scale'] else 'STOPPED'
                return httpx.Response(200, json={})
            if tail == ['resize']:
                d['planID'] = body['planID']
                return httpx.Response(200, json={})
            if tail == ['backups'] and m == 'GET':
                return httpx.Response(200, json={'backups': self.backups})
            if tail == ['backups'] and m == 'POST':
                return httpx.Response(200, json={})
            if len(tail) == 3 and tail[0] == 'backups' and tail[2] == 'download':
                return httpx.Response(200, json={'link': 'https://backup.liara.ir/dl?sig=SECRETSIG123'})
            if tail == ['reset-password']:
                self.reset_calls += 1
                if self.reset_calls > 1:
                    return httpx.Response(429, json={'message': 'too many'})
                d['root_password'] = 'N3w-Pa55-abc-321'
                return httpx.Response(200, json={})
            if tail == [f"{d['type']}-bak"]:
                return httpx.Response(200, json={})
            if tail == ['metrics', 'summary']:
                return httpx.Response(200, json={'cpuUsage': [], 'memoryUsage': []})
            if tail[:1] == ['public-network']:
                d['publicNetwork'] = tail[1] == 'enable'
                return httpx.Response(200, json={})
        return httpx.Response(404, json={'message': f'no route {m} {p}'})


@pytest.fixture
def api(monkeypatch, fresh_db):
    f = Fake()
    cl = Liara(token='tok-xyz-123456', team='team1', base='https://api.test', transport=httpx.MockTransport(f))
    for mod in (dbaas, liara_data):
        monkeypatch.setattr(mod, 'liara', cl)
    monkeypatch.setattr(dbaas, '_cache', {'me': (0, None)})
    monkeypatch.setattr(dbaas.settings, 'liara_network', 'NET1')

    async def nosleep(s):
        return None
    monkeypatch.setattr(dbaas.asyncio, 'sleep', nosleep)
    store.x('delete from kv')
    return f


def leak(*objs):
    s = json.dumps(objs, default=str) + json.dumps(store.audit_list(200), default=str) + json.dumps(store.proposals(limit=200), default=str)
    return any(x in s for x in (ROOT_PW, 'N3w-Pa55-abc-321', 'SECRETSIG123'))


def test_plans_prices_versions(api):
    ps = run(dbaas.plans('postgres'))
    assert ps[0]['plan'] == 'small-g2' and dbaas.price(ps[0], 'basic') == {'toman_per_hour': 1041.66, 'toman_per_month': 749995}
    assert 'nano' not in [p['plan'] for p in ps] and run(dbaas.versions('postgres')) == {'postgres': ['17.10', '16.14']}
    assert [p['plan'] for p in run(dbaas.plans('mssql'))] == ['standard-base-g2']


def test_create_with_precheck_connect_and_no_secret_anywhere(api):
    r = run(dbaas.create('shop-db', 'postgres', connect_app='demo-shop', ctx={'actor': 't'}))
    assert r['running'] and r['plan'] == 'small-g2' and r['version'] == '17.10' and r['toman_per_hour'] == 1041.66
    post = next(b for m, p, b in api.seen if m == 'POST' and p == '/v1/databases')
    assert post['network'] and post['bundlePlanID'] == 'basic' and post['publicNetwork'] is False
    env = api.envs['demo-shop']
    assert env['APP_VERSION'] == '1' and env['DB_HOST'] == 'shop-db' and env['DB_PORT'] == '5432' and env['DB_PASSWORD'] == ROOT_PW
    assert env['DATABASE_URL'] == f'postgresql://root:{ROOT_PW}@shop-db:5432/app'
    assert r['connected']['overwritten'] == ['DB_HOST'] and 'DB_PASSWORD' in r['connected']['env_names']
    lst, det = run(dbaas.list_databases()), run(dbaas.details('shop-db'))
    assert det['host'] == 'shop-db' and det['public_host'] == 'pub.liara.ir' and det['db_name'] == 'app'
    assert not leak(r, lst, det)


def test_precheck_refuses_card_and_create(api):
    api.balance = 100_000  # < one week of (8000 + 1041) toman/h
    with pytest.raises(DiagError, match='۴۲۹'):
        run(dbaas._create_impact({'name': 'x-db', 'engine': 'postgres'}))
    with pytest.raises(DiagError, match='۴۲۹'):
        run(dbaas.create('x-db', 'postgres'))
    assert not any(p == '/v1/databases' and m == 'POST' for m, p, b in api.seen)
    pre = run(liara_data.week_precheck(1000))
    assert pre['ok'] is False and pre['need'] == round(9000 * 168)


def test_cards_show_cost_names_only_and_typed_delete(api):
    run(dbaas.create('shop-db', 'postgres'))
    imp, rb = run(dbaas._create_impact({'name': 'other-db', 'engine': 'redis', 'connect_app': 'demo-shop'}))
    assert imp['toman_per_hour'] == 1041.66 and 'تومان در ماه' in imp['cost'] and imp['env_names'] == ['REDIS_URL', 'REDIS_HOST', 'REDIS_PORT', 'REDIS_PASSWORD']
    imp, _ = run(dbaas._connect_impact({'database': 'shop-db', 'app': 'demo-shop'}))
    assert imp['overwrites'] == ['DB_HOST'] and imp['values'] == 'نمایش داده نمی‌شود'
    with pytest.raises(DiagError, match='locked'):
        run(dbaas._connect_impact({'database': 'shop-db', 'app': 'meowmeow-wallex'}))
    register(tools.tool)
    for t in ('delete_database', 'restore_database_backup'):
        assert 'confirm_name' not in tools.TOOLS[t]['schema']['properties']
    with pytest.raises(DiagError, match='does not match'):
        run(dbaas.delete('shop-db', 'wrong'))
    assert run(dbaas.delete('shop-db', 'shop-db'))['deleted'] == 'shop-db' and not api.dbs


def test_power_resize_backups_restore_reset_password_secret_once(api):
    run(dbaas.create('shop-db', 'postgres'))
    assert run(dbaas.scale('shop-db', False))['status'] == 'STOPPED'
    assert run(dbaas.restart('shop-db'))['restarted']
    assert run(dbaas.resize('shop-db', 'medium-g2'))['to'] == 'medium-g2'
    with pytest.raises(DiagError, match='does not support'):
        run(dbaas.create('ms-db', 'mssql', plan='small-g2'))
    assert run(dbaas.backups('shop-db'))['backups'][0]['mb'] == 2.0
    lk = run(dbaas.backup_link('shop-db', 'b-2026-10-23'))
    assert lk['link'] == 'hidden' and iaas.reveal_secret(lk['secret_once'].rsplit('/', 1)[1])['value'].endswith('SECRETSIG123')
    with pytest.raises(DiagError):
        run(dbaas.restore('shop-db', 'b-2026-10-23', 'nope'))
    assert run(dbaas.restore('shop-db', 'b-2026-10-23', 'shop-db'))['status'] == 'started'
    assert any(p.endswith('/postgres-bak') and b == {'url': 'https://backup.liara.ir/dl?sig=SECRETSIG123'} for m, p, b in api.seen)
    r = run(dbaas.reset_password('shop-db'))
    tok = r['secret_once'].rsplit('/', 1)[1]
    assert iaas.reveal_secret(tok)['value'] == 'N3w-Pa55-abc-321' and iaas.reveal_secret(tok) is None
    with pytest.raises(DiagError, match='429'):
        run(dbaas.reset_password('shop-db'))
    assert not leak(r, lk)


def test_strip_secrets_like_root_password():
    o = {'database': {'hostname': 'h', 'root_password': 'pw-123456', 'password': 'x' * 8, 'connectionString': 'postgres://u:pw-123456@h:5432/d',
                      'nested': [{'uri': 'mongodb://a:b-secret-9@h/x', 'note': 'see postgresql://root:pw-123456@h/app'}]}}
    s = json.dumps(dbaas.strip_secrets(o))
    assert 'pw-123456' not in s and 'b-secret-9' not in s and '"hostname": "h"' in s


def test_tools_via_registry_redact_and_route_sensitive(api, fresh_db, monkeypatch):
    import negahban.approvals as ap

    async def fp(app):
        return 'fp'
    monkeypatch.setattr(ap, 'state_fingerprint', fp)
    register(tools.tool)
    run(dbaas.create('shop-db', 'postgres'))
    out = run(tools.run('database_details', {'database': 'shop-db'}, {'actor': 't'}))
    assert out['engine'] == 'postgres' and not leak(out)
    card = run(tools.run('connect_database', {'database': 'shop-db', 'app': 'demo-shop'}, {'actor': 't', 'origin': 'chat'}))
    assert card['proposal']['status'] == 'pending_approval' and 'env_names' in card['proposal']['impact']
    assert 'DB_PASSWORD' not in api.envs['demo-shop'] and not leak(card)  # nothing executed before approval


# ---- regressions found in the live test-team run (2026-10-23)
def test_live_regressions_db_name_price_and_secret_once_survives_redaction():
    from negahban.redact import redact_obj
    env = dbaas.connection_env('postgres', {'hostname': 'pg1', 'internalPort': 5432, 'username': 'root', 'root_password': 'pw-123456'})
    assert env['DB_NAME'] == 'postgres' and env['DATABASE_URL'].endswith(':5432/postgres')
    row = dbaas.summarize({'planID': 'small-g2', 'bundlePlanID': 'basic', 'hourlyPrice': 659.72, 'bundleHourlyPrice': 381.94})
    assert row['toman_per_hour'] == 1041.66  # plan + bundle, not the plan part only
    out = redact_obj({'secret_once': '/api/diag/secret/abc123', 'secret_key': 'hidden-value', 'access_key': '***'})
    assert out['secret_once'] == '/api/diag/secret/abc123' and out['secret_key'] == '***'


def test_addons_skip_when_app_already_connected(monkeypatch):
    from negahban.liara import liara as L
    from negahban.diag import addons

    async def envs(app):
        return {'DATABASE_URL': 'postgresql://x', 'LIARA_BUCKET_NAME': 'b'}
    monkeypatch.setattr(L, 'envs', envs)
    cards = run(addons.propose_addons('shop', {'database': {'engine': 'postgres'}, 'storage': {}}, {}))
    assert [c.get('skipped') for c in cards] == [True, True]
