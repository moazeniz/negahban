"""Tests for the backup and monitoring jobs against in-process fakes (no network, nothing touches Liara)."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "infra"), str(ROOT / "ops" / "backup"), str(ROOT / "ops" / "monitor")]

os.environ["PROBE_RETRY_DELAY"] = "0.01"

import liara_backup  # noqa: E402
import probe  # noqa: E402


def sqlite_bytes(rows: int = 3) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.db"
        con = sqlite3.connect(p)
        con.execute("create table t(a)")
        con.executemany("insert into t values(?)", [(i,) for i in range(rows)])
        con.commit()
        con.close()
        return p.read_bytes()


class FakeLiara:
    def __init__(self):
        self.files = {"./memory.db": sqlite_bytes(), "./cfg.json": b'{"a":1}', "./sub/notes.txt": b"hello",
                      "./catalog.db": b"big seed"}
        self.uploads, self.calls = {}, []
        self.health_ok = True
        self.health2_ok = True
        self.fail_next = 0          # fail this many /healthz requests, then recover
        self.vantage = None         # JSON served at /vantage (Negahban internal probes)
        self.bundle = "basic"


def serve(fake: FakeLiara):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj=None, raw=None):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def route(self, method):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            fake.calls.append((method, p))
            if p == "/healthz":
                if fake.fail_next > 0:
                    fake.fail_next -= 1
                    return self.send(503, {"ok": False})
                return self.send(200 if fake.health_ok else 503, {"ok": fake.health_ok})
            if p == "/healthz2":
                return self.send(200 if fake.health2_ok else 503, {"ok": fake.health2_ok})
            if p == "/vantage":
                if self.headers.get("Authorization") != "Bearer ingest-test-token":
                    return self.send(401, {})
                return self.send(200, fake.vantage or {"apps": []})
            if p == "/slow":
                time.sleep(0.05)
                return self.send(200, {"ok": True})
            if p == "/v1/projects/app1":
                return self.send(200, {"project": {"_id": "pid1", "planID": "small-g2", "bundlePlanID": fake.bundle}})
            if p == "/v1/projects/pid1/disks":
                return self.send(200, {"disks": [{"name": "data", "size": 1, "filebrowserUrl": f"http://127.0.0.1:{self.server.server_address[1]}"}]})
            if p == "/v1/me":
                caps = {"basic": {"availableBackupInterval": []}, "standard": {"availableBackupInterval": ["daily", "manual"]}}
                return self.send(200, {"plans": {"projectBundlePlans": {"small-g2": caps}}})
            if p == "/v1/projects/pid1/disks/data/list":
                if q.get("page", "1") != "1":
                    return self.send(200, {"items": []})
                base = q["path"].rstrip("/")
                items, dirs = [], set()
                for f, data in fake.files.items():
                    parent, name = f.rsplit("/", 1)
                    if parent == base:
                        items.append({"path": f, "name": name, "size": len(data), "isDir": False})
                    elif parent.startswith(base + "/"):
                        sub = base + "/" + parent[len(base) + 1:].split("/")[0]
                        if sub not in dirs:
                            dirs.add(sub)
                            items.append({"path": sub, "name": sub.rsplit("/", 1)[1], "size": 4096, "isDir": True})
                return self.send(200, {"items": items})
            if p == "/v1/projects/pid1/disks/data/createLink":
                return self.send(201, {"queries": {"path": q["path"], "type": "download", "token": "tok-" + q["path"]}})
            if p == "/v1/projects/pid1/disks/data/serve":
                assert q["token"] == "tok-" + q["path"]
                return self.send(200, raw=fake.files[q["path"]])
            if p == "/v1/projects/pid1/disks/data/upload":
                n = int(self.headers["Content-Length"])
                body = self.rfile.read(n)
                data = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
                fake.uploads[q["path"]] = data
                return self.send(201, {})
            if p == "/v1/projects/pid1/disks/data/backups":
                return self.send(201 if method == "POST" else 200, {"backups": []})
            return self.send(404, {"message": p})

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLiara()
        self.srv = serve(self.fake)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.old = {k: os.environ.get(k) for k in ("LIARA_API_TOKEN", "LIARA_TEAM_ID", "LIARA_API_BASE", "STATUS_INGEST_URL",
                                                   "INTERNAL_VANTAGE_URL", "STATUS_INGEST_TOKEN")}
        os.environ.update({"LIARA_API_TOKEN": "t0ken-for-tests", "LIARA_TEAM_ID": "team1", "LIARA_API_BASE": self.url})
        os.environ.pop("STATUS_INGEST_URL", None)
        os.environ.pop("INTERNAL_VANTAGE_URL", None)
        os.environ["STATUS_INGEST_TOKEN"] = "ingest-test-token"  # noqa: S105 - test fixture
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def quiet(self, fn, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            r = fn(*a)
        return r, buf.getvalue()


class TestBackup(Base):
    def export(self):
        li = liara_backup.Liara()
        arc, _ = self.quiet(liara_backup.export, li, "app1", "data", self.out, ["catalog.db"])
        return arc

    def test_export_and_verify(self):
        arc = self.export()
        self.assertTrue(arc.name.startswith("app1--data--") and arc.name.endswith(".tar.gz"))
        self.assertEqual(oct(arc.stat().st_mode & 0o777), "0o600")
        ok, rep = liara_backup.verify(arc)
        self.assertTrue(ok, rep)
        self.assertTrue(any("sqlite memory.db: integrity_check=ok tables=1" in r for r in rep), rep)
        with tarfile.open(arc) as tf:
            names = tf.getnames()
            man = json.load(tf.extractfile("MANIFEST.json"))
        self.assertIn("data/sub/notes.txt", names)
        self.assertNotIn("data/catalog.db", names)  # excluded: reproducible seed
        self.assertEqual({f["path"] for f in man["files"]}, {"memory.db", "cfg.json", "sub/notes.txt"})

    def test_verify_detects_corruption(self):
        arc = self.export()
        bad = self.out / "app1--data--20990101T000000Z.tar.gz"
        with tarfile.open(arc) as src, tarfile.open(bad, "w:gz") as dst:
            for m in src.getmembers():
                data = src.extractfile(m).read() if m.isfile() else None
                if m.name == "data/memory.db":
                    data = data[:100] + b"X" * 50 + data[150:]
                if data is not None:
                    m.size = len(data)
                    dst.addfile(m, io.BytesIO(data))
                else:
                    dst.addfile(m)
        ok, rep = liara_backup.verify(bad)
        self.assertFalse(ok)
        self.assertIn("HASH MISMATCH memory.db", rep)

    def test_verify_rejects_path_traversal(self):
        evil = self.out / "evil.tar.gz"
        with tarfile.open(evil, "w:gz") as tf:
            ti = tarfile.TarInfo("../escape.txt")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
        ok, rep = liara_backup.verify(evil)
        self.assertFalse(ok)
        self.assertIn("unsafe member", rep[0])

    def test_prune_keeps_newest_and_weekly(self):
        for day in range(1, 29):
            (self.out / f"app1--data--202610{day:02d}T023000Z.tar.gz").write_bytes(b"x")
        removed = liara_backup.prune(self.out, keep=7, weekly=4)
        left = sorted(p.name for p in self.out.glob("*.tar.gz"))
        self.assertEqual(len(left), 28 - len(removed))
        self.assertIn("app1--data--20261028T023000Z.tar.gz", left)
        self.assertIn("app1--data--20261022T023000Z.tar.gz", left)  # 7 newest
        self.assertLessEqual(len(left), 7 + 4)

    def test_restore_is_dry_run_by_default_and_non_destructive(self):
        arc = self.export()
        li = liara_backup.Liara()
        code, out = self.quiet(liara_backup.restore, li, arc, "app1", "data", "restore-test", False, False)
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(self.fake.uploads, {})
        code, out = self.quiet(liara_backup.restore, li, arc, "app1", "data", "restore-test", False, True)
        self.assertEqual(code, 0, out)
        self.assertEqual(set(self.fake.uploads), {"restore-test", "restore-test/sub"})

    def test_native_backup_refused_on_basic(self):
        li = liara_backup.Liara()
        d = liara_backup.Disk(li, "app1", "data")
        with self.assertRaises(SystemExit):
            d.native_create()
        self.fake.bundle = "standard"
        d = liara_backup.Disk(li, "app1", "data")
        d.native_create()
        self.assertIn(("POST", "/v1/projects/pid1/disks/data/backups"), self.fake.calls)

    def test_run_job_writes_status(self):
        manifest = {"apps": [{"name": "app1", "disks": [{"name": "data", "backup": {"native": False, "offsite": {"keep": 2, "exclude": ["catalog.db"]}}}]}]}
        code, out = self.quiet(liara_backup.run, liara_backup.Liara(), manifest, self.out, None)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((self.out / "last-run.json").read_text())["failures"], 0)
        self.assertNotIn("t0ken-for-tests", out)


class TestProbe(Base):
    def manifest(self, path="/healthz"):
        return {"apps": [{"name": "app1", "url": self.url, "slo": {"availability": 99.5, "p95LatencyMs": 1500, "probePath": path},
                          "smoke": {"checks": [{"path": "/healthz", "expectJson": {"ok": True}}]}}]}

    def test_up_down_and_stats(self):
        st = self.out / "state"
        doc, out = self.quiet(probe.run, self.manifest(), st, False)
        self.assertTrue(doc["apps"][0]["up"])
        self.fake.health_ok = False
        self.quiet(probe.run, self.manifest(), st, False)
        doc, out = self.quiet(probe.run, self.manifest(), st, False)
        a = doc["apps"][0]
        self.assertFalse(a["up"])  # down after 2 consecutive failures
        self.assertIn('"event": "app.down"', out)
        self.assertAlmostEqual(a["availability_24h"], 33.333, places=2)
        self.assertEqual(doc["overall"], "outage")
        self.assertEqual(len(a["incidents_7d"]), 1)
        self.fake.health_ok = True
        doc, out = self.quiet(probe.run, self.manifest(), st, False)
        self.assertTrue(doc["apps"][0]["up"])
        self.assertIn('"event": "app.up"', out)
        pub = st / "public"
        self.assertTrue((pub / "status.html").read_text().startswith("<!doctype html>"))
        badge = json.loads((pub / "badge-app1.json").read_text())
        self.assertEqual(badge["schemaVersion"], 1)
        self.assertEqual(badge["color"], "red")

    def test_content_check(self):
        m = self.manifest()
        m["apps"][0]["smoke"]["checks"][0]["expectJson"] = {"ok": "yes"}
        doc, _ = self.quiet(probe.run, m, self.out / "s2", False)
        self.assertEqual(doc["apps"][0]["last_error"], "JSON check failed")

    def test_percentile(self):
        self.assertEqual(probe.percentile(list(range(1, 101)), 95), 95)
        self.assertIsNone(probe.percentile([], 95))

    def test_status_json_has_no_secrets_and_escapes_html(self):
        m = self.manifest()
        m["apps"][0]["name"] = "<script>x</script>"
        self.quiet(probe.run, m, self.out / "s3", False)
        page = (self.out / "s3" / "public" / "status.html").read_text()
        self.assertNotIn("<script>x</script>", page)
        self.assertNotIn("t0ken-for-tests", (self.out / "s3" / "public" / "status.json").read_text())


class TestTwoVantages(Base):
    def manifest(self):
        app = lambda n, path: {"name": n, "url": self.url, "slo": {"availability": 99.5, "p95LatencyMs": 1500, "probePath": path},  # noqa: E731
                               "smoke": {"checks": [{"path": path, "expectJson": {"ok": True}}]}}
        return {"apps": [app("a1", "/healthz"), app("a2", "/healthz2")]}

    def vantage(self, a1_ok=True, a2_ok=True, age=0):
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        self.fake.vantage = {"vantage": "liara-internal", "apps": [
            {"name": "a1", "ok": a1_ok, "status": 200 if a1_ok else 503, "latency_ms": 12, "checked_at": ts, "error": None if a1_ok else "HTTP 503"},
            {"name": "a2", "ok": a2_ok, "status": 200 if a2_ok else 503, "latency_ms": 9, "checked_at": ts, "error": None}]}
        os.environ["INTERNAL_VANTAGE_URL"] = self.url + "/vantage"

    def run_probe(self, st):
        doc, out = self.quiet(probe.run, self.manifest(), st, False)
        return {a["name"]: a for a in doc["apps"]}, doc, out

    def test_retry_once_hides_a_blip(self):
        self.fake.fail_next = 1
        apps, _, out = self.run_probe(self.out / "r")
        self.assertEqual(apps["a1"]["verdict"], "up")
        self.assertEqual(apps["a1"]["vantages"]["external"]["tries"], 2)
        self.assertEqual(apps["a1"]["availability_24h"], 100.0)

    def test_http_errors_on_all_apps_are_real_outages(self):
        self.fake.health_ok = self.fake.health2_ok = False   # HTTP 503 everywhere: the apps answered, so it is them
        apps, doc, _ = self.run_probe(self.out / "h")
        self.assertEqual(apps["a1"]["verdict"], "down")
        self.assertEqual(doc["notes"], [])

    def test_network_failures_on_most_apps_are_vantage_unreachable(self):
        dead = "http://127.0.0.1:1"   # connection refused: no HTTP answer at all
        m = self.manifest()
        m["apps"] += [{"name": n, "url": dead, "slo": {"probePath": "/healthz"}} for n in ("a3", "a4")]
        st = self.out / "v"
        self.quiet(probe.run, m, st, False)
        doc, out = self.quiet(probe.run, {"apps": [dict(a, url=dead) for a in m["apps"][:3]] + [m["apps"][3]]}, st, False)
        apps = {a["name"]: a for a in doc["apps"]}
        self.assertEqual(apps["a1"]["verdict"], "vantage_unreachable")   # now 4 of 4 apps fail with network errors
        for _ in range(2):
            doc, out = self.quiet(probe.run, {"apps": [dict(a, url=dead) for a in m["apps"]]}, st, False)
        apps = {a["name"]: a for a in doc["apps"]}
        self.assertEqual(apps["a1"]["verdict"], "vantage_unreachable")
        self.assertTrue(apps["a1"]["up"])                       # no state change: says nothing about the apps
        self.assertEqual(apps["a1"]["availability_24h"], 100.0)  # excluded from availability
        self.assertEqual(doc["notes"][0]["kind"], "vantage_unreachable")
        self.assertEqual(doc["notes"][0]["runs"], 3)
        # three of four apps timing out together (one still answering) is also the vantage, not the apps
        three = {"apps": [dict(a, url="http://127.0.0.1:1") for a in m["apps"][:3]] + [dict(m["apps"][0], name="a4")]}
        doc, _ = self.quiet(probe.run, three, st, False)
        self.assertEqual({a["name"]: a["verdict"] for a in doc["apps"]}["a2"], "vantage_unreachable")
        self.assertNotIn('"event": "app.down"', out)
        self.assertIn("Probe-side notes", (st / "public" / "status.html").read_text())

    def test_single_app_down_from_outside_only_is_degraded_not_down(self):
        self.vantage(a1_ok=True)
        self.fake.health_ok = False
        st = self.out / "d"
        for _ in range(3):
            apps, _, out = self.run_probe(st)
        self.assertEqual(apps["a1"]["verdict"], "degraded_external_only")
        self.assertTrue(apps["a1"]["up"])
        self.assertEqual(apps["a1"]["availability_24h"], 100.0)
        self.assertTrue(apps["a1"]["vantages"]["internal"]["ok"])
        self.assertFalse(apps["a1"]["vantages"]["external"]["ok"])

    def test_both_vantages_agree_down(self):
        self.vantage(a1_ok=False)
        self.fake.health_ok = False
        st = self.out / "b"
        for _ in range(2):
            apps, _, out = self.run_probe(st)
        self.assertEqual(apps["a1"]["verdict"], "down")
        self.assertFalse(apps["a1"]["up"])
        self.assertIn('"event": "app.down"', out)
        self.assertEqual(apps["a2"]["verdict"], "up")

    def test_all_fail_externally_but_internal_says_up(self):
        self.vantage()
        m = {"apps": [dict(a, url="http://127.0.0.1:1") for a in self.manifest()["apps"]]}
        doc, _ = self.quiet(probe.run, m, self.out / "x", False)
        apps = {a["name"]: a for a in doc["apps"]}
        self.assertEqual(apps["a1"]["verdict"], "degraded_external_only")
        self.assertEqual(doc["notes"][0]["kind"], "vantage_unreachable")

    def test_stale_internal_results_are_unknown(self):
        self.vantage(a1_ok=True, age=600)
        self.fake.health_ok = False
        apps, _, _ = self.run_probe(self.out / "s")
        self.assertEqual(apps["a1"]["verdict"], "down")
        self.assertIsNone(apps["a1"]["vantages"]["internal"])

    def test_legacy_rows_are_migrated_and_all_fail_runs_reclassified(self):
        st = self.out / "m"
        st.mkdir()
        con = sqlite3.connect(st / "probes.db")
        con.execute("create table probes(ts REAL, app TEXT, ok INTEGER, status INTEGER, latency_ms REAL, error TEXT)")
        t = time.time() - 600
        con.executemany("insert into probes values(?,?,?,?,?,?)", [
            (t, "a1", 1, 200, 100, ""), (t, "a2", 1, 200, 100, ""),
            (t + 60, "a1", 0, 0, 10000, "timeout"), (t + 60, "a2", 0, 0, 10000, "timeout"),   # vantage blip (network)
            (t + 120, "a1", 0, 503, 50, "HTTP 503"), (t + 120, "a2", 1, 200, 100, "")])          # real a1 failure
        con.commit()
        con.close()
        apps, doc, _ = self.run_probe(st)
        self.assertAlmostEqual(apps["a1"]["availability_24h"], 66.667, places=2)   # 2 good of 3 counted
        self.assertEqual(apps["a2"]["availability_24h"], 100.0)
        self.assertEqual(len(doc["notes"]), 1)


if __name__ == "__main__":
    unittest.main()
