"""Deterministic scaling policy: pure functions, no I/O, no LLM.

decide(signals, state, policy, now) -> Decision is the whole brain of the controller. It is a small rule set with
hysteresis, sustained-breach windows, cooldowns, bounds, a daily budget and deploy-quota awareness, so the same
inputs always give the same answer and every answer carries a human-readable reason (written to the audit log).
The AI (Negahban) may only *request* a target through an override; the request goes through the same guards.

Vertical fallback: when sustained load calls for a replica but a replica is not possible (Liara's one-week balance
rule, a Liara refusal, the daily budget, the deploy quota or the max bound), the policy resizes the base app one step
up its allowed plan ladder instead (scale_up), and steps it back down (scale_down) after sustained calm. It only
scales down what it scaled up itself.
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
    # vertical fallback for the base app (plan ladder, lowest first); down_after_s = calm needed before stepping down.
    # Off by default: where Negahban's autopilot owns vertical resizes of the app, exactly one actor may resize it.
    "vertical": {"enabled": False, "plans": ["small-g2", "medium-g2"], "down_after_s": 1800, "cooldown_s": 600,
                 "refused_backoff_s": 900, "verify_timeout_s": 240},
}

# Temporary demo timings (POST /scaler/override {"action": "demo_mode", "minutes": N}); they revert by themselves.
DEMO_OVERRIDES = {"scale_out": {"sustain_s": 45}, "scale_in": {"sustain_s": 60}, "cooldown_out_s": 60, "cooldown_in_s": 60,
                  "vertical": {"down_after_s": 60, "cooldown_s": 60}}

FA = {  # Persian reasons shown on Negahban's scaling card
    "max": "سقف تعداد نمونه‌ها پر است",
    "budget": "سقف بودجه‌ی روزانه‌ی رپلیکا اجازه نمی‌دهد",
    "quota": "سهمیه‌ی استقرار روزانه تمام شده است",
    "balance": "قانون موجودی یک‌هفته‌ای لیارا: ساخت رپلیکا ممکن نیست",
    "refused": "لیارا ساخت رپلیکا را رد کرد (قانون موجودی یک‌هفته‌ای لیارا)",
    "cooldown": "دوره‌ی خنک‌سازی",
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
    base_plan: str | None = None      # current Liara plan of the base app
    vertical_from: str | None = None  # plan the controller scaled up from (it only scales down what it scaled up)
    out_refused_until: float = 0.0    # Liara answered 429 to a replica create: treat replicas as blocked until then
    up_refused_until: float = 0.0     # Liara refused a resize: no new resize attempt until then
    demo_until: float = 0.0           # demo timings active until (DEMO_OVERRIDES)
    dry_until: float = 0.0            # dry run until: decisions are audited, nothing is created or resized

    @property
    def instances(self) -> int:
        return 1 + self.replicas


@dataclass
class Decision:
    action: str                       # scale_out | scale_in | none
    reason: str
    target: int
    signals: dict = field(default_factory=dict)
    plan: str | None = None           # scale_up / scale_down: the target plan of the base app
    blocked: str | None = None        # why a replica was not possible: max|budget|quota|balance|refused|cooldown
    reason_fa: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def merge_policy(base: dict, override: dict | None) -> dict:
    out = {**base}
    for k, v in (override or {}).items():
        out[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def effective_policy(pol: dict, state: State, now: float) -> dict:
    """The policy in force right now: demo timings while demo mode is on."""
    return merge_policy(pol, DEMO_OVERRIDES) if now < state.demo_until else pol


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


def breaching_fa(sig: Signals, pol: dict) -> str:
    """The same triggers as breaching(), worded in Persian for the scaling card."""
    o, why = pol["scale_out"], []
    if _gt(sig.rps_per_backend, o["rps_per_backend"]):
        why.append(f"{sig.rps_per_backend:.1f} درخواست در ثانیه برای هر نمونه (آستانه {o['rps_per_backend']:g})")
    if _gt(sig.p95_ms, o["p95_ms"]):
        why.append(f"p95 برابر {sig.p95_ms:.0f} میلی‌ثانیه (آستانه {o['p95_ms']})")
    if _gt(sig.cpu, o["cpu"]):
        why.append(f"CPU {sig.cpu:.0%} (آستانه {o['cpu']:.0%})")
    if _gt(sig.mem, o["mem"]):
        why.append(f"حافظه {sig.mem:.0%} (آستانه {o['mem']:.0%})")
    return "، ".join(why)


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


def _can_out(state: State, pol: dict, now: float) -> tuple[str, str] | None:
    if state.instances >= pol["max"]:
        return "max", f"at max ({pol['max']} instances)"
    if state.last_action_ts and now - state.last_action_ts < pol["cooldown_out_s"]:
        return "cooldown", f"cooldown ({int(pol['cooldown_out_s'] - (now - state.last_action_ts))} s left)"
    projected = state.cost_today + state.replica_hourly * pol["min_billed_hours"]
    if state.replica_hourly and projected > pol["daily_budget_toman"]:
        return "budget", f"daily budget: {projected:,.0f} > {pol['daily_budget_toman']:,} toman"
    if state.quota_left <= 0:
        return "quota", "deploy quota for today is used up"
    if now < state.out_refused_until:
        return "refused", "Liara refused the last replica (one-week balance rule)"
    if state.balance is not None and state.run_rate_hourly is not None:
        need = 168 * (state.run_rate_hourly + state.replica_hourly)
        if state.balance < need:
            return "balance", f"Liara one-week balance rule: needs {need:,.0f} toman, balance {state.balance:,.0f}"
    return None


def _fa_block(code: str, state: State) -> str:
    if code == "balance" and state.balance is not None and state.run_rate_hourly is not None:
        need = 168 * (state.run_rate_hourly + state.replica_hourly)
        return f"{FA['balance']} (نیاز {need:,.0f} تومان، موجودی {state.balance:,.0f} تومان)"
    return FA.get(code, code)


def _next_plan(state: State, pol: dict, step: int) -> str | None:
    plans = (pol.get("vertical") or {}).get("plans") or []
    if state.base_plan not in plans:
        return None
    i = plans.index(state.base_plan) + step
    return plans[i] if 0 <= i < len(plans) else None


def _can_up(state: State, pol: dict, now: float) -> str | None:
    v = pol.get("vertical") or {}
    if not v.get("enabled"):
        return "vertical fallback is off (vertical resizes are owned by Negahban's autopilot)"
    if state.base_plan not in (v.get("plans") or []):
        return f"base plan {state.base_plan} is outside the allowed ladder {v.get('plans')}"
    if _next_plan(state, pol, +1) is None:
        return f"already on the largest allowed plan ({state.base_plan})"
    if now < state.up_refused_until:
        return f"Liara refused the last resize; next try in {int(state.up_refused_until - now)} s"
    if state.last_action_ts and now - state.last_action_ts < v.get("cooldown_s", 600):
        return f"vertical cooldown ({int(v.get('cooldown_s', 600) - (now - state.last_action_ts))} s left)"
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
            "cpu": sig.cpu, "mem": sig.mem, "healthy": sig.healthy_backends, "instances": state.instances,
            "plan": state.base_plan}
    cur = state.instances
    app = pol.get("app", "app")
    if not pol.get("enabled", True):
        return Decision("none", "controller disabled by policy", cur, snap, reason_fa="کنترلر خاموش است")
    if state.pending:
        return Decision("none", f"waiting for {state.pending}", cur, snap, reason_fa=f"در حال اجرا: {state.pending}")

    # 1. an operator/AI override (time-bounded) sets the target; guards still apply
    if state.override_desired is not None and now < state.override_until:
        want = max(pol["min"], min(pol["max"], state.override_desired))
        if want > cur:
            blocked = _can_out(state, {**pol, "cooldown_out_s": 0}, now)
            return Decision("none", f"override wants {want}, blocked: {blocked[1]}", cur, snap, blocked=blocked[0],
                            reason_fa=f"درخواست دستی {want} نمونه؛ {_fa_block(blocked[0], state)}") if blocked else \
                Decision("scale_out", f"override: desired {want} instances", cur + 1, snap, reason_fa=f"درخواست دستی: {want} نمونه")
        if want < cur:
            blocked = _can_in(state, sig, {**pol, "cooldown_in_s": 0}, now)
            return Decision("none", f"override wants {want}, blocked: {blocked}", cur, snap) if blocked else \
                Decision("scale_in", f"override: desired {want} instances", cur - 1, snap, reason_fa=f"درخواست دستی: {want} نمونه")
        return Decision("none", f"override holds {want} instances", cur, snap)
    if now < state.hold_until:
        return Decision("none", f"hold for {int(state.hold_until - now)} s (operator request)", cur, snap,
                        reason_fa="اقدام خودکار موقتاً متوقف است")

    # 2. below the configured minimum (e.g. min raised): repair upwards
    if cur < pol["min"]:
        blocked = _can_out(state, {**pol, "cooldown_out_s": 0}, now)
        return Decision("none", f"below min, blocked: {blocked[1]}", cur, snap, blocked=blocked[0]) if blocked else \
            Decision("scale_out", f"below min {pol['min']}", cur + 1, snap)

    # 3. sustained load -> scale out one step; if a replica is not possible -> vertical fallback (one plan up)
    why = breaching(sig, pol)
    if why and state.breach_out_since is not None and now - state.breach_out_since >= pol["scale_out"]["sustain_s"]:
        held = int(now - state.breach_out_since)
        blocked = _can_out(state, pol, now)
        if not blocked:
            return Decision("scale_out", f"sustained {held} s: {'; '.join(why)}", cur + 1, snap,
                            reason_fa=f"بار بالا به مدت {held} ثانیه: {breaching_fa(sig, pol)} ← افزودن یک رپلیکا ({cur + 1} نمونه)")
        code, msg = blocked
        if code == "cooldown":
            return Decision("none", f"load high ({'; '.join(why)}) but {msg}", cur, snap, blocked=code,
                            reason_fa=f"بار بالا؛ {FA['cooldown']}")
        up = _can_up(state, pol, now)
        if up is None:
            nxt = _next_plan(state, pol, +1)
            return Decision("scale_up", f"sustained {held} s: {'; '.join(why)}; replica not possible ({msg}) -> vertical fallback: "
                                        f"resize {app} {state.base_plan} -> {nxt}", cur, snap, plan=nxt, blocked=code,
                            reason_fa=f"{_fa_block(code, state)} ← جایگزین: مقیاس عمودی {app} از {state.base_plan} به {nxt}")
        return Decision("none", f"load high ({'; '.join(why)}) but {msg}; vertical: {up}", cur, snap, blocked=code,
                        reason_fa=f"بار بالا؛ {_fa_block(code, state)}" + (
                            f"؛ {app} روی بزرگ‌ترین پلن مجاز ({state.base_plan}) است" if "largest" in up else
                            "؛ مقیاس عمودی با خودران نگهبان است" if "Negahban" in up else ""))
    if why:
        return Decision("none", f"load high ({'; '.join(why)}), waiting for {pol['scale_out']['sustain_s']} s sustain", cur, snap,
                        reason_fa=f"بار بالا: {breaching_fa(sig, pol)}؛ انتظار برای پایداری {pol['scale_out']['sustain_s']} ثانیه")

    # 4. sustained calm -> scale in one step (replicas first), then step a vertical fallback back down
    calm_for = None if state.calm_since is None else now - state.calm_since
    if calm_for is not None and calm_for >= pol["scale_in"]["sustain_s"] and state.replicas > 0:
        blocked = _can_in(state, sig, pol, now)
        if blocked:
            return Decision("none", f"calm but {blocked}", cur, snap)
        return Decision("scale_in", f"calm for {int(calm_for)} s "
                                    f"(rps/backend {sig.rps_per_backend:.1f} < {pol['scale_in']['rps_per_backend']})", cur - 1, snap,
                        reason_fa=f"آرامش پایدار به مدت {int(calm_for)} ثانیه ({sig.rps_per_backend:.1f} درخواست در ثانیه برای هر نمونه، "
                                  f"آستانه {pol['scale_in']['rps_per_backend']:g}) ← حذف یک رپلیکا ({cur - 1} نمونه)")
    v = pol.get("vertical") or {}
    if (state.replicas == 0 and state.vertical_from and state.base_plan and state.base_plan != state.vertical_from
            and calm_for is not None):
        if calm_for < v.get("down_after_s", 1800):
            return Decision("none", f"calm for {int(calm_for)} s; back to {state.vertical_from} after {v.get('down_after_s', 1800)} s",
                            cur, snap, reason_fa=f"آرامش {int(calm_for)} ثانیه؛ بازگشت به {state.vertical_from} پس از {v.get('down_after_s', 1800)} ثانیه")
        if state.last_action_ts and now - state.last_action_ts < v.get("cooldown_s", 600):
            return Decision("none", "calm, vertical cooldown", cur, snap, reason_fa="آرامش؛ دوره‌ی خنک‌سازی")
        return Decision("scale_down", f"calm for {int(calm_for)} s: resize {app} {state.base_plan} -> {state.vertical_from}",
                        cur, snap, plan=state.vertical_from,
                        reason_fa=f"آرامش پایدار به مدت {int(calm_for)} ثانیه ← بازگشت {app} از {state.base_plan} به {state.vertical_from}")
    return Decision("none", "within band", cur, snap, reason_fa="در محدوده‌ی عادی")


def hours_left_today(now: float) -> float:
    return (86400 - now % 86400) / 3600


def percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))]
