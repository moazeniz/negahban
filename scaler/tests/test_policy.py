"""Simulated metrics -> decisions. The policy is pure, so every scenario is deterministic."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meowlb.policy import DEFAULT_POLICY, Signals, State, decide, merge_policy, observe  # noqa: E402

POL = merge_policy(DEFAULT_POLICY, {"min": 1, "max": 3, "scale_out": {"sustain_s": 60}, "scale_in": {"sustain_s": 180},
                                    "cooldown_out_s": 120, "cooldown_in_s": 180, "daily_budget_toman": 30000})


def state(**kw):
    s = State(replica_hourly=1041.66, quota_left=20)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def step(st, sig, t, pol=POL):
    observe(st, sig, pol, t)
    return decide(sig, st, pol, t)


class TestDecisions(unittest.TestCase):
    def test_short_spike_does_not_scale(self):
        st = state()
        for t in range(0, 50, 10):
            d = step(st, Signals(rps_1m=20, healthy_backends=1), 1000 + t)
        self.assertEqual(d.action, "none")
        self.assertIn("waiting for 60 s sustain", d.reason)

    def test_sustained_load_scales_out_once_then_cooldown(self):
        st = state()
        for t in range(0, 70, 10):
            d = step(st, Signals(rps_1m=20, healthy_backends=1), 1000 + t)
        self.assertEqual((d.action, d.target), ("scale_out", 2))
        st.last_action_ts, st.replicas = 1060, 1
        d = step(st, Signals(rps_1m=40, healthy_backends=2), 1100)
        self.assertEqual(d.action, "none")
        self.assertIn("cooldown", d.reason)

    def test_each_signal_can_trigger(self):
        for sig in (Signals(p95_ms=2500), Signals(cpu=0.9), Signals(mem=0.95)):
            st = state()
            step(st, sig, 0)
            d = step(st, sig, 61)
            self.assertEqual(d.action, "scale_out", sig)

    def test_max_bound(self):
        st = state(replicas=2)  # 3 instances = max
        step(st, Signals(rps_1m=100, healthy_backends=3), 0)
        d = step(st, Signals(rps_1m=100, healthy_backends=3), 100)
        self.assertEqual(d.action, "none")
        self.assertIn("at max", d.reason)

    def test_budget_and_quota_block_scale_out(self):
        st = state(cost_today=29500)
        step(st, Signals(rps_1m=50), 0)
        d = step(st, Signals(rps_1m=50), 100)
        self.assertIn("daily budget", d.reason)
        st = state(quota_left=0)
        step(st, Signals(rps_1m=50), 0)
        self.assertIn("quota", step(st, Signals(rps_1m=50), 100).reason)

    def test_liara_one_week_balance_rule(self):
        st = state(balance=1_817_355, run_rate_hourly=10_405)
        step(st, Signals(rps_1m=50), 0)
        d = step(st, Signals(rps_1m=50), 100)
        self.assertEqual(d.action, "none")
        self.assertIn("one-week balance rule", d.reason)
        st = state(balance=1_900_000, run_rate_hourly=9_000)
        step(st, Signals(rps_1m=50), 0)
        self.assertEqual(step(st, Signals(rps_1m=50), 100).action, "scale_out")

    def test_sustained_calm_scales_in(self):
        st = state(replicas=1, last_action_ts=0)
        for t in range(0, 190, 10):
            d = step(st, Signals(rps_1m=1, healthy_backends=2, p95_ms=200), 1000 + t)
        self.assertEqual((d.action, d.target), ("scale_in", 1))

    def test_never_removes_last_healthy_backend(self):
        st = state(replicas=1)
        step(st, Signals(rps_1m=0, healthy_backends=1), 1000)
        d = step(st, Signals(rps_1m=0, healthy_backends=1), 1300)
        self.assertEqual(d.action, "none")
        self.assertIn("never remove the last one", d.reason)

    def test_dead_band_resets_timers(self):
        st = state(replicas=1)
        step(st, Signals(rps_1m=2, healthy_backends=2), 0)       # 1 rps/backend: calm
        step(st, Signals(rps_1m=10, healthy_backends=2), 100)    # 5 rps/backend: between bands
        self.assertIsNone(st.calm_since)
        self.assertIsNone(st.breach_out_since)
        self.assertEqual(step(st, Signals(rps_1m=2, healthy_backends=2), 150).action, "none")

    def test_override_is_clamped_and_guarded(self):
        st = state(override_desired=10, override_until=10_000)
        d = step(st, Signals(rps_1m=0), 100)
        self.assertEqual((d.action, d.target), ("scale_out", 2))
        st = state(replicas=1, override_desired=1, override_until=10_000)
        d = step(st, Signals(rps_1m=50, healthy_backends=1), 100)   # only one healthy: must not remove it
        self.assertEqual(d.action, "none")
        self.assertIn("blocked", d.reason)

    def test_hold_and_disabled(self):
        st = state(hold_until=500)
        step(st, Signals(rps_1m=50), 0)
        self.assertIn("hold", step(st, Signals(rps_1m=50), 100).reason)
        self.assertEqual(decide(Signals(rps_1m=50), state(), {**POL, "enabled": False}, 0).action, "none")

    def test_pending_operation_blocks(self):
        d = decide(Signals(rps_1m=100), state(pending="create demo-shop-r1"), POL, 0)
        self.assertIn("waiting for create", d.reason)


class TestSimulation(unittest.TestCase):
    """A 40-minute load profile: idle -> ramp to 40 rps -> plateau -> drop. The instance count must follow
    1 -> 2 -> 3 and back to 1, one step at a time, respecting sustain windows and cooldowns."""

    def test_trajectory(self):
        st, inst, events = state(), 1, []
        for t in range(0, 2400, 15):
            minute = t / 60
            rps = 2 if minute < 5 else (40 if minute < 20 else 1)
            healthy = inst
            sig = Signals(rps_1m=rps, healthy_backends=healthy, p95_ms=300)
            d = step(st, sig, t)
            if d.action == "scale_out":
                inst += 1
                st.replicas, st.last_action_ts = inst - 1, t
                events.append((round(minute, 2), "out", inst))
            elif d.action == "scale_in":
                inst -= 1
                st.replicas, st.last_action_ts = inst - 1, t
                events.append((round(minute, 2), "in", inst))
        self.assertEqual([e[2] for e in events], [2, 3, 2, 1])
        out1, out2, in1, in2 = (e[0] for e in events)
        self.assertGreaterEqual(out1, 6)          # 5 min idle + 60 s sustain
        self.assertGreaterEqual(out2 - out1, 2)   # cooldown_out 120 s
        self.assertGreaterEqual(in1, 23)          # load stops at 20 min + 180 s calm
        self.assertGreaterEqual(in2 - in1, 3)     # cooldown_in 180 s


if __name__ == "__main__":
    unittest.main()
