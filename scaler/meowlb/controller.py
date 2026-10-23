"""Replica controller: a deterministic control loop that keeps N copies of a stateless Liara app behind the LB.

Each tick (every TICK seconds):
  1. signals  <- LB (rps over the last minute, p95, healthy backends) + Liara metrics (CPU/RAM of every instance)
  2. state    <- timers for sustained breach / sustained calm, accrued replica cost, deploy quota
  3. decision <- policy.decide() (pure, no LLM)
  4. action   <- create or remove ONE replica app, asynchronously; every step is audited

A replica is a separate Liara app `<app>-rN` built from the base app's *current release source* and env (Liara has no
native replicas). Scale-out: create app -> upload the source -> release -> wait READY -> register in the LB -> healthy.
Scale-in: drain (no new requests, wait for in-flight) -> deregister -> delete the app. The base app is never deleted and
the last healthy backend is never removed. Negahban's AI (or a human) can request a target with an override; the same
guards apply. With the LLM disabled nothing changes: the loop does not use one.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import deque

from . import obs
from .liara import Liara, LiaraError
from .lb import Pool
from .policy import Decision, Signals, State, decide, observe

TICK = 15.0
VERTICAL = {  # apps that must not get replicas, and why (shown in /scaler/status and /api/replicas)
    "bazargard-ai": "persistent disk + SQLite (catalogue and user memory): one writer; scales vertically",
    "negahban-sre": "SQLite state on its disk and a single orchestrator loop; scales vertically",
    "meowmeow-wallex": "single trading engine per account (two replicas would double orders); scales vertically",
}


class Controller:
    poll_s = 10.0       # release state polling
    health_poll_s = 2.0
    drain_poll_s = 0.5

    def __init__(self, liara: Liara | None, pool: Pool, policy: dict, now=time.time):
        self.li, self.pool, self.pol, self.now = liara, pool, policy, now
        self.app = policy["app"]
        self.state = State(quota_left=policy["deploy_quota_per_app"])
        self.audit: deque = deque(maxlen=500)
        self.last_decision: Decision | None = None
        self.task: asyncio.Task | None = None
        self.replica_names: list[str] = []
        self.cpu_mem: tuple[float | None, float | None] = (None, None)
        self.base_info: dict = {}
        self.day = int(self.now() // 86400)
        self.deploys_today: dict[str, int] = {}
        self.unhealthy_since: dict[str, float] = {}
        self.registry: list[dict] = []
        self._last_tick = self.now()
        self._metrics_at = 0.0
        self._billing_at = 0.0

    # -- audit ---------------------------------------------------------------------------------------
    def record(self, kind: str, actor: str, result: str, reason: str = "", **details) -> dict:
        entry = {"ts": round(self.now(), 3), "kind": kind, "actor": actor, "result": result, "reason": reason, **details}
        self.audit.append(entry)
        obs.log("scaler.audit", "warn" if result in ("failed", "rejected") else "info", f"{kind}: {result}",
                actor=actor, kind=kind, result=result, reason=reason, **details)
        return entry

    def url(self, name: str) -> str:
        return self.pol["replica_url"].format(name=name)

    # -- discovery -----------------------------------------------------------------------------------
    def replica_re(self) -> re.Pattern:
        return re.compile(rf"^{re.escape(self.app)}-r(\d+)$")

    async def bootstrap(self) -> None:
        """Adopt replica apps that already exist (the LB keeps no state of its own; Liara is the source of truth)."""
        base = await self.li.project(self.app)
        me = await self.li.me()
        plans = me["plans"]
        pol = self.pol
        self.base_info = {"network": (base.get("network") or {}).get("_id"), "plan": base.get("planID"),
                          "bundle": base.get("bundlePlanID")}
        self.plan_specs = plans["projects"]
        self.state.replica_hourly = float(plans["projects"][pol["replica_plan"]]["price"]) + float(
            plans["projectBundlePlans"][pol["replica_plan"]][pol["replica_bundle"]]["price"])
        for p in await self.li.projects():
            if self.replica_re().match(p["project_id"]):
                self.replica_names.append(p["project_id"])
                self.pool.add(p["project_id"], self.url(p["project_id"]), managed=True)
        self.replica_names.sort(key=lambda n: int(self.replica_re().match(n).group(1)))
        self.state.replicas = len(self.replica_names)
        self.record("bootstrap", "controller", "ok", f"adopted {len(self.replica_names)} existing replica(s)",
                    replicas=list(self.replica_names), replica_hourly_toman=self.state.replica_hourly)

    # -- signals -------------------------------------------------------------------------------------
    async def refresh_metrics(self) -> None:
        """Max CPU and RAM fraction over the base app and its replicas (Liara reports cores and bytes)."""
        cpu = mem = None
        try:  # the base app's plan can change (vertical resize): read it on every refresh
            self.base_info["plan"] = (await self.li.project(self.app)).get("planID") or self.base_info.get("plan")
        except LiaraError:
            pass
        for app, plan in [(self.app, self.base_info.get("plan"))] + [(n, self.pol["replica_plan"]) for n in self.replica_names]:
            try:
                m = await self.li.metrics(app)
            except LiaraError:
                continue
            spec = self.plan_specs.get(plan or "", {})
            cores, ram_gb = float(spec.get("CPU", {}).get("amount") or 0), float(spec.get("RAM", {}).get("amount") or 0)
            for s in m.get("cpuUsage", []):
                if cores:
                    cpu = max(cpu or 0.0, float(s["value"][1]) / cores)
            for s in m.get("memoryUsage", []):
                if ram_gb:
                    mem = max(mem or 0.0, float(s["value"][1]) / (ram_gb * 1024 ** 3))
        self.cpu_mem = (None if cpu is None else round(cpu, 3), None if mem is None else round(mem, 3))

    async def refresh_billing(self) -> None:
        """Balance and account run-rate: Liara refuses to create an app unless the balance covers one week of cost."""
        try:
            me = await self.li.me()
            bal = (me.get("team") or {}).get("balance")
            self.state.balance = None if bal is None else float(bal)
            rep = await self.li.req("GET", "/v1/usage-report", what="usage report")
            self.state.run_rate_hourly = float(rep.get("totalHourlyPrice") or 0)
        except (LiaraError, ValueError, TypeError):
            pass

    def signals(self) -> Signals:
        backends = [b for b in self.pool.backends.values() if not b.draining]
        return Signals(rps_1m=self.pool.rps_1m(), healthy_backends=max(0, len(self.pool.healthy())),
                       backends=len(backends), p95_ms=self.pool.p95_1m(), cpu=self.cpu_mem[0], mem=self.cpu_mem[1])

    def accrue(self) -> None:
        now = self.now()
        if int(now // 86400) != self.day:
            self.day, self.state.cost_today, self.deploys_today = int(now // 86400), 0.0, {}
        self.state.cost_today += self.state.replica_hourly * self.state.replicas * max(0.0, now - self._last_tick) / 3600
        self._last_tick = now

    def next_name(self) -> str:
        used = {int(self.replica_re().match(n).group(1)) for n in self.replica_names}
        i = 1
        while i in used:
            i += 1
        return f"{self.app}-r{i}"

    # -- the loop ------------------------------------------------------------------------------------
    async def tick(self) -> Decision:
        self.accrue()
        if self.now() - self._metrics_at >= 60:
            self._metrics_at = self.now()
            await self.refresh_metrics()
        if self.now() - self._billing_at >= 300:
            self._billing_at = self.now()
            await self.refresh_billing()
        sig = self.signals()
        name = self.next_name()
        self.state.quota_left = self.pol["deploy_quota_per_app"] - self.deploys_today.get(name, 0)
        self.state.pending = None if self.task is None or self.task.done() else self.state.pending
        observe(self.state, sig, self.pol, self.now())
        d = decide(sig, self.state, self.pol, self.now())
        if not self.last_decision or (d.action, re.sub(r"[0-9.,]+", "#", d.reason)) != (
                self.last_decision.action, re.sub(r"[0-9.,]+", "#", self.last_decision.reason)):
            obs.log("scaler.decision", msg=d.reason, action=d.action, target=d.target, signals=d.signals)
        self.last_decision = d
        self.watch_unhealthy()
        actor = "override" if d.reason.startswith("override") else "controller"
        if d.action == "scale_out":
            self.start(f"create {name}", self.scale_out(name, d.reason, actor))
        elif d.action == "scale_in" and self.replica_names:
            victim = self.replica_names[-1]
            self.start(f"remove {victim}", self.scale_in(victim, d.reason, actor))
        return d

    def start(self, label: str, coro) -> None:
        self.state.pending = label
        self.state.last_action_ts = self.now()
        self.task = asyncio.ensure_future(coro)

    def watch_unhealthy(self) -> None:
        """A replica that has been registered but never healthy for too long is reported (once)."""
        now = self.now()
        for n in self.replica_names:
            b = self.pool.backends.get(n)
            if b and not b.healthy:
                since = self.unhealthy_since.setdefault(n, now)
                if now - since > self.pol["max_replica_unhealthy_s"] and since > 0:
                    self.record("replica_unhealthy", "controller", "alert", f"{n} unhealthy for {int(now - since)} s", replica=n)
                    self.unhealthy_since[n] = -1  # report once
            else:
                self.unhealthy_since.pop(n, None)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - the loop must survive any single failure
                obs.log("scaler.tick_failed", "error", obs.redact(str(e))[:300], error_type=type(e).__name__)
            await asyncio.sleep(TICK)

    # -- actions -------------------------------------------------------------------------------------
    async def scale_out(self, name: str, reason: str, actor: str) -> None:
        self.state.pending = f"create {name}"
        t0 = self.now()
        self.record("scale_out", actor, "started", reason, replica=name)
        created = False
        try:
            rels = await self.li.releases(self.app, 5)
            cur = rels.get("currentRelease")
            rel = next((r for r in rels.get("releases", []) if r["_id"] == cur), None) or {}
            base = await self.li.project(self.app)
            envs = {e["key"]: e["value"] for e in base.get("envs", [])}
            await self.li.create_app(name, self.pol["replica_plan"], self.pol["replica_bundle"], self.base_info.get("network"))
            created = True
            self.replica_names.append(name)
            self.state.replicas = len(self.replica_names)
            archive = await self.li.source_bytes(cur)
            sid = await self.li.upload_source(name, archive)
            body = {"sourceID": sid, "type": "docker", "port": int(rel.get("port") or 8080), "envs": envs,
                    "message": f"replica of {self.app} release {rel.get('tag', cur)} (meow-lb controller)",
                    "build": {"location": rel.get("buildLocation") or "germany", "cache": True}}
            rid = await self.li.create_release(name, body)
            self.deploys_today[name] = self.deploys_today.get(name, 0) + 1
            state = await self.wait_release(rid, 900)
            if state != "READY":
                raise RuntimeError(f"release {rid} ended {state}")
            self.pool.add(name, self.url(name), managed=True)
            healthy = await self.wait_healthy(name, 300)
            if not healthy:
                raise RuntimeError(f"{name} READY but never passed the LB health check")
            self.record("scale_out", actor, "done", reason, replica=name, release=rid, seconds=round(self.now() - t0))
        except Exception as e:  # noqa: BLE001
            self.record("scale_out", actor, "failed", obs.redact(str(e))[:300], replica=name, seconds=round(self.now() - t0))
            if created:  # never leave a half-built replica billing
                await self.remove_app(name, "cleanup after failed scale-out", actor)
        finally:
            self.state.pending = None
            self.state.last_action_ts = self.now()
            self._billing_at = 0.0  # the account run-rate changed: re-read it before the next decision

    async def scale_in(self, name: str, reason: str, actor: str) -> None:
        self.state.pending = f"remove {name}"
        t0 = self.now()
        self.record("scale_in", actor, "started", reason, replica=name)
        try:
            others = [b for b in self.pool.healthy() if b.name != name]
            if not others:
                self.record("scale_in", actor, "rejected", "would remove the last healthy backend", replica=name)
                return
            b = self.pool.backends.get(name)
            if b:
                b.draining = True
                for _ in range(60):  # drain up to 30 s
                    if b.in_flight == 0:
                        break
                    await asyncio.sleep(self.drain_poll_s)
            await self.remove_app(name, reason, actor)
            self.record("scale_in", actor, "done", reason, replica=name, seconds=round(self.now() - t0))
        except Exception as e:  # noqa: BLE001
            self.record("scale_in", actor, "failed", obs.redact(str(e))[:300], replica=name)
        finally:
            self.state.pending = None
            self.state.last_action_ts = self.now()
            self._billing_at = 0.0

    async def remove_app(self, name: str, reason: str, actor: str) -> None:
        if not self.replica_re().match(name) or name == self.app:
            raise RuntimeError(f"refusing to delete non-replica app {name}")
        self.pool.remove(name)
        try:
            await self.li.delete_app(name)
        except LiaraError as e:
            if e.status != 404:
                raise
        if name in self.replica_names:
            self.replica_names.remove(name)
        self.state.replicas = len(self.replica_names)
        self.record("delete_app", actor, "done", reason, replica=name)

    async def wait_release(self, rid: str, timeout: float) -> str:
        t0 = self.now()
        while self.now() - t0 < timeout:
            st = (await self.li.release(rid)).get("state")
            if st in ("READY", "FAILED", "CANCELED", "TIMEDOUT", "UNHEALTHY"):
                return st
            await asyncio.sleep(self.poll_s)
        return "WATCH_TIMEOUT"

    async def wait_healthy(self, name: str, timeout: float) -> bool:
        t0 = self.now()
        while self.now() - t0 < timeout:
            b = self.pool.backends.get(name)
            if b and b.healthy:
                return True
            await asyncio.sleep(self.health_poll_s)
        return False

    # -- operator / AI interface ------------------------------------------------------------------------
    def override(self, body: dict) -> dict:
        actor = str(body.get("actor") or "human")[:40]
        action, reason = body.get("action"), str(body.get("reason") or "")[:300]
        now = self.now()
        minutes = max(1, min(240, int(body.get("minutes") or 30)))
        cur = self.state.instances
        if action == "hold":
            self.state.hold_until = now + minutes * 60
            res = {"accepted": True, "reason": f"automatic actions paused for {minutes} min"}
        elif action == "resume":
            self.state.hold_until, self.state.override_desired = 0.0, None
            res = {"accepted": True, "reason": "automatic policy resumed"}
        elif action in ("scale_out", "scale_in", "desired"):
            want = int(body.get("desired") or (cur + 1 if action == "scale_out" else cur - 1))
            clamped = max(self.pol["min"], min(self.pol["max"], want))
            self.state.override_desired, self.state.override_until = clamped, now + minutes * 60
            res = {"accepted": True, "applied_desired": clamped,
                   "reason": f"target {clamped} instances for {minutes} min" + (f" (clamped from {want})" if clamped != want else "")}
        elif action == "set_bounds":
            lo = int(body.get("min", self.pol["min"]))
            hi = int(body.get("max", self.pol["max"]))
            if not (1 <= lo <= hi <= self.pol.get("hard_max", 5)):
                res = {"accepted": False, "reason": f"bounds must satisfy 1 <= min <= max <= {self.pol.get('hard_max', 5)}"}
            else:
                self.pol["min"], self.pol["max"] = lo, hi
                res = {"accepted": True, "reason": f"bounds set to {lo}..{hi}"}
        else:
            res = {"accepted": False, "reason": "action must be scale_out, scale_in, desired, hold, resume or set_bounds"}
        self.record("override", actor, "accepted" if res["accepted"] else "rejected", f"{action}: {reason} -> {res['reason']}")
        return res

    def status(self) -> dict:
        now = self.now()
        sig = self.signals()
        s = self.state
        return {
            "generated_at": now, "app": self.app, "mode": "horizontal", "policy": self.pol,
            "instances": s.instances, "replicas": list(self.replica_names), "pending": s.pending,
            "signals": {"rps_1m": round(sig.rps_1m, 2), "rps_per_backend": round(sig.rps_per_backend, 2), "p95_ms": sig.p95_ms,
                        "cpu": sig.cpu, "mem": sig.mem, "healthy_backends": sig.healthy_backends},
            "timers": {"breach_out_for_s": None if s.breach_out_since is None else round(now - s.breach_out_since),
                       "calm_for_s": None if s.calm_since is None else round(now - s.calm_since)},
            "last_decision": self.last_decision.as_dict() if self.last_decision else None,
            "last_action_ts": s.last_action_ts or None,
            "hold_until": s.hold_until or None,
            "override": {"desired": s.override_desired, "until": s.override_until} if s.override_desired is not None and now < s.override_until else None,
            "cost": {"replica_hourly_toman": round(s.replica_hourly, 2), "today_toman": round(s.cost_today, 1),
                     "daily_budget_toman": self.pol["daily_budget_toman"],
                     "account_balance_toman": s.balance, "account_run_rate_hourly": s.run_rate_hourly,
                     "one_week_rule_need_toman": None if s.run_rate_hourly is None else round(168 * (s.run_rate_hourly + s.replica_hourly))},
            "deploys_today": self.deploys_today,
            "registry": [{"app": self.app, "mode": "horizontal", "reason": "stateless (no disk); replicas behind meow-lb"}]
                        + [{"app": a, "mode": "vertical", "reason": r} for a, r in VERTICAL.items()],
            "audit_tail": list(self.audit)[-20:],
            "llm": "not used: decisions are rule-based (policy.decide)",
        }
