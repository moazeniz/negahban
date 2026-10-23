"""One real end-to-end proof of the IaaS tools through Negahban's approval flow (user-approved, 2026-10-22):
create a server on the cheapest plan from the `ubuntu-mini` template (propose -> owner approves -> execute), verify it appears
and boots (power on, IP, SSH port open), then delete it through the type-the-name card. A finally-block deletes the server
if anything fails, so no stray server keeps billing. Evidence (redacted) goes to evidence/iaas-e2e-<ts>.json.
Usage (Hetzner): ENV_FILE=/root/.liara.env DATA_DIR=/tmp/ngh-iaas-e2e .venv/bin/python deploy/iaas_e2e.py"""
import asyncio, json, os, socket, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for line in open(os.environ['ENV_FILE']):
    if '=' in line and not line.startswith('#'):
        k, v = line.strip().split('=', 1)
        os.environ.setdefault(k, v.strip('"\''))
os.environ.setdefault('DATA_DIR', '/tmp/ngh-iaas-e2e')
os.environ.setdefault('ORCH_ENABLED', '0')

from negahban import approvals, store, tools  # noqa: E402
from negahban.diag import iaas, register  # noqa: E402
from negahban.liara import liara  # noqa: E402
from negahban.redact import redact_obj  # noqa: E402

store.init()
register(tools.tool)  # idempotent; makes the proof independent of the tools.py wiring
LOG = []


def log(step, **kw):
    row = {'t': round(time.time() - T0, 1), 'step': step, **kw}
    LOG.append(row)
    print(json.dumps(redact_obj(row), ensure_ascii=False, default=str)[:600], flush=True)


async def settle(pid, timeout=300):
    t = time.time()
    while time.time() - t < timeout:
        p = store.get_proposal(pid)
        if p['status'] not in ('pending', 'executing', 'verifying'):
            return p
        await asyncio.sleep(3)
    return store.get_proposal(pid)


def ssh_open(ip, port=22, timeout=5):
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(64).decode(errors='replace').strip()[:40] or 'open'
    except OSError as e:
        return f'closed ({type(e).__name__})'


async def balance():
    b = await liara.billing()
    return (b.get('team') or b.get('user') or {}).get('balance')


async def main():
    ctx = {'origin': 'e2e', 'actor': 'owner', 'session': 'iaas-e2e'}
    name = 'ngh-e2e-' + time.strftime('%H%M')
    created = False
    bal0 = await balance()
    log('balance_before', toman=bal0)
    assert not (await iaas.vms()), 'team already has servers; refusing to run the proof next to them'
    try:
        r = await tools.run('create_server', {'name': name, 'template': 'ubuntu-mini', 'reason': 'user-approved e2e proof'}, ctx)
        prop = r['proposal']
        log('proposed_create', id=prop['id'], title=prop['title'], impact=prop['impact'])
        a = await approvals.approve(prop['id'], 'owner')
        log('approved_create', status=a.get('status'), error=a.get('error'))
        p = await settle(prop['id'])
        created = True
        log('executed_create', status=p['status'], result=p['result'])
        lst = await tools.run('list_servers', {}, ctx)
        me = next((s for s in lst['servers'] if s['name'] == name), None)
        log('listed', server=me)
        det = await tools.run('server_details', {'server': name}, ctx)
        log('details', power=det.get('power'), guest=det.get('guest'), ips=det.get('ips'), ops=det.get('operations'), leaked_password='rootPassword' in json.dumps(det))
        ip = (det.get('ips') or [None])[0]
        banner = None
        for _ in range(12):  # SSH comes up a little after the hypervisor reports POWERED_ON
            banner = ssh_open(ip, det.get('ssh_port') or 22) if ip else 'no ip'
            if banner.startswith('SSH'):
                break
            await asyncio.sleep(10)
        log('ssh_check', ip=ip, banner=banner)
        r = await tools.run('delete_server', {'server': name, 'reason': 'end of e2e proof'}, ctx)
        dprop = r['proposal']
        log('proposed_delete', id=dprop['id'], title=dprop['title'], impact=dprop['impact'])
        bad = await approvals.approve(dprop['id'], 'owner', {'confirm_name': name + 'x'})
        log('wrong_name_attempt_is_executed_but_refused', status=(await settle(dprop['id']))['status'], result=store.get_proposal(dprop['id'])['result'])
        r = await tools.run('delete_server', {'server': name, 'reason': 'end of e2e proof (typed name)'}, ctx)
        dprop = r['proposal']
        await approvals.approve(dprop['id'], 'owner', {'confirm_name': name})
        p = await settle(dprop['id'])
        log('executed_delete', status=p['status'], result=p['result'])
        created = p['status'] != 'done'
    finally:
        if created:
            try:
                v = await iaas.find(name)
                await iaas.call('DELETE', f"/vm/{v['_id']}")
                log('cleanup_forced_delete', id=v['_id'])
            except Exception as e:
                log('cleanup', note=f'{type(e).__name__}: {str(e)[:120]}')
    left = [v['name'] for v in await iaas.vms()]
    log('servers_after', servers=left)
    await asyncio.sleep(20)
    bal1 = await balance()
    log('balance_after', toman=bal1, delta=round((bal0 or 0) - (bal1 or 0), 2), note='Liara bills hourly; the charge may post with a delay and is shared with the running apps')
    out = os.path.join(ROOT, 'evidence', f"iaas-e2e-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w') as f:
        json.dump(redact_obj({'name': name, 'seconds': round(time.time() - T0), 'log': LOG}), f, ensure_ascii=False, indent=1, default=str)
    print('evidence:', out)
    await liara.close()


T0 = time.time()
asyncio.run(main())
