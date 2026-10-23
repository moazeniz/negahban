import asyncio, inspect, re
import pytest
from negahban import tools
from negahban.diag import LABELS, SENSITIVE, _wrap, all_specs, register
from negahban.diag.common import DiagError, shrink
from negahban.tools import ToolError

CORE = {'list_apps', 'app_status', 'get_logs', 'analyze_logs', 'get_metrics', 'get_events', 'list_releases', 'build_logs', 'probe_url', 'plans_and_prices',
        'estimate_cost', 'account_balance', 'list_domains', 'domain_setup_guide', 'diagnose', 'list_archives', 'list_incidents', 'get_policy', 'workspace_info',
        'run_tests', 'propose_code_fix', 'analyze_project', 'chart_metrics', 'chart_costs', 'show_chart', 'show_diagram', 'topology_diagram', 'team_usage',
        'running_info', 'restart_app', 'scale_app', 'resize_app', 'set_env', 'unset_env', 'toggle_zero_downtime', 'rollback_release', 'deploy_workspace',
        'apply_code_fix', 'redeploy_archive', 'create_app', 'deploy_project', 'add_domain', 'enable_ssl', 'create_dns_zone', 'delete_app',
        'list_templates', 'show_template', 'save_template', 'deploy_template', 'set_default_subdomain', 'remember', 'recall', 'forget'}


def test_specs_are_complete_and_distinct():
    specs = all_specs()
    names = [s['name'] for s in specs]
    assert len(names) == len(set(names)) and not set(names) & CORE and len(names) >= 25
    for s in specs:
        assert s['risk'] in ('read', 'safe', 'sensitive') and re.search('[؀-ۿ]', s['label_fa']) and len(s['desc']) > 40
        assert set(s['required']) <= set(s['params']) and inspect.iscoroutinefunction(s['fn'])
        if s['risk'] == 'sensitive':
            assert callable(s['title']) and callable(s['impact']), s['name']
    assert {s['name'] for s in specs if s['risk'] == 'sensitive'} == {'container_exec_any', 'load_test', 'db_query', 'create_server', 'server_power', 'resize_server',
                                                                      'server_add_disk', 'server_extend_disk', 'server_reset_password', 'server_reset_network', 'delete_server',
                                                                      'create_database', 'connect_database', 'database_power', 'resize_database', 'backup_database',
                                                                      'database_backup_link', 'restore_database_backup', 'reset_database_password', 'database_public_network',
                                                                      'delete_database', 'bucket_file_link', 'create_bucket', 'connect_bucket', 'bucket_permission',
                                                                      'resize_bucket', 'delete_bucket', 'create_storage_key', 'revoke_storage_key', 'delete_storage_key'}


def test_register_uses_core_decorator_signature():
    got = {}

    def tool(name, risk, desc, params=None, required=(), title=None, impact=None, label_fa=None):
        def deco(fn):
            got[name] = {'risk': risk, 'label_fa': label_fa, 'title': title, 'impact': impact, 'schema_props': params, 'fn': fn}
            return fn
        return deco
    register(tool)
    assert got['container_exec_any']['title'] and got['container_exec_any']['impact'] and LABELS['http_probe'].startswith('در حال')
    assert 'db_query' in SENSITIVE and 'http_probe' not in SENSITIVE
    register(tools.tool)  # the real registry accepts every spec and its schema validates
    tools.validate('container_diag', {'app': 'demo-shop', 'check': 'memory'})
    with pytest.raises(ToolError):
        tools.validate('container_diag', {'app': 'demo-shop', 'check': 'rm'})


def test_wrapper_audits_caps_and_times_out(fresh_db):
    from negahban import store

    async def big(a, ctx):
        return {'lines': ['x' * 500] * 400, 'token': 'liara-test-token-abcdef'}

    async def slow(a, ctx):
        await asyncio.sleep(5)

    async def bad(a, ctx):
        raise DiagError('nope')
    mk = lambda fn, t=5: {'name': 'tst', 'fn': fn, 'timeout': t}
    out = asyncio.run(_wrap(mk(big))({'app': 'demo-shop'}, {'actor': 'tester'}))
    import json
    s = json.dumps(out)
    assert len(s) <= 24000 and 'liara-test-token-abcdef' not in s
    with pytest.raises(DiagError, match='timed out'):
        asyncio.run(_wrap(mk(slow, 0.1))({'app': 'demo-shop'}, {}))
    with pytest.raises(ToolError):
        asyncio.run(_wrap(mk(bad))({}, {}))
    rows = [r for r in store.audit_list(10) if r['action'] == 'diag.tst']
    assert len(rows) == 3 and rows[-1]['actor'] == 'tester' and [r['ok'] for r in rows] == [0, 0, 1]


def test_shrink_keeps_structure():
    o = {'a': list(range(1000)), 'b': 'y' * 50000, 'c': {'d': ['z' * 3000] * 50}}
    r = shrink(o, 6000)
    import json
    assert len(json.dumps(r)) <= 6000 and isinstance(r['a'], list) and 'more]' in r['a'][-1]
