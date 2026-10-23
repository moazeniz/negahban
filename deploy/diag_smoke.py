"""Read-only live smoke test of the diagnostics toolkit against real Liara apps (default demo-shop and bazargard-ai).
Usage (Hetzner): ENV_FILE=/root/.liara.env DATA_DIR=/tmp/ngh-smoke .venv/bin/python deploy/diag_smoke.py [app ...] [--only name,name]
Only read tools run (no sensitive tool, no approval cards). Output is redacted and truncated; secrets are never printed."""
import asyncio, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if os.environ.get('ENV_FILE'):
    for line in open(os.environ['ENV_FILE']):
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ.setdefault(k, v.strip('"\''))
os.environ.setdefault('DATA_DIR', '/tmp/ngh-diag-smoke')
os.environ.setdefault('ORCH_ENABLED', '0')

from negahban import store  # noqa: E402
from negahban.diag import all_specs, _wrap  # noqa: E402
from negahban.redact import redact  # noqa: E402

store.init()
args = [a for a in sys.argv[1:] if not a.startswith('--')]
only = next((a.split('=', 1)[1] for a in sys.argv[1:] if a.startswith('--only=')), '')
APPS = args or ['demo-shop', 'bazargard-ai']


def plan(app):
    return [
        ('container_diag', {'app': app, 'check': 'overview'}), ('container_diag', {'app': app, 'check': 'ports'}),
        ('container_diag', {'app': app, 'check': 'env_names', 'expect': ['PORT', 'DATABASE_URL']}), ('container_diag', {'app': app, 'check': 'http_local', 'path': '/'}),
        ('container_diag', {'app': app, 'check': 'sqlite_check', **({'path': '/data/places.db'} if app == 'bazargard-ai' else {})}), ('container_diag', {'app': app, 'check': 'runtime_info'}),
        ('container_exec', {'app': app, 'command': 'cat /proc/loadavg'}), ('container_exec', {'app': app, 'command': 'df -h'}),
        ('container_exec', {'app': app, 'command': 'printenv'}), ('container_exec', {'app': app, 'command': 'ps aux'}),
        ('http_probe', {'app': app, 'path': '/'}), ('tls_check', {'host': f'{app}.liara.run'}), ('dns_lookup', {'name': f'{app}.liara.run', 'compare': True}),
        ('release_details', {'app': app}), ('events_timeline', {'app': app, 'hours': 24}), ('network_metrics', {'app': app, 'hours': 3}),
        ('disks_info', {'app': app}), ('disk_files', {'app': app}), ('container_diag', {'app': app, 'check': 'data_usage'}), ('metric_anomalies', {'app': app, 'metric': 'memory', 'hours': 6}),
        ('resource_forecast', {'app': app, 'hours': 6}), ('deploy_impact', {'app': app, 'releases': 2}), ('slo_report', {'app': app}),
        ('log_insights', {'app': app, 'minutes': 180}), ('llm_usage', {'app': app, 'hours': 6}),
        ('run_runbook', {'app': app, 'runbook': 'auto', 'minutes': 60}), ('run_runbook', {'app': app, 'runbook': 'disk_full'}),
    ]


GLOBAL = [('billing_details', {}), ('cost_forecast', {'monthly_budget_toman': 3000000}), ('databases_status', {}), ('storage_buckets', {}),
          ('dns_zones', {}), ('domains_health', {}), ('web_search_error', {'error': 'sqlite3.OperationalError: database is locked', 'context': 'FastAPI on Liara'})]


async def main():
    specs = {s['name']: s for s in all_specs()}
    calls = [c for app in APPS for c in plan(app)] + GLOBAL
    if only:
        calls = [c for c in calls if c[0] in only.split(',')]
    ok = fail = 0
    for name, a in calls:
        s = specs[name]
        assert s['risk'] != 'sensitive', name
        t0 = time.perf_counter()
        try:
            r = await _wrap(s)(a, {'actor': 'smoke'})
            r.pop('_attach', None) if isinstance(r, dict) else None
            ok += 1
            print(f'OK   {name} {json.dumps(a, ensure_ascii=False)} {time.perf_counter() - t0:.1f}s\n     {redact(json.dumps(r, ensure_ascii=False, default=str))[:700]}')
        except Exception as e:
            fail += 1
            print(f'FAIL {name} {json.dumps(a, ensure_ascii=False)} {time.perf_counter() - t0:.1f}s {type(e).__name__}: {redact(str(e))[:300]}')
    print(f'\n{ok} ok, {fail} failed')


asyncio.run(main())
