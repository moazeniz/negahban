#!/usr/bin/env python3
"""Create (if needed) and deploy the meow-lb Liara app from this directory.

  deploy_meowlb.py [MESSAGE]       run as root on the operator host
Secrets: /root/.liara.env (LIARA_API_TOKEN, LIARA_TEAM_ID, LIARA_API_BASE) and /root/.meow-scaler.env
(SCALER_ADMIN_TOKEN, SCALER_READ_TOKEN). Values are passed to Liara as env vars and never printed.
Policy overrides: SCALER_POLICY_FILE (JSON) or the default below.
"""
import io
import json
import os
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
# demo-shop is Negahban's chaos target: its CPU/RAM leak scenarios are fixed by a restart, not by more replicas, so
# CPU/RAM triggers are off for it (None) and it scales on request rate and latency only.
POLICY = {"app": "demo-shop", "min": 1, "max": 3,
          "scale_out": {"rps_per_backend": 5.0, "p95_ms": 2000, "cpu": None, "mem": None, "sustain_s": 60},
          "scale_in": {"rps_per_backend": 1.0, "p95_ms": 800, "cpu": None, "mem": None, "sustain_s": 120},
          "cooldown_out_s": 120, "cooldown_in_s": 120, "daily_budget_toman": 30000}


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


def main():
    li_env, sc_env = envfile("/root/.liara.env"), envfile("/root/.meow-scaler.env")
    os.environ.update({k: li_env[k] for k in ("LIARA_API_TOKEN", "LIARA_TEAM_ID", "LIARA_API_BASE")})
    li = Liara()
    names = {p["project_id"] for p in li.projects()}
    if APP not in names:
        net = li.project("demo-shop")["network"]["_id"]
        li.post("/v1/projects", {"name": APP, "planID": PLAN, "platform": "docker", "bundlePlanID": BUNDLE, "network": net})
        print("created app", APP)
    policy = json.loads(Path(os.environ["SCALER_POLICY_FILE"]).read_text()) if os.getenv("SCALER_POLICY_FILE") else POLICY
    envs = {"LIARA_API_TOKEN": li_env["LIARA_API_TOKEN"], "LIARA_TEAM_ID": li_env["LIARA_TEAM_ID"],
            "LIARA_API_BASE": li_env.get("LIARA_API_BASE") or "https://api.iran.liara.ir",
            "SCALER_ADMIN_TOKEN": sc_env["SCALER_ADMIN_TOKEN"], "SCALER_READ_TOKEN": sc_env["SCALER_READ_TOKEN"],
            "LB_BACKENDS": "demo-shop=https://demo-shop.liara.run", "LB_STRATEGY": "least_conn",
            "SCALER_POLICY": json.dumps(policy), "OBS_ENV": "liara"}
    hc = json.loads((HERE / "liara.json").read_text())["healthCheck"]
    archive = tarball()
    sid = li.upload_source(APP, archive)
    body = {"sourceID": sid, "type": "docker", "port": PORT, "envs": envs, "healthCheck": hc,
            "message": sys.argv[1] if len(sys.argv) > 1 else "meow-lb deploy",
            "build": {"location": "germany", "cache": True}}
    rid = li.create_release(APP, body)
    print("release", rid, f"({len(archive)} bytes)", flush=True)
    t0 = time.time()
    while time.time() - t0 < 900:
        st = li.release(rid).get("state")
        print(f"{int(time.time() - t0):4d}s {st}", flush=True)
        if st in ("READY", "FAILED", "UNHEALTHY", "CANCELED", "TIMEDOUT"):
            return 0 if st == "READY" else 1
        time.sleep(10)
    return 1


if __name__ == "__main__":
    sys.exit(main())
