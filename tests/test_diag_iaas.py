import asyncio, json
import httpx
import pytest
from negahban import store, tools
from negahban.liara import Liara
from negahban.diag import iaas, register, server_templates as st
from negahban.diag.common import DiagError

PLANS = {'free': {'available': False, 'hourlyPrice': 0, 'monthlyPrice': 0, 'volume': 20, 'RAM': {'amount': 2}, 'CPU': {'amount': 1}},
         'standard-base-g2': {'available': True, 'hourlyPrice': 2048.61, 'monthlyPrice': 1475000, 'volume': 20, 'RAM': {'amount': 2}, 'CPU': {'amount': 1},
                              'IPv4HourlyPrice': [277.77, 486.11], 'IPv4MonthlyPrice': [200000, 350000]},
         'standard-plus-g2': {'available': True, 'hourlyPrice': 3680.55, 'monthlyPrice': 2650000, 'volume': 40, 'RAM': {'amount': 4}, 'CPU': {'amount': 2},
                              'IPv4HourlyPrice': [277.77], 'IPv4MonthlyPrice': [200000]},
         'pro-g2': {'available': True, 'hourlyPrice': 6388.88, 'monthlyPrice': 4600000, 'volume': 80, 'RAM': {'amount': 8}, 'CPU': {'amount': 4},
                    'IPv4HourlyPrice': [277.77], 'IPv4MonthlyPrice': [200000]}}
OSS = {'one-click-apps': {'docker': ['29.1.2']}, 'ubuntu': ['24.04', '22.04'], 'debian': ['12.9']}


def run(c):
    return asyncio.run(c)


class Fake:
    """In-memory IaaS API behind the real core client (httpx.MockTransport)."""
    def __init__(self):
        self.vms, self.seen, self.scripts, self.boot_polls = {}, [], [], 0
        self.created_at = '2026-10-20T10:00:00.000Z'  # older than 24 h: deletable

    def __call__(self, req):
        assert req.url.host == 'iaas-api.test' and req.url.params['teamID'] == 'team1' and req.headers['authorization'] == 'Bearer tok-xyz-123456'
        m, p = req.method, req.url.path
        body = json.loads(req.content) if req.content else None
        self.seen.append((m, p, body))
        if p == '/plans':
            return httpx.Response(200, json={'plans': PLANS})
        if p == '/oss':
            return httpx.Response(200, json=OSS)
        if p == '/init-scripts' and m == 'POST':
            self.scripts.append(body)
            return httpx.Response(201, json={'initScript': {'_id': 'S1', 'name': body['name']}})
        if p == '/vm' and m == 'GET':
            return httpx.Response(200, json={'vms': [{k: v[k] for k in ('_id', 'name', 'plan', 'OS', 'state', 'power', 'createdAt')} for v in self.vms.values()]})
        if p == '/vm' and m == 'POST':
            vid = f'VM{len(self.vms) + 1}'
            self.vms[vid] = {'_id': vid, 'name': body['name'], 'plan': body['plan'], 'OS': body['OS'], 'state': 'CREATING', 'power': 'POWERED_OFF', 'config': body.get('config') or {},
                             'createdAt': self.created_at}
            return httpx.Response(200, json={'VMID': vid, 'taskID': 'T1'})
        parts = p.strip('/').split('/')
        vid = parts[-1] if parts[0] == 'vm' else None
        if p.startswith('/vm/power/'):
            self.vms[vid]['power'] = 'POWERED_ON' if body['action'] in ('start', 'reboot') else 'POWERED_OFF'
            return httpx.Response(200, json={})
        if p.startswith('/vm/resize/'):
            self.vms[vid]['plan'] = body['plan']
            return httpx.Response(200, json={'taskID': 'T2'})
        if p.startswith('/vm/reset-password/'):
            if self.vms[vid]['power'] != 'POWERED_OFF':
                return httpx.Response(409, json={'message': 'VM is not powered off'})
            return httpx.Response(200, json={'password': 'Sup3r-S3cret-Root!'})
        if p.startswith('/vm/operation/'):
            return httpx.Response(200, json={'operations': [{'name': 'create', 'state': 'SUCCESS', 'createdAt': 'x'}]})
        if p.startswith('/vm/disk/') and m == 'GET':
            return httpx.Response(200, json={'availableSize': 10, 'disks': [{'_id': 'D1', 'name': 'root', 'size': 20}]})
        if p.startswith('/vm/disk/') and m == 'POST':
            return httpx.Response(200, json={})
        if p.startswith('/vm/traffic-volume/'):
            return httpx.Response(200, json=[{'dstip': '1.2.3.4', 'sumbytes': str(2 * 2 ** 20)}])
        if p.startswith('/vm/traffic-graph/'):
            return httpx.Response(200, json=[{'minute': 29880000, 'dstip': '1.2.3.4', 'sumbytes': '1048576'}])
        if p.startswith('/vm/') and m == 'DELETE':
            self.vms.pop(vid)
            return httpx.Response(200, json={})
        if p.startswith('/vm/') and m == 'GET':
            v = self.vms.get(vid)
            if not v:
                return httpx.Response(404, json={'message': 'VM not found'})
            if v['state'] == 'CREATING':
                self.boot_polls += 1
                if self.boot_polls >= 2:
                    v.update(state='CREATED', power='POWERED_ON')
            return httpx.Response(200, json={**v, 'config': {**v['config'], 'rootPassword': 'leaked-root-pw-123', 'hostname': v['name']},
                                             'IPs': [{'address': '185.1.2.3', 'version': 'v4'}] if v['power'] == 'POWERED_ON' else [],
                                             'planDetails': PLANS[v['plan']], 'guestState': 'running'})
        return httpx.Response(404, json={'message': 'nope'})


@pytest.fixture
def api(monkeypatch, fresh_db):
    f = Fake()
    cl = Liara(token='tok-xyz-123456', team='team1', base='https://api.test', transport=httpx.MockTransport(f))
    monkeypatch.setattr(iaas, 'liara', cl)
    monkeypatch.setattr(iaas, 'IAAS', 'https://iaas-api.test')
    monkeypatch.setattr(iaas, '_cache', {'plans': (0, None), 'oss': (0, None)})

    async def nosleep(s):
        return None
    monkeypatch.setattr(iaas.asyncio, 'sleep', nosleep)
    store.x('delete from kv')
    return f


def test_catalog_plans_images_pick(api):
    pl = run(iaas.plan_list())
    assert [p['plan'] for p in pl] == ['standard-base-g2', 'standard-plus-g2', 'pro-g2']  # free/unavailable dropped, cheapest first
    assert pl[0]['toman_per_hour'] == 2326.38 and pl[0]['toman_per_month'] == 1675000
    assert run(iaas.pick_plan(cpu=2, ram_gb=4))['plan'] == 'standard-plus-g2'
    with pytest.raises(DiagError):
        run(iaas.pick_plan(cpu=64))
    ids = [i['os'] for i in run(iaas.images())]
    assert 'docker-29.1.2' in ids and 'ubuntu-24.04' in ids and 'debian-12.9' in ids


def test_create_from_template_boots_and_never_leaks_password(api):
    r = run(iaas.create('test-srv', 'standard-base-g2', 'ubuntu-24.04', init_script='#!/bin/bash\necho hi', extra_disk_gb=5, template_id='ubuntu-mini'))
    assert r['booted'] and r['ips'] == ['185.1.2.3'] and r['init_script_id'] == 'S1' and r['extra_disk_gb'] == 5
    post = next(b for m, p, b in api.seen if m == 'POST' and p == '/vm')
    assert post == {'name': 'test-srv', 'plan': 'standard-base-g2', 'OS': 'ubuntu-24.04', 'config': {'InitScriptID': 'S1'}}
    assert api.scripts[0]['type'] == 'shell' and api.scripts[0]['content'].startswith('#!/bin/bash')
    info = run(iaas.server_info('test-srv'))
    s = json.dumps(info)
    assert 'leaked-root-pw-123' not in s and info['cpu'] == 1 and info['toman_per_hour'] == 2326.38 and info['traffic_24h_mb'] == 2.0
    lst = run(iaas.list_servers())
    assert lst['count'] == 1 and 'leaked-root-pw-123' not in json.dumps(lst)
    with pytest.raises(DiagError, match='already exists'):
        run(iaas.create('test-srv', 'standard-base-g2', 'ubuntu-24.04'))
    with pytest.raises(DiagError):
        run(iaas.create('Bad Name', 'standard-base-g2', 'ubuntu-24.04'))
    with pytest.raises(DiagError, match='unknown image'):
        run(iaas.create('other', 'standard-base-g2', 'windows-xp'))


def test_power_resize_reset_password_once_and_delete(api):
    run(iaas.create('web1', 'standard-base-g2', 'ubuntu-24.04'))
    assert run(iaas.power('web1', 'stop'))['power'] == 'POWERED_OFF'
    r = run(iaas.reset_password('web1'))
    assert r['password'] == 'hidden' and 'Sup3r' not in json.dumps(r)
    tok = r['secret_once'].rsplit('/', 1)[1]
    assert iaas.reveal_secret(tok)['value'] == 'Sup3r-S3cret-Root!'
    assert iaas.reveal_secret(tok) is None  # one time only
    assert 'Sup3r' not in json.dumps(store.audit_list(50))
    assert run(iaas.resize('web1', 'pro-g2'))['to'] == 'pro-g2'
    with pytest.raises(DiagError, match='does not match'):
        run(iaas.delete('web1', 'web2'))
    d = run(iaas.delete('web1', 'web1'))
    assert d['deleted'] == 'web1' and d['confirmed_gone'] and not api.vms


def test_protection_blocks_actions_and_cards(api):
    run(iaas.create('prod-db', 'standard-base-g2', 'ubuntu-24.04'))
    store.set_policy('vm:prod-db', {'write_lock': True})
    for coro in (iaas.power('prod-db', 'reboot'), iaas.delete('prod-db', 'prod-db'), iaas._power_impact({'server': 'prod-db', 'action': 'stop'})):
        with pytest.raises(DiagError, match='protected'):
            run(coro)


def test_cards_show_cost_and_delete_needs_typed_name(api):
    imp, rb = run(iaas._create_impact({'name': 'node-1', 'template': 'ubuntu-2c4g'}))
    assert imp['plan'] == 'standard-plus-g2' and imp['toman_per_hour'] == 3958.32 and imp['init_script'] and 'تومان در ساعت' in imp['cost']
    imp, _ = run(iaas._create_impact({'name': 'node-2', 'cpu': 4, 'ram_gb': 8}))
    assert imp['plan'] == 'pro-g2'
    register(tools.tool)
    assert 'confirm_name' not in tools.TOOLS['delete_server']['schema']['properties']  # the LLM cannot pre-fill the confirmation
    with pytest.raises(tools.ToolError):
        tools.validate('delete_server', {'server': 'x', 'confirm_name': 'x'})


def test_create_tool_autosaves_template_and_template_versions(api):
    register(tools.tool)
    r = run(tools.TOOLS['create_server']['fn']({'name': 'api-box', 'cpu': 2, 'ram_gb': 4, 'os': 'debian-12.9'}, {'actor': 't'}))
    assert r['saved_template'] == 'srv-api-box' and r['plan'] == 'standard-plus-g2'
    t = st.must('srv-api-box')
    assert t['os'] == 'debian-12.9' and t['origin'] == 'server:api-box'
    t2 = st.update('srv-api-box', {'plan': 'pro-g2'}, 'owner')
    assert t2['version'] == 2 and [v['version'] for v in st.versions('srv-api-box')] == [2, 1]
    assert st.restore('srv-api-box', 1)['plan'] == 'standard-plus-g2'
    with pytest.raises(DiagError):
        st.update('ubuntu-mini', {'plan': 'pro-g2'})
    with pytest.raises(DiagError):
        st.create({'name': 'bad', 'plan': 'pro-g2', 'os': 'ubuntu-24.04', 'init_script': 'export API_KEY=abcdef123456'})
    with pytest.raises(DiagError):
        st.delete('srv-api-box', 'wrong')
    priced = run(st.with_prices(st.list_all()))
    mini = next(x for x in priced if x['id'] == 'ubuntu-mini')
    assert mini['toman_per_hour'] == 2326.38 and mini['cpu'] == 1 and 'init_script' not in mini


def test_young_server_cannot_be_deleted_and_card_warns_min_cost(api):
    import time as _t
    api.created_at = _t.strftime('%Y-%m-%dT%H:%M:%S.000Z', _t.gmtime(_t.time() - 3600))
    run(iaas.create('young1', 'standard-base-g2', 'ubuntu-24.04'))
    with pytest.raises(DiagError, match='24 h after creation'):
        run(iaas.delete('young1', 'young1'))
    with pytest.raises(DiagError, match='24 h'):
        run(iaas._del_impact({'server': 'young1'}))
    assert run(iaas.list_servers())['servers'][0]['deletable_from']
    imp, rb = run(iaas._create_impact({'name': 'node-3', 'template': 'ubuntu-mini'}))
    assert imp['min_cost_toman'] == round(2326.38 * 24) and '۲۴' not in imp['risk'] and '24' in imp['risk']


def test_status_merges_power_from_list_when_detail_omits_it(api, monkeypatch):
    run(iaas.create('boot1', 'standard-base-g2', 'ubuntu-24.04'))
    orig = iaas.detail

    async def no_power(vid):
        d = await orig(vid)
        d.pop('power', None)
        return d
    monkeypatch.setattr(iaas, 'detail', no_power)
    vid = next(iter(api.vms))
    assert run(iaas.status(vid))['power'] == 'POWERED_ON'
