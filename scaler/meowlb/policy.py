"""Deterministic scaling policy: pure functions, no I/O, no LLM.

decide(signals, state, policy, now) -> Decision is the whole brain of the controller. It is a small rule set with
hysteresis, sustained-breach windows, cooldowns, bounds, a daily budget and deploy-quota awareness, so the same
inputs always give the same answer and every answer carries a human-readable reason (written to the audit log).
The AI (Negahban) may only *request* a target through an override; the request goes through the same guards.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

DEFAULT_POLICY = {
    "app": "demo-shop",               # the stateless app that is scaled horizontally
    "enabled": True,
    "min": 1,                         # instances = the base app + replicas
    "max": 3,
    "replica_plan": "small-g2",
    "replica_bundle": "basic",
    "health_path": "/healthz",
    "scale_out": {"rps_per_backend": 8.0, "p95_ms": 1500, "cpu": 0.75, "mem": 0.85, "sustain_s": 60},
    "scale_in": {"rps_per_backend": 2.0, "p95_ms": 600, "cpu": 0.30, "mem": 0.60, "sustain_s": 180},
    "cooldown_out_s": 120,            # after any action, wait before the next scale-out
    "cooldown_in_s": 180,             # after any action, wait before the next scale-in
    "daily_budget_toman": 30000,      # replica spend cap per UTC day (hourly billing)
    "min_billed_hours": 1,            # a new replica is assumed to cost at least this many hours
    "deploy_quota_per_app": 20,       # basic bundle: 20 deploys/day per app (each replica is its own app)
    "max_replica_unhealthy_s": 600,   # a replica that never turns healthy is reported after this
    "hard_max": 5,                    # ceiling for set_bounds overrides
    "replica_url": "https://{name}.liara.run",
}


@dataclass
class Signals:
    rps_1m: float = 0.0
    healthy_backends: int = 1
    backends: int = 1
    p95_ms: float | None = None
    cpu: float | None = None          # max fraction of the plan's CPU across instances (0..1+)
    mem: float | None = None          # max fraction of the plan's RAM across instances

    @property
    def rps_per_backend(self) -> float:
        return self.rps_1m / max(1, self.healthy_backends)


@dataclass
class State:
    replicas: int = 0                 # replica apps that exist (ready or not)
    pending: str | None = None        # operation in flight ("create demo-shop-r1" ...)
    last_action_ts: float = 0.0
    breach_out_since: float | None = None
    calm_since: float | None = None
    hold_until: float = 0.0
    override_desired: int | None = None
    override_until: float = 0.0
    cost_today: float = 0.0           # toman spent on replicas today (accrued hourly price)
    replica_hourly: float = 0.0       # toman/hour of one replica (plan + bundle)
    quota_left: int = 20              # deploys left today for the next replica app
    balance: float | None = None      # account balance (toman); Liara refuses new apps unless it covers
    run_rate_hourly: float | None = None  # ... one week of the whole account's run-rate including the new app

    @property
    def instances(self) -> int:
        return 1 + self.replicas


@dataclass
class Decision:
    action: str                       # scale_out | scale_in | none
    reason: str
    target: int
    signals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def merge_policy(base: dict, override: dict | None) -> dict:
    out = {**base}
    for k, v in (override or {}).items():
        out[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _gt(v, limit) -> bool:
    return v is not None and limit is not None and v > limit


def _lt_or_none(v, limit) -> bool:
    return v is None or limit is None or v < limit


def breaching(sig: Signals, pol: dict) -> list[str]:
    o = pol["scale_out"]
    why = []
    if _gt(sig.rps_per_backend, o["rps_per_backend"]):
        why.append(f"rps/backend {sig.rps_per_backend:.1f} > {o['rps_per_backend']}")
    if _gt(sig.p95_ms, o["p95_ms"]):
        why.append(f"p95 {sig.p95_ms:.0f} ms > {o['p95_ms']}")
    if _gt(sig.cpu, o["cpu"]):
        why.append(f"cpu {sig.cpu:.0%} > {o['cpu']:.0%}")
    if _gt(sig.mem, o["mem"]):
        why.append(f"mem {sig.mem:.0%} > {o['mem']:.0%}")
    return why


def calm(sig: Signals, pol: dict) -> bool:
    i = pol["scale_in"]
    return (sig.rps_per_backend < i["rps_per_backend"] and _lt_or_none(sig.p95_ms, i["p95_ms"])
            and _lt_or_none(sig.cpu, i["cpu"]) and _lt_or_none(sig.mem, i["mem"]))


def observe(state: State, sig: Signals, pol: dict, now: float) -> State:
    """Update the sustained-breach / sustained-calm timers (hysteresis: the two bands do not overlap)."""
    if breaching(sig, pol):
        if state.breach_out_since is None:
            state.breach_out_since = now
        state.calm_since = None
    elif calm(sig, pol):
        if state.calm_since is None:
            state.calm_since = now
        state.breach_out_since = None
    else:                             # in the dead band between the two thresholds: reset both
        state.breach_out_since = None
        state.calm_since = None
    return state


def _can_out(state: State, pol: dict, now: float) -> str | None:
    if state.instances >= pol["max"]:
        return f"at max ({pol['max']} instances)"
    if state.last_action_ts and now - state.last_action_ts < pol["cooldown_out_s"]:
        return f"cooldown ({int(pol['cooldown_out_s'] - (now - state.last_action_ts))} s left)"
    projected = state.cost_today + state.replica_hourly * pol["min_billed_hours"]
    if state.replica_hourly and projected > pol["daily_budget_toman"]:
        return f"daily budget: {projected:,.0f} > {pol['daily_budget_toman']:,} toman"
    if state.quota_left <= 0:
        return "deploy quota for today is used up"
    if state.balance is not None and state.run_rate_hourly is not None:
        need = 168 * (state.run_rate_hourly + state.replica_hourly)
        if state.balance < need:
            return f"Liara one-week balance rule: needs {need:,.0f} toman, balance {state.balance:,.0f}"
    return None


def _can_in(state: State, sig: Signals, pol: dict, now: float) -> str | None:
    if state.replicas <= 0 or state.instances <= pol["min"]:
        return f"at min ({pol['min']} instance{'s' if pol['min'] > 1 else ''})"
    if state.last_action_ts and now - state.last_action_ts < pol["cooldown_in_s"]:
        return f"cooldown ({int(pol['cooldown_in_s'] - (now - state.last_action_ts))} s left)"
    if sig.healthy_backends <= 1:
        return "only one healthy backend: never remove the last one"
    return None


def decide(sig: Signals, state: State, pol: dict, now: float) -> Decision:
    snap = {"rps_1m": round(sig.rps_1m, 2), "rps_per_backend": round(sig.rps_per_backend, 2), "p95_ms": sig.p95_ms,
            "cpu": sig.cpu, "mem": sig.mem, "healthy": sig.healthy_backends, "instances": state.instances}
    cur = state.instances
    if not pol.get("enabled", True):
        return Decision("none", "controller disabled by policy", cur, snap)
    if state.pending:
        return Decision("none", f"waiting for {state.pending}", cur, snap)

    # 1. an operator/AI override (time-bounded) sets the target; guards still apply
    if state.override_desired is not None and now < state.override_until:
        want = max(pol["min"], min(pol["max"], state.override_desired))
        if want > cur:
            blocked = _can_out(state, {**pol, "cooldown_out_s": 0}, now)
            return Decision("none", f"override wants {want}, blocked: {blocked}", cur, snap) if blocked else \
                Decision("scale_out", f"override: desired {want} instances", cur + 1, snap)
        if want < cur:
            blocked = _can_in(state, sig, {**pol, "cooldown_in_s": 0}, now)
            return Decision("none", f"override wants {want}, blocked: {blocked}", cur, snap) if blocked else \
                Decision("scale_in", f"override: desired {want} instances", cur - 1, snap)
        return Decision("none", f"override holds {want} instances", cur, snap)
    if now < state.hold_until:
        return Decision("none", f"hold for {int(state.hold_until - now)} s (operator request)", cur, snap)

    # 2. below the configured minimum (e.g. min raised): repair upwards
    if cur < pol["min"]:
        blocked = _can_out(state, {**pol, "cooldown_out_s": 0}, now)
        return Decision("none", f"below min, blocked: {blocked}", cur, snap) if blocked else \
            Decision("scale_out", f"below min {pol['min']}", cur + 1, snap)

    # 3. sustained load -> scale out one step
    why = breaching(sig, pol)
    if why and state.breach_out_since is not None and now - state.breach_out_since >= pol["scale_out"]["sustain_s"]:
        blocked = _can_out(state, pol, now)
        if blocked:
            return Decision("none", f"load high ({'; '.join(why)}) but {blocked}", cur, snap)
        return Decision("scale_out", f"sustained {int(now - state.breach_out_since)} s: {'; '.join(why)}", cur + 1, snap)
    if why:
        return Decision("none", f"load high ({'; '.join(why)}), waiting for {pol['scale_out']['sustain_s']} s sustain", cur, snap)

    # 4. sustained calm -> scale in one step
    if state.calm_since is not None and now - state.calm_since >= pol["scale_in"]["sustain_s"] and state.replicas > 0:
        blocked = _can_in(state, sig, pol, now)
        if blocked:
            return Decision("none", f"calm but {blocked}", cur, snap)
        return Decision("scale_in", f"calm for {int(now - state.calm_since)} s "
                                    f"(rps/backend {sig.rps_per_backend:.1f} < {pol['scale_in']['rps_per_backend']})", cur - 1, snap)
    return Decision("none", "within band", cur, snap)


def hours_left_today(now: float) -> float:
    return (86400 - now % 86400) / 3600


def percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))]
