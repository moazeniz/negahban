#!/usr/bin/env python3
"""Create (if needed) and deploy the meow-lb Liara app from this directory.

  deploy_meowlb.py [MESSAGE]                 production: meow-lb in front of demo-shop (team «دوخت»)
  deploy_meowlb.py --lab [MESSAGE]           lab stack in the TEST team «لیارا»: meow-lb-lab in front of demo-shop-lab
  deploy_meowlb.py --lab-shop                (re)deploy demo-shop-lab there from ../demo-shop (small-g2 + basic)
  deploy_meowlb.py --delete-lab              delete meow-lb-lab, demo-shop-lab and every demo-shop-lab-rN (test team only)
Run as root on the operator host. The lab uses LIARA_API_TOKEN_2 and the test team id; it never touches production.
Secrets: /root/.liara.env (LIARA_API_TOKEN, LIARA_TEAM_ID, LIARA_API_BASE) and /root/.meow-scaler.env
(SCALER_ADMIN_TOKEN, SCALER_READ_TOKEN). Values are passed to Liara as env vars and never printed.
Policy overrides: SCALER_POLICY_FILE (JSON) or the default below.
"""
import io
import json
import os
import re
import secrets
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for cand in (HERE.parent / "infra", Path("/opt/meow-platform/infra")):  # platform kit's Liara client
    if (cand / "liara_api.py").exists():
        sys.path.insert(0, str(cand))
        break
from liara_api import Liara  # noqa: E402

APP, PLAN, BUNDLE, PORT = "meow-lb", "small-g2", "basic", 8080
LAB_TEAM, LAB_APP, LAB_SHOP = "<liara-test-team-id>", "meow-lb-lab", "demo-shop-lab"
# demo-shop is Negahban's chaos target: its CPU/RAM leak scenarios are fixed by a restart, not by more replicas, so
# CPU/RAM triggers are off for it (None) and it scales on request rate and latency only.
POLICY = {"app": "demo-shop", "min": 1, "max": 3,
          "scale_out": {"rps_per_backend": 5.0, "p95_ms": 2000, "cpu": None, "mem": None, "sustain_s": 60},
          "scale_in": {"rps_per_backend": 1.0, "p95_ms": 800, "cpu": None, "mem": None, "sustain_s": 120},
          "cooldown_out_s": 120, "cooldown_in_s": 120, "daily_budget_toman": 30000}
# Lab (test team): the same rules in front of demo-shop-lab, at most one replica; Negahban's autopilot owns the vertical
# small-g2 <-> medium-g2 step there too, so the controller's own vertical fallback stays off.
LAB_POLICY = {**POLICY, "app": LAB_SHOP, "max": 2, "daily_budget_toman": 15000,
              "scale_out": {**POLICY["scale_out"]}, "scale_in": {**POLICY["scale_in"]}}


def envfile(p):
    out = {}
    for line in Path(p).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(HERE.rglob("*")):
            rel = p.relative_to(HERE)
            if p.is_file() and "__pycache__" not in rel.parts and rel.parts[0] not in ("evidence",) and p.suffix != ".pyc":
                tf.add(p, arcname=str(rel))
    return buf.getvalue()


def wait(li, rid, label):
    t0 = time.time()
    while time.time() - t0 < 900:
        st = li.release(rid).get("state")
        print(f"{label} {int(time.time() - t0):4d}s {st}", flush=True)
        if st in ("READY", "FAILED", "UNHEALTHY", "CANCELED", "TIMEDOUT"):
            return 0 if st == "READY" else 1
        time.sleep(10)
    return 1


def lab_env(li_env):
    os.environ.update({"LIARA_API_TOKEN": li_env["LIARA_API_TOKEN_2"], "LIARA_TEAM_ID": LAB_TEAM,
                       "LIARA_API_BASE": li_env.get("LIARA_API_BASE") or "https://api.iran.liara.ir"})
    return Liara()


def lab_network(li):
    nets = li.get("/v1/networks").get("networks") or []
    if not nets:
        sys.exit("no private network in the test team")
    return nets[0]["_id"]


def deploy_lab_shop(li):
    """demo-shop-lab: the demo-shop source on small-g2 + basic in the test team (env values are lab-only)."""
    src = HERE.parent / "demo-shop"
    if LAB_SHOP not in {p["project_id"] for p in li.projects()}:
        li.post("/v1/projects", {"name": LAB_SHOP, "planID": "small-g2", "platform": "docker", "bundlePlanID": "basic",
                                 "network": lab_network(li)})
        print("created app", LAB_SHOP)
    lab = Path("/root/.meow-lab.env")
    if not lab.exists():
        lab.write_text(f"LAB_CHAOS_TOKEN={secrets.token_urlsafe(24)}\n")
        lab.chmod(0o600)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if p.is_file() and "__pycache__" not in rel.parts and rel.parts[0] != "tests" and p.suffix != ".pyc":
                tf.add(p, arcname=str(rel))
    sid = li.upload_source(LAB_SHOP, buf.getvalue())
    envs = {"PAYMENT_GATEWAY_KEY": "lab-sandbox-key", "CHAOS_TOKEN": envfile(lab)["LAB_CHAOS_TOKEN"], "APP_VERSION": "lab"}
    hc = {"command": ["CMD", "python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=5)"],
          "interval": 30, "timeout": 10, "retries": 3, "startPeriod": 20}
    rid = li.create_release(LAB_SHOP, {"sourceID": sid, "type": "docker", "port": 8080, "envs": envs, "healthCheck": hc,
                                       "message": "lab copy of demo-shop (scaling demo)", "build": {"location": "germany", "cache": True}})
    return wait(li, rid, LAB_SHOP)


def delete_lab(li):
    if li.team != LAB_TEAM:
        sys.exit("refusing: lab deletion runs in the test team only")
    gone = []
    for p in li.projects():
        n = p["project_id"]
        if n in (LAB_APP, LAB_SHOP) or re.fullmatch(rf"{LAB_SHOP}-r\d+", n):
            li.request("DELETE", f"/v1/projects/{n}", what=f"delete {n}")
            gone.append(n)
    left = [p["project_id"] for p in li.projects()]
    print(json.dumps({"deleted": gone, "left_in_test_team": left}, ensure_ascii=False))
    return 0


def main():
    args = sys.argv[1:]
    li_env, sc_env = envfile("/root/.liara.env"), envfile("/root/.meow-scaler.env")
    lab = "--lab" in args or "--lab-shop" in args or "--delete-lab" in args
    if lab:
        li = lab_env(li_env)
        if "--delete-lab" in args:
            return delete_lab(li)
        if "--lab-shop" in args:
            return deploy_lab_shop(li)
        app, policy, backend, team_label = LAB_APP, LAB_POLICY, f"{LAB_SHOP}=https://{LAB_SHOP}.liara.run", "لیارا (تیم آزمایشی)"
        net = lambda: lab_network(li)  # noqa: E731
    else:
        os.environ.update({k: li_env[k] for k in ("LIARA_API_TOKEN", "LIARA_TEAM_ID", "LIARA_API_BASE")})
        li = Liara()
        app, backend, team_label = APP, "demo-shop=https://demo-shop.liara.run", "دوخت (تولید)"
        policy = json.loads(Path(os.environ["SCALER_POLICY_FILE"]).read_text()) if os.getenv("SCALER_POLICY_FILE") else POLICY
        net = lambda: li.project("demo-shop")["network"]["_id"]  # noqa: E731
    msg = next((a for a in args if not a.startswith("--")), f"{app} deploy")
    if app not in {p["project_id"] for p in li.projects()}:
        li.post("/v1/projects", {"name": app, "planID": PLAN, "platform": "docker", "bundlePlanID": BUNDLE, "network": net()})
        print("created app", app)
    envs = {"LIARA_API_TOKEN": os.environ["LIARA_API_TOKEN"], "LIARA_TEAM_ID": os.environ["LIARA_TEAM_ID"],
            "LIARA_API_BASE": os.environ.get("LIARA_API_BASE") or "https://api.iran.liara.ir", "LIARA_TEAM_LABEL": team_label,
            "SCALER_ADMIN_TOKEN": sc_env["SCALER_ADMIN_TOKEN"], "SCALER_READ_TOKEN": sc_env["SCALER_READ_TOKEN"],
            "LB_BACKENDS": backend, "LB_STRATEGY": "least_conn",
            "SCALER_POLICY": json.dumps(policy), "OBS_ENV": "liara"}
    hc = json.loads((HERE / "liara.json").read_text())["healthCheck"]
    archive = tarball()
    sid = li.upload_source(app, archive)
    body = {"sourceID": sid, "type": "docker", "port": PORT, "envs": envs, "healthCheck": hc, "message": msg,
            "build": {"location": "germany", "cache": True}}
    rid = li.create_release(app, body)
    print("release", rid, f"({len(archive)} bytes)", flush=True)
    return wait(li, rid, app)


if __name__ == "__main__":
    sys.exit(main())
