"""Deploy Negahban itself to Liara (run on the operator host). Secrets come from 600-mode env files; never printed.
usage: python deploy/deploy_negahban.py [message]"""
import asyncio, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def envfile(p):
    out = {}
    for line in open(p):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            out[k] = v.strip('"\'')
    return out


async def main():
    liara_env = envfile('/root/.liara.env'); box = envfile('/root/.boxapi.env'); adm = envfile('/root/.negahban-admin.env'); demo = envfile('/root/.negahban-demo.env')
    os.environ.update({k: liara_env[k] for k in ('LIARA_API_TOKEN', 'LIARA_TEAM_ID', 'LIARA_API_BASE', 'LIARA_NETWORK_ID')})
    from negahban.liara import Liara, make_tarball
    li = Liara()
    app = os.environ.get('SELF_APP', 'negahban-sre')
    disks = (await li.disks((await li.app(app))['_id'])).get('disks', [])  # GET disks needs the project _id
    if not any(d['name'] == 'data' for d in disks):
        print('creating disk data (2 GB)'); await li.create_disk(app, 'data', 2)
    envs = {**{k: liara_env[k] for k in ('LIARA_API_TOKEN', 'LIARA_TEAM_ID', 'LIARA_API_BASE', 'LIARA_NETWORK_ID')},
            'BOXAPI_BASE_URL': box['BOXAPI_BASE_URL'], 'BOXAPI_KEYS': box['BOXAPI_KEYS'], 'NEGAHBAN_MODEL': 'openai/gpt-6-luna',
            **{k: adm[k] for k in ('ADMIN_PASSWORD', 'SESSION_SECRET', 'MCP_TOKEN', 'WATCHDOG_TOKEN', 'STATUS_INGEST_TOKEN') if k in adm},
            'DEMO_CHAOS_TOKEN': demo['CHAOS_TOKEN'],
            # Anthropic via the Arvan LLM gate (gate token only; the gate holds the real credential). Never deploy relay/Anthropic keys.
            'LLM_PROVIDER': os.environ.get('LLM_PROVIDER', 'anthropic'), 'LLM_GATE_URL': liara_env.get('LLM_GATE_URL', ''), 'LLM_GATE_TOKEN': liara_env.get('LLM_GATE_TOKEN', ''),
            'ANTHROPIC_MODEL': 'claude-haiku-5-5',
            # container exec goes through the team relay outside Iran (Liara's exec gateway refuses Iranian origins)
            **({'LIARA_EXEC_URL': liara_env['EXEC_RELAY_URL'], 'LIARA_EXEC_RELAY_TOKEN': liara_env['EXEC_RELAY_TOKEN']} if liara_env.get('EXEC_RELAY_URL') else {}), 'DATA_DIR': '/data', 'SELF_APP': app, 'DEMO_APP': 'demo-shop', 'ORCH_INTERVAL': '60'}
    scaler = envfile('/root/.meow-scaler.env') if os.path.exists('/root/.meow-scaler.env') else {}
    envs.update({k: v for k, v in {**liara_env, **scaler}.items() if k in ('SCALER_URL', 'SCALER_ADMIN_TOKEN', 'LB_URL', 'LB_READ_TOKEN', 'LB_WRITE_TOKEN') and v})
    # multi-team: production (LIARA_TEAM_ID) + the test team for create demos; the same Liara user/token works for both,
    # LIARA_API_TOKEN_2 is an optional per-team override. Names are refreshed from GET /v2/teams at runtime.
    envs['LIARA_TEAMS'] = json.dumps([{'id': liara_env['LIARA_TEAM_ID'], 'name': 'دوخت'},
                                      {'id': '<liara-test-team-id>', 'name': 'لیارا', 'token_env': 'LIARA_API_TOKEN_2', 'test': True}], ensure_ascii=False)
    if liara_env.get('LIARA_API_TOKEN_2'):
        envs['LIARA_API_TOKEN_2'] = liara_env['LIARA_API_TOKEN_2']
    gh = envfile('/root/.github-tokens.env') if os.path.exists('/root/.github-tokens.env') else {}
    if gh.get('NEGAHBAN_GITHUB_TOKEN'):  # GitOps (read + deploy-workflow dispatch only; allowlist enforced in negahban/gitops.py)
        envs['NEGAHBAN_GITHUB_TOKEN'] = gh['NEGAHBAN_GITHUB_TOKEN']
    tb = make_tarball(ROOT, extra_skip=('data', 'evidence', 'docs', 'deploy', 'tests', 'scaler', 'infra', 'ops', '.github'))
    print('source', len(tb), 'bytes')
    sid = await li.upload_source(app, tb)
    rid = await li.create_release(app, sid, 8080, envs, [{'name': 'data', 'mountTo': '/data'}], sys.argv[1] if len(sys.argv) > 1 else 'negahban deploy')
    print('release', rid)

    async def on(st): print('state', st, flush=True)
    st, rel = await li.wait_release(app, rid, 900, on)
    print('final', st, (rel or {}).get('failReason') or '')
    await li.close()

asyncio.run(main())
