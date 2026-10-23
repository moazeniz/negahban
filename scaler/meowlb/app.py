"""meow-lb: load balancer + deterministic replica controller (one aiohttp process).

Public, read-only (no secrets):   GET /lb/status · GET /scaler/status · GET /scaler/audit · GET /lb/healthz
Admin (Bearer SCALER_ADMIN_TOKEN): POST /scaler/override · POST /lb/backends
Negahban compatibility:            GET /api/replicas (Bearer SCALER_READ_TOKEN or admin) ·
                                   POST /api/replicas/{app}/override {desired, reason, ttl_s} (admin)
Everything else is proxied to the healthy backends.

Environment:
  LB_BACKENDS        "name=https://url,name2=https://url2" static backends (the base app)
  LB_STRATEGY        least_conn (default) | round_robin;  LB_STICKY=1 enables cookie stickiness
  SCALER_POLICY      JSON merged over policy.DEFAULT_POLICY;  SCALER_ENABLED=0 runs the LB only
  LIARA_API_TOKEN / LIARA_TEAM_ID / LIARA_API_BASE  for the controller
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time

import aiohttp
from aiohttp import web

from . import obs
from .controller import VERTICAL, Controller
from .lb import Pool
from .liara import Liara
from .policy import DEFAULT_POLICY, merge_policy


def authorized(req: web.Request, *names: str) -> bool:
    got = req.headers.get("Authorization", "")
    for n in names:
        tok = os.getenv(n, "")
        if tok and secrets.compare_digest(got, "Bearer " + tok):
            return True
    return False


def build_app(pool: Pool | None = None, controller: Controller | None = None, start_background: bool = True) -> web.Application:
    policy = merge_policy(DEFAULT_POLICY, json.loads(os.getenv("SCALER_POLICY") or "{}"))
    pool = pool or Pool(health_path=policy["health_path"], strategy=os.getenv("LB_STRATEGY", "least_conn"),
                        sticky=os.getenv("LB_STICKY") == "1")
    for item in filter(None, (os.getenv("LB_BACKENDS") or "").split(",")):
        name, _, url = item.partition("=")
        pool.add(name.strip(), url.strip())
    app = web.Application(client_max_size=10 * 1024 * 1024)
    ctx = {"pool": pool, "controller": controller, "policy": policy}   # shared state of this app instance

    async def on_start(app):
        ctx["session"] = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=200, ttl_dns_cache=60))
        pool.session = ctx["session"]
        if not start_background:
            return
        ctx["health"] = asyncio.create_task(pool.health_loop())
        if ctx["controller"] is None and os.getenv("SCALER_ENABLED", "1") != "0" and os.getenv("LIARA_API_TOKEN"):
            ctx["controller"] = Controller(Liara(ctx["session"]), pool, policy)
        if ctx["controller"] is not None:
            async def boot_and_run():
                for i in range(10):
                    try:
                        await ctx["controller"].bootstrap()
                        break
                    except Exception as e:  # noqa: BLE001
                        obs.log("scaler.bootstrap_failed", "error", obs.redact(str(e))[:300], attempt=i + 1)
                        await asyncio.sleep(15)
                await ctx["controller"].run()
            ctx["loop"] = asyncio.create_task(boot_and_run())
        obs.log("app.start", msg="meow-lb ready", backends=list(pool.backends), controller=ctx["controller"] is not None)

    async def on_stop(app):
        for k in ("health", "loop"):
            if ctx.get(k):
                app[k].cancel()
        await ctx["session"].close()
        obs.log("app.stop", msg="meow-lb stopping")

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)

    async def lb_status(req):
        return web.json_response(pool.status(), headers={"Cache-Control": "no-store"})

    async def lb_healthz(req):
        return web.json_response({"ok": True, "release": obs.RELEASE, "healthy_backends": len(pool.healthy())})

    async def scaler_status(req):
        c = ctx["controller"]
        if c is None:
            return web.json_response({"mode": "lb-only", "controller": "disabled (no Liara token or SCALER_ENABLED=0)",
                                      "policy": policy}, headers={"Cache-Control": "no-store"})
        return web.json_response(c.status(), headers={"Cache-Control": "no-store"}, dumps=lambda o: json.dumps(o, default=str))

    async def scaler_audit(req):
        c = ctx["controller"]
        n = max(1, min(500, int(req.query.get("limit", "100"))))
        return web.json_response({"audit": list(c.audit)[-n:] if c else []})

    async def scaler_override(req):
        if not authorized(req, "SCALER_ADMIN_TOKEN"):
            obs.log("auth.fail", "warn", "override rejected: bad token", route=req.path)
            return web.json_response({"error": "unauthorized"}, status=401)
        c = ctx["controller"]
        if c is None:
            return web.json_response({"accepted": False, "reason": "controller disabled"}, status=409)
        body = await req.json()
        return web.json_response(c.override(body))

    async def lb_backends(req):
        if not authorized(req, "SCALER_ADMIN_TOKEN"):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await req.json()
        name, url = str(body.get("name", ""))[:60], str(body.get("url", ""))[:200]
        if body.get("op") == "add" and name and url.startswith("https://"):
            pool.add(name, url)
        elif body.get("op") == "remove" and name in pool.backends:
            if len(pool.healthy()) <= 1 and pool.backends[name].healthy:
                return web.json_response({"ok": False, "reason": "never remove the last healthy backend"}, status=409)
            pool.remove(name)
        else:
            return web.json_response({"ok": False, "reason": "op must be add (https url) or remove (known name)"}, status=400)
        if ctx["controller"]:
            ctx["controller"].record("backend_" + body["op"], str(body.get("actor") or "human")[:40], "done", "manual", backend=name)
        return web.json_response({"ok": True, "backends": [b.name for b in pool.backends.values()]})

    async def api_replicas(req):
        """Negahban's compact view."""
        if not authorized(req, "SCALER_READ_TOKEN", "SCALER_ADMIN_TOKEN"):
            return web.json_response({"error": "unauthorized"}, status=401)
        c, now = ctx["controller"], time.time()
        apps = []
        if c:
            st = c.status()
            last = next((a for a in reversed(c.audit) if a["kind"] in ("scale_out", "scale_in") and a["result"] == "done"), None)
            apps.append({"app": c.app, "mode": "replicas",
                         "replicas": [{"name": b.name, "plan": policy["replica_plan"] if b.managed else c.base_info.get("plan"),
                                       "state": "draining" if b.draining else ("healthy" if b.healthy else "unhealthy"),
                                       "healthy": b.healthy, "latency_ms": b.view(now)["p95_ms"], "weight": 1}
                                      for b in pool.backends.values()],
                         "desired": st["last_decision"]["target"] if st["last_decision"] else st["instances"],
                         "min": policy["min"], "max": policy["max"],
                         "policy": {"scale_out_cpu": policy["scale_out"]["cpu"], "scale_in_cpu": policy["scale_in"]["cpu"],
                                    "scale_out_rps_per_backend": policy["scale_out"]["rps_per_backend"],
                                    "cooldown_s": policy["cooldown_out_s"]},
                         "last_action": {"ts": last["ts"], "kind": last["kind"], "reason": last["reason"]} if last else None})
        apps += [{"app": a, "mode": "single", "reason": r, "replicas": []} for a, r in VERTICAL.items()]
        return web.json_response({"generated_at": now, "apps": apps})

    async def api_override(req):
        if not authorized(req, "SCALER_ADMIN_TOKEN"):
            return web.json_response({"error": "unauthorized"}, status=401)
        c = ctx["controller"]
        if c is None or req.match_info["app"] != c.app:
            return web.json_response({"accepted": False, "reason": "app is not horizontally scaled"}, status=409)
        body = await req.json()
        return web.json_response(c.override({"action": "desired", "desired": body.get("desired"), "reason": body.get("reason"),
                                             "minutes": max(1, int(body.get("ttl_s") or 1800) // 60),
                                             "actor": body.get("actor") or "negahban-ai"}))

    app.router.add_get("/lb/status", lb_status)
    app.router.add_get("/lb/healthz", lb_healthz)
    app.router.add_post("/lb/backends", lb_backends)
    app.router.add_get("/scaler/status", scaler_status)
    app.router.add_get("/scaler/audit", scaler_audit)
    app.router.add_post("/scaler/override", scaler_override)
    app.router.add_get("/api/replicas", api_replicas)
    app.router.add_post("/api/replicas/{app}/override", api_override)
    app.router.add_route("*", "/{tail:.*}", pool.handle)
    return app


def main() -> None:
    web.run_app(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")), access_log=None,
                print=lambda *_: None)


if __name__ == "__main__":
    main()
