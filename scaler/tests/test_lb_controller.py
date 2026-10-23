"""Load balancer + controller against in-process fakes: fake backends (aiohttp) and a fake Liara API."""
import asyncio
import io
import json
import os
import sys
import tarfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meowlb import controller as ctl  # noqa: E402
from meowlb.app import build_app  # noqa: E402
from meowlb.lb import Pool  # noqa: E402
from meowlb.liara import Liara  # noqa: E402
from meowlb.policy import DEFAULT_POLICY, merge_policy  # noqa: E402

os.environ["SCALER_ADMIN_TOKEN"] = "admin-test-token"  # noqa: S105 - test fixture
os.environ["SCALER_READ_TOKEN"] = "read-test-token"  # noqa: S105 - test fixture


def backend_app(name, state):
    async def any_(req):
        state["hits"] = state.get("hits", 0) + 1
        if state.get("status_once"):
            st = state.pop("status_once")
            return web.Response(status=st)
        if state.get("status_always"):
            return web.Response(status=state["status_always"])
        return web.json_response({"backend": name, "path": req.path, "rid": req.headers.get("X-Request-ID")})

    async def healthz(req):
        return web.json_response({"ok": state.get("healthy", True)}, status=200 if state.get("healthy", True) else 503)

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_route("*", "/{t:.*}", any_)
    return app


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.out = io.StringIO()
        self._redir = redirect_stdout(self.out)
        self._redir.__enter__()
        self.servers = []

    async def asyncTearDown(self):
        for s in self.servers:
            await s.close()
        self._redir.__exit__(None, None, None)

    async def serve(self, app):
        s = TestServer(app)
        await s.start_server()
        self.servers.append(s)
        return s


class TestLB(Base):
    async def make(self, n=2, **pool_kw):
        self.states = [{} for _ in range(n)]
        urls = []
        for i in range(n):
            s = await self.serve(backend_app(f"b{i}", self.states[i]))
            urls.append(str(s.make_url("")).rstrip("/"))
        pool = Pool(interval=0.05, timeout=1, **pool_kw)
        for i, u in enumerate(urls):
            pool.add(f"b{i}", u)
        app = build_app(pool=pool, start_background=False)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.servers.append(self.client)
        self.pool = pool
        for _ in range(2):  # rise=2: two good checks put backends in rotation
            await asyncio.gather(*(pool.check(b) for b in pool.backends.values()))

    async def test_distributes_and_propagates_request_id(self):
        await self.make()
        seen = []
        for _ in range(10):
            r = await self.client.get("/hello", headers={"X-Request-ID": "rid-1"})
            body = await r.json()
            seen.append(body["backend"])
            self.assertEqual(body["rid"], "rid-1")
            self.assertEqual(r.headers["X-Request-ID"], "rid-1")
        self.assertEqual(sorted(set(seen)), ["b0", "b1"])
        self.assertTrue(4 <= seen.count("b0") <= 6)

    async def test_eject_and_readmit(self):
        await self.make()
        self.states[1]["healthy"] = False
        for _ in range(2):
            await asyncio.gather(*(self.pool.check(b) for b in self.pool.backends.values()))
        self.assertFalse(self.pool.backends["b1"].healthy)
        for _ in range(5):
            self.assertEqual((await (await self.client.get("/x")).json())["backend"], "b0")
        self.states[1]["healthy"] = True
        for _ in range(2):
            await asyncio.gather(*(self.pool.check(b) for b in self.pool.backends.values()))
        self.assertTrue(self.pool.backends["b1"].healthy)
        self.assertIn('"event": "lb.backend_down"', self.out.getvalue())
        self.assertIn('"event": "lb.backend_up"', self.out.getvalue())

    async def test_retry_idempotent_only(self):
        await self.make(strategy="round_robin")
        hits = lambda: sum(st.get("hits", 0) for st in self.states)  # noqa: E731
        self.states[0]["status_once"] = 502
        self.states[1]["status_once"] = 502
        r = await self.client.get("/x")
        self.assertEqual((r.status, hits()), (502, 2))          # GET: tried on the second backend too
        self.states[0]["status_once"] = 502
        self.states[1]["status_once"] = 502
        r = await self.client.post("/x", data=b"{}")
        self.assertEqual((r.status, hits()), (502, 3))          # POST: never retried
        self.states[0].pop("status_once", None)
        self.states[1].pop("status_once", None)
        self.states[0]["status_once"] = 503
        for _ in range(2):
            r = await self.client.get("/y")
            self.assertEqual(r.status, 200)                     # one bad answer is hidden by the retry

    async def test_outlier_detection_ejects_a_backend_failing_real_requests(self):
        await self.make(strategy="round_robin", outlier_min_requests=6, outlier_window=60, outlier_eject_s=60)
        self.states[0]["status_always"] = 500          # /healthz still passes, real pages fail (a bad release)
        codes = [(await self.client.post("/x", data=b"{}")).status for _ in range(30)]
        self.assertFalse(any(b.name == "b0" for b in self.pool.healthy()))
        self.assertTrue(all(c == 200 for c in codes[-10:]))   # after ejection only b1 answers
        self.assertIn('"event": "lb.outlier_ejected"', self.out.getvalue())

    async def test_outlier_never_ejects_the_last_backend(self):
        await self.make(n=1, outlier_min_requests=4)
        self.states[0]["status_always"] = 500
        for _ in range(10):
            await self.client.post("/x", data=b"{}")
        self.assertEqual(len(self.pool.healthy()), 1)

    async def test_no_healthy_backend_is_503(self):
        await self.make(n=1)
        self.pool.backends["b0"].healthy = False
        r = await self.client.get("/x")
        self.assertEqual(r.status, 503)
        self.assertIn("Retry-After", r.headers)

    async def test_status_and_admin_auth(self):
        await self.make()
        for _ in range(4):
            await self.client.get("/x")
        st = await (await self.client.get("/lb/status")).json()
        self.assertEqual(st["healthy"], 2)
        self.assertEqual(sum(b["requests"] for b in st["backends"]), 4)
        self.assertEqual((await self.client.post("/lb/backends", json={"op": "remove", "name": "b0"})).status, 401)
        hdr = {"Authorization": "Bearer admin-test-token"}
        r = await self.client.post("/lb/backends", json={"op": "remove", "name": "b0"}, headers=hdr)
        self.assertEqual((await r.json())["backends"], ["b1"])
        r = await self.client.post("/lb/backends", json={"op": "remove", "name": "b1"}, headers=hdr)
        self.assertEqual(r.status, 409)  # never the last healthy backend
        self.assertEqual((await self.client.get("/api/replicas")).status, 401)
        self.assertEqual((await self.client.get("/api/replicas", headers={"Authorization": "Bearer read-test-token"})).status, 200)


def fake_liara(state):
    """Projects, releases, source download/upload and app create/delete - enough for the controller."""
    routes = web.RouteTableDef()

    @routes.get("/v1/me")
    async def me(req):
        caps = {"basic": {"price": 381.94}, "standard": {"price": 659.72}}
        small = {"price": 659.72, "CPU": {"amount": 0.5}, "RAM": {"amount": 0.5}}
        return web.json_response({"team": {"balance": 5_000_000}, "plans": {"projects": {"small-g2": small},
                                            "projectBundlePlans": {"small-g2": caps}}})

    @routes.get("/v1/usage-report")
    async def usage(req):
        return web.json_response({"totalHourlyPrice": 5000})

    @routes.get("/v1/projects")
    async def projects(req):
        return web.json_response({"projects": [{"project_id": n} for n in state["apps"]]})

    @routes.post("/v1/projects")
    async def create(req):
        body = await req.json()
        if state.get("create_429"):
            return web.json_response({"message": "You need enough balance to cover one week's cost"}, status=429)
        state["apps"][body["name"]] = {"envs": [], "planID": body["planID"]}
        state["calls"].append(("create", body["name"], body["planID"], body.get("network")))
        return web.json_response({}, status=201)

    @routes.get("/v1/projects/{app}")
    async def project(req):
        a = req.match_info["app"]
        if a not in state["apps"]:
            return web.json_response({}, status=404)
        return web.json_response({"project": {"_id": a, "planID": "small-g2", "bundlePlanID": "standard",
                                              "network": {"_id": "net1"}, **state["apps"][a]}})

    @routes.post("/v1/projects/{app}/resize")
    async def resize(req):
        body = await req.json()
        state["calls"].append(("resize", req.match_info["app"], body["planID"]))
        if state.get("resize_429"):
            return web.json_response({"message": "You need enough balance to cover one week's cost"}, status=429)
        state["apps"][req.match_info["app"]]["planID"] = body["planID"]
        return web.json_response({}, status=200)

    @routes.delete("/v1/projects/{app}")
    async def delete(req):
        state["apps"].pop(req.match_info["app"], None)
        state["calls"].append(("delete", req.match_info["app"]))
        return web.json_response({}, status=204)

    @routes.get("/v1/projects/{app}/releases")
    async def releases(req):
        return web.json_response({"currentRelease": "base-rel", "releases": [{"_id": "base-rel", "port": 8080, "tag": "v9"}]})

    @routes.get("/v1/projects/{app}/metrics/summary")
    async def metrics(req):
        return web.json_response({"cpuUsage": [{"value": [0, "0.1"]}], "memoryUsage": [{"value": [0, str(128 * 1024 ** 2)]}]})

    @routes.post("/v2/releases/{rid}/source")
    async def link(req):
        return web.json_response({"url": str(req.url.with_path("/download").with_query(""))})

    @routes.get("/download")
    async def download(req):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"FROM python:3.12-slim\n"
            ti = tarfile.TarInfo("Dockerfile")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        return web.Response(body=buf.getvalue())

    @routes.post("/v2/projects/{app}/sources")
    async def upload(req):
        state["calls"].append(("upload", req.match_info["app"], len(await req.read())))
        return web.json_response({"sourceID": "src1"}, status=201)

    @routes.post("/v2/projects/{app}/releases")
    async def release(req):
        body = await req.json()
        state["calls"].append(("release", req.match_info["app"], body["envs"], body["port"]))
        state["replica_healthy"] = state.get("release_ok", True)
        return web.json_response({"releaseID": "rel-" + req.match_info["app"]}, status=201)

    @routes.get("/v1/releases/{rid}")
    async def rel(req):
        return web.json_response({"release": {"state": "READY" if state.get("release_ok", True) else "FAILED"}})

    @routes.get("/replica/healthz")
    async def replica_health(req):
        ok = state.get("replica_healthy", False)
        return web.json_response({"ok": ok}, status=200 if ok else 503)

    app = web.Application()
    app.add_routes(routes)
    return app


class TestController(Base):
    async def make(self, release_ok=True):
        self.state = {"apps": {"demo-shop": {"envs": [{"key": "PAYMENT_GATEWAY_KEY", "value": "k"}]}}, "calls": [],
                      "release_ok": release_ok}
        fl = await self.serve(fake_liara(self.state))
        base = await self.serve(backend_app("base", {}))
        self.session = __import__("aiohttp").ClientSession()
        self.pool = Pool(interval=0.05, timeout=1)
        self.pool.session = self.session
        self.pool.add("demo-shop", str(base.make_url("")).rstrip("/"))
        pol = merge_policy(DEFAULT_POLICY, {"replica_url": str(fl.make_url("")).rstrip("/") + "/replica"})
        li = Liara(self.session, token="t", team="team", base=str(fl.make_url("")).rstrip("/"))  # noqa: S106
        self.c = ctl.Controller(li, self.pool, pol)
        self.c.poll_s = self.c.health_poll_s = self.c.drain_poll_s = 0.01
        self.health = asyncio.create_task(self.pool.health_loop())
        await self.c.bootstrap()

    async def asyncTearDown(self):
        self.health.cancel()
        await self.session.close()
        await super().asyncTearDown()

    async def test_scale_out_and_in(self):
        await self.make()
        await self.c.scale_out("demo-shop-r1", "test", "controller")
        self.assertEqual(self.c.replica_names, ["demo-shop-r1"])
        self.assertTrue(self.pool.backends["demo-shop-r1"].healthy)
        rel = [c for c in self.state["calls"] if c[0] == "release"][0]
        self.assertEqual(rel[2], {"PAYMENT_GATEWAY_KEY": "k"})   # env copied from the base app
        self.assertEqual(self.state["calls"][0][1:], ("demo-shop-r1", "small-g2", "net1"))
        self.assertNotIn('"k"', self.out.getvalue())               # env values never logged
        await self.c.scale_in("demo-shop-r1", "test", "controller")
        self.assertNotIn("demo-shop-r1", self.state["apps"])
        self.assertNotIn("demo-shop-r1", self.pool.backends)
        kinds = [(a["kind"], a["result"]) for a in self.c.audit]
        self.assertIn(("scale_out", "done"), kinds)
        self.assertIn(("scale_in", "done"), kinds)

    async def test_failed_release_cleans_up(self):
        await self.make(release_ok=False)
        await self.c.scale_out("demo-shop-r1", "test", "controller")
        self.assertNotIn("demo-shop-r1", self.state["apps"])       # never leave a half-built replica billing
        self.assertIn(("scale_out", "failed"), [(a["kind"], a["result"]) for a in self.c.audit])

    async def test_bootstrap_adopts_existing_replicas_and_refuses_base_delete(self):
        await self.make()
        self.state["apps"]["demo-shop-r2"] = {"envs": []}
        c2 = ctl.Controller(self.c.li, Pool(), dict(self.c.pol))
        await c2.bootstrap()
        self.assertEqual(c2.replica_names, ["demo-shop-r2"])
        self.assertEqual(c2.next_name(), "demo-shop-r1")
        with self.assertRaises(RuntimeError):
            await c2.remove_app("demo-shop", "x", "test")

    async def test_scale_in_never_removes_last_healthy(self):
        await self.make()
        await self.c.scale_out("demo-shop-r1", "test", "controller")
        self.pool.backends["demo-shop"].healthy = False
        self.pool.interval = 3600
        await self.c.scale_in("demo-shop-r1", "test", "controller")
        self.assertIn("demo-shop-r1", self.state["apps"])
        self.assertIn(("scale_in", "rejected"), [(a["kind"], a["result"]) for a in self.c.audit])

    async def test_replica_429_backs_off_and_is_audited(self):
        await self.make()
        self.state["create_429"] = True
        await self.c.scale_out("demo-shop-r1", "test", "controller")
        self.assertEqual(self.c.replica_names, [])
        self.assertGreater(self.c.state.out_refused_until, 0)
        last = self.c.audit[-1]
        self.assertEqual((last["kind"], last["result"], last["blocked"]), ("scale_out", "refused", "refused"))
        self.assertIn("قانون موجودی یک‌هفته‌ای لیارا", last["reason_fa"])

    async def test_vertical_resize_verified_and_back(self):
        await self.make()
        self.c.pol["vertical"] = {**self.c.pol["vertical"], "enabled": True, "verify_timeout_s": 5}
        await self.c.resize_base("medium-g2", "scale_up", "test", "controller")
        self.assertEqual(self.state["calls"][-1], ("resize", "demo-shop", "medium-g2"))
        self.assertEqual((self.c.state.base_plan, self.c.state.vertical_from), ("medium-g2", "small-g2"))
        self.assertEqual((self.c.audit[-1]["result"], self.c.audit[-1]["to_plan"]), ("done", "medium-g2"))
        self.assertIn("تأیید شد", self.c.audit[-1]["reason_fa"])
        await self.c.resize_base("small-g2", "scale_down", "test", "controller")
        self.assertEqual((self.c.state.base_plan, self.c.state.vertical_from), ("small-g2", None))

    async def test_resize_refusal_backs_off(self):
        await self.make()
        self.state["resize_429"] = True
        await self.c.resize_base("medium-g2", "scale_up", "test", "controller")
        self.assertEqual(self.c.audit[-1]["result"], "refused")
        self.assertGreater(self.c.state.up_refused_until, 0)
        self.assertEqual(self.c.state.base_plan, "small-g2")

    async def test_dry_run_audits_without_acting(self):
        await self.make()
        self.c.override({"action": "dry_run", "minutes": 5})
        self.c.state.breach_out_since = self.c.now() - 120
        self.pool.rps_1m = lambda: 50.0
        d = await self.c.tick()
        self.assertEqual(d.action, "scale_out")
        self.assertIsNone(self.c.task)
        self.assertEqual(self.c.audit[-1]["result"], "would_scale_out")
        self.assertNotIn("create", [c[0] for c in self.state["calls"]])
        st = json.loads(json.dumps(self.c.status(), default=str))
        self.assertIsNotNone(st["modes"]["dry_run_until"])

    async def test_balance_refusal_is_audited_once_per_episode(self):
        await self.make()
        self.c._billing_at = self.c.now()
        self.c.state.breach_out_since = self.c.now() - 120
        self.pool.rps_1m = lambda: 50.0

        async def low_balance():
            self.c.state.balance, self.c.state.run_rate_hourly = 1_000_000, 9_000
        self.c.refresh_billing = low_balance
        await low_balance()
        for _ in range(3):
            await self.c.tick()
        refused = [a for a in self.c.audit if a["result"] == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["blocked"], "balance")
        self.assertIn("نیاز", refused[0]["reason_fa"])

    async def test_override_clamps(self):
        await self.make()
        r = self.c.override({"action": "desired", "desired": 9, "reason": "judge demo", "actor": "negahban-ai"})
        self.assertEqual(r["applied_desired"], 3)
        self.assertFalse(self.c.override({"action": "set_bounds", "min": 0, "max": 2})["accepted"])
        self.assertTrue(self.c.override({"action": "hold", "minutes": 5})["accepted"])
        st = json.loads(json.dumps(self.c.status(), default=str))
        self.assertEqual(st["registry"][1]["mode"], "vertical")
        self.assertEqual(st["llm"][:8], "not used")


if __name__ == "__main__":
    unittest.main()
