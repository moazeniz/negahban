import time
from pathlib import Path
from negahban.analysis import parse_build_logs, parse_logs, restarts_from_events, trend
from negahban.diagnose import hypotheses

FX = Path(__file__).parent / 'fixtures'


def lines(name, t0=1000.0):
    return [{'ts': t0 + 10 * i, 'type': 'stdout', 'line': l} for i, l in enumerate((FX / name).read_text().splitlines())]


def ev(**kw):
    base = {'app': 'demo-shop', 'window_min': 20, 'status': {'scale': 1, 'status': 'ACTIVE', 'planID': 'small-g2'}, 'plan': {'cpu': 0.5, 'ram': 0.5},
            'probe': {'ok': True, 'status': 200, 'latency_ms': 120}, 'env_keys': ['PAYMENT_GATEWAY_KEY'], 'env_removed_since_last_seen': [], 'env_restorable': [],
            'releases': [], 'current_release': None, 'build': None, 'logs': parse_logs([]), 'events': [], 'cpu': {}, 'mem': {}}
    base.update(kw)
    return base


def test_parse_traceback_5xx_and_slow():
    L = parse_logs(lines('app_500.log'))
    tb = L['tracebacks'][0]
    assert tb['exc'] == 'ZeroDivisionError' and tb['top_frame']['file'] == '/app/shop/pricing.py' and tb['top_frame']['line'] == 7
    assert L['errors_5xx'] == 2 and L['requests'] == 4 and L['error_rate'] == 0.5 and len(L['slow']) == 3


def test_missing_env_and_restart_loop():
    L = parse_logs(lines('missing_env.log'))
    assert L['missing_env'] == {'PAYMENT_GATEWAY_KEY': 2} and L['restart_loop']


def test_oom_detected():
    assert parse_logs(lines('oom.log'))['oom']


def test_build_failure_package():
    b = parse_build_logs(['Step 5/8 : RUN pip install', 'ERROR: No matching distribution found for persian-invoice-toolkit==4.2.0'])
    assert b['missing_package'] == 'persian-invoice-toolkit==4.2.0'


def test_events_restarts_exclude_deploys():
    evs = [{'action': 'start', 'release': 'r1'}, {'action': 'start', 'release': 'r2'}, {'action': 'start', 'release': 'r2'}, {'action': 'start', 'release': 'r2'}, {'action': 'oom', 'release': 'r2'}]
    assert restarts_from_events(evs) == {'restarts': 2, 'oom': 1, 'die': 0, 'kill': 0}


def test_trend_projection():
    t = trend([(i * 60, 0.2 + 0.02 * i) for i in range(10)], 0.5)
    assert t['monotonic_rise'] == 1.0 and 0 < t['minutes_to_limit'] < 10


def test_hypothesis_code_exception_after_release_ranks_first():
    rel = {'_id': 'r9', 'tag': 'v9', 'state': 'READY', 'finishedAt': '1970-01-01T00:00:10Z', 'message': 'refactor', 'type': 'DEPLOY', 'createdAt': '1970-01-01T00:00:01Z'}
    H = hypotheses(ev(logs=parse_logs(lines('app_500.log')), releases=[rel], current_release='r9'))
    assert H[0]['id'] == 'code_exception' and H[0]['confidence'] >= 0.9
    assert [f['tool'] for f in H[0]['fixes']] == ['rollback_release'] or 'rollback_release' in [f['tool'] for f in H[0]['fixes']]


def test_hypothesis_missing_env_with_restore():
    H = hypotheses(ev(logs=parse_logs(lines('missing_env.log')), env_keys=['APP_VERSION'], env_removed_since_last_seen=['PAYMENT_GATEWAY_KEY'], env_restorable=['PAYMENT_GATEWAY_KEY']))
    assert H[0]['id'] == 'missing_env' and H[0]['fixes'][0] == {'tool': 'set_env', 'args': {'app': 'demo-shop', 'key': 'PAYMENT_GATEWAY_KEY', 'restore': True}, 'label': 'بازگرداندن متغیر PAYMENT_GATEWAY_KEY'}


def test_hypothesis_memory_leak_vs_undersized():
    leak = trend([(i * 60, 0.25 + 0.025 * i) for i in range(10)], 0.5)
    H = hypotheses(ev(events=[{'action': 'oom', 'release': 'r'}], mem=leak))
    assert H[0]['id'] == 'oom_leak' and H[0]['fixes'][0]['tool'] == 'restart_app'
    flat = trend([(i * 60, 0.49) for i in range(10)], 0.5)
    H = hypotheses(ev(events=[{'action': 'oom', 'release': 'r'}], mem=flat))
    assert H[0]['id'] == 'oom_undersized' and H[0]['fixes'][0] == {'tool': 'resize_app', 'args': {'app': 'demo-shop', 'plan': 'medium-g2'}, 'label': 'ارتقای پلن به medium-g2'}


def test_hypothesis_cpu_saturation_suggests_next_plan():
    H = hypotheses(ev(cpu=trend([(i * 60, 0.48) for i in range(6)], 0.5), probe={'ok': True, 'status': 200, 'latency_ms': 3200}))
    assert H[0]['id'] == 'cpu_saturation' and H[0]['fixes'][0]['args']['plan'] == 'medium-g2'


def test_hypothesis_slow_with_idle_resources_points_to_code():
    H = hypotheses(ev(probe={'ok': True, 'status': 200, 'latency_ms': 3600}, cpu=trend([(i, 0.02) for i in range(5)], 0.5), mem=trend([(i, 0.1) for i in range(5)], 0.5)))
    assert H[0]['id'] == 'slow_code'


def test_hypothesis_build_failed_keeps_old_release_serving():
    rel = {'_id': 'new', 'tag': 'v5', 'state': 'FAILED', 'type': 'DEPLOY', 'createdAt': '1970-01-01T00:00:01Z'}
    H = hypotheses(ev(releases=[rel], current_release='old', build={'error_lines': ['ERROR: No matching distribution'], 'missing_package': 'x==1', 'failReason': None}))
    assert H[0]['id'] == 'build_failed' and any('نسخه‌ی قبلی' in e for e in H[0]['evidence'])


def test_healthy_app_has_no_hypotheses():
    assert hypotheses(ev()) == []


def test_oom_with_recent_linear_rise_is_a_leak():
    flat_then_rise = [(i * 30, 0.06) for i in range(40)] + [(1200 + i * 30, 0.06 + 0.03 * i) for i in range(12)]
    H = hypotheses(ev(events=[{'action': 'oom', 'release': 'r'}], mem=trend(flat_then_rise, 0.5), mem_series=flat_then_rise))
    assert H[0]['id'] == 'oom_leak' and H[0]['fixes'][0]['tool'] == 'restart_app'


def test_predictive_leak_before_oom():
    rising = [(i * 30, 0.1 + 0.02 * i) for i in range(12)]  # 0.32 GB of 0.5 and climbing
    H = hypotheses(ev(mem=trend(rising, 0.5), mem_series=rising))
    assert H[0]['id'] == 'mem_leak' and H[0]['fixes'][0]['tool'] == 'restart_app'
