import random
from negahban.diag import analytics as an


def test_ewma_flags_spike_not_noise():
    random.seed(1)
    s = [(i * 60, 100 + random.uniform(-2, 2)) for i in range(60)]
    assert an.ewma_anomalies(s) == []
    s[45] = (45 * 60, 180.0)
    a = an.ewma_anomalies(s)
    assert len(a) >= 1 and a[0]['ts'] == 45 * 60 and a[0]['direction'] == 'spike'


def test_forecast_detects_leak_and_eta():
    s = [(i * 300, 300 + i * 5 + (i % 3)) for i in range(48)]  # +60 MB/h over 4 h
    f = an.forecast_limit(s, 1024)
    assert f['verdict'] == 'leak_likely' and 55 < f['slope_per_hour'] < 65 and f['r2'] > 0.95
    assert 8 < f['hours_to_limit'] < 12 and f['eta']
    flat = [(i * 300, 300 + (i % 2) * 3) for i in range(48)]
    assert an.forecast_limit(flat, 1024)['verdict'] == 'stable'
    near = [(i * 300, 950 + (i % 2)) for i in range(20)]
    assert an.forecast_limit(near, 1024)['verdict'] == 'near_limit'
    assert an.forecast_limit([(0, 1)], 10)['verdict'] == 'insufficient_data'


def test_significance_helpers():
    assert an.two_proportion_p(2, 1000, 60, 1000) < 0.001
    assert an.two_proportion_p(10, 1000, 11, 1000) > 0.5
    assert an.welch_p([100] * 10 + [101] * 10, [300] * 10 + [301] * 10) < 0.001
    assert an.two_proportion_p(0, 0, 1, 1) is None


def test_compare_windows_verdicts():
    base = {'requests': 500, 'errors': 2, 'latencies': [100 + i % 7 for i in range(50)], 'tracebacks': 0}
    bad = {'requests': 500, 'errors': 40, 'latencies': [100 + i % 7 for i in range(50)], 'tracebacks': 9}
    assert an.compare_windows(base, bad)['verdict'] == 'regression'
    slow = {'requests': 500, 'errors': 2, 'latencies': [400 + i % 7 for i in range(50)], 'tracebacks': 0}
    assert an.compare_windows(base, slow)['verdict'] == 'slower'
    assert an.compare_windows(bad, base)['verdict'] == 'improved'
    assert an.compare_windows(base, dict(base))['verdict'] == 'no_significant_change'


def test_slo_and_burn():
    now = 1_800_000_000
    samples = [{'ts': now - (600 - i) * 60, 'ok': not (300 <= i < 310), 'latency_ms': 120, 'status': 200 if not (300 <= i < 310) else 502} for i in range(600)]
    r = an.slo(samples, 99.5)
    assert r['samples'] == 600 and r['availability_pct'] == 98.333 and not r['met']
    assert r['error_budget_used_pct'] == 333.3 and r['outages'][0]['minutes'] == 10.0 and r['outages'][0]['statuses'] == {'502': 10}
    assert r['burn_rate_1h'] == 0.0 and r['latency_p95_ms'] == 120
    assert an.slo([]) == {'samples': 0}
    b = an.burn(240000, 3000, [(0, 300000), (3600, 296000), (7200, 292000)], budget_month=1_000_000)
    assert b['hours_left'] == 80.0 and b['observed_toman_per_hour'] == 4000.0 and b['over_budget'] and b['budget_use_pct'] == 216.0


def test_deploy_windows_from_logs():
    t = 10_000
    rows = [{'ts': t - 600 + i, 'line': f'INFO:     1.1.1.1:1 - "GET /a HTTP/1.1" 200 OK'} for i in range(200)]
    rows += [{'ts': t + i, 'line': f'INFO:     1.1.1.1:1 - "GET /a HTTP/1.1" {500 if i % 4 == 0 else 200} OK'} for i in range(200)]
    before, after = an._windows(rows, t, 30)
    assert before['requests'] == 200 and before['errors'] == 0 and after['errors'] == 50
    assert an.compare_windows(before, after)['verdict'] == 'regression'
