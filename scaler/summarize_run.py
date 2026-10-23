#!/usr/bin/env python3
"""Turn a scaling_demo.sh evidence folder into a Markdown report (timeline, per-backend split, errors, cost).

  summarize_run.py EVIDENCE_DIR > SCALING_RUN.md
"""
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def hhmmss(ts) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%H:%M:%S")


def main(d: Path) -> None:
    load = [json.loads(line) for line in (d / "loadtest.jsonl").read_text().splitlines() if line.strip()]
    status = [json.loads(line) for line in (d / "status.jsonl").read_text().splitlines() if line.strip()]
    audit = json.loads((d / "audit.json").read_text())["audit"] if (d / "audit.json").exists() else []
    run = json.loads((d / "run.json").read_text())
    summary = next((x for x in load if x.get("summary")), {})
    windows = [x for x in load if not x.get("summary")]

    print(f"# Live scaling run: {run['started']}\n")
    print(f"Load: {run['rps']} rps for {run['load_minutes']} min against {run['lb']} (load generator on Hetzner, outside Liara).\n")
    print("## Result\n")
    codes = summary.get("codes", {})
    ok = sum(v for k, v in codes.items() if k.startswith("2"))
    print(f"- Requests: **{summary.get('requests')}**, of which {ok} were 2xx "
          f"({100 * ok / max(1, summary.get('requests', 1)):.2f} %); status codes: `{codes}`")
    print(f"- Split by backend (`X-Backend` header): `{summary.get('backends')}`")
    inst = [(s["t"], s["scaler"]["instances"]) for s in status if s.get("scaler")]
    print(f"- Instances during the run: max {max(i for _, i in inst)}, final {inst[-1][1]}")
    print("\n## Controller audit (deterministic, no LLM)\n")
    print("| Time (UTC) | Event | Result | Reason |\n|---|---|---|---|")
    t0 = datetime.fromisoformat(run["started"].replace("Z", "+00:00")).timestamp()
    for a in audit:
        if a["ts"] >= t0 - 60:
            reason = re.sub(r'\{"statusCode".*', "(Liara 429: one-week balance rule)", a["reason"])[:140]
            print(f"| {hhmmss(a['ts'])} | {a['kind']} {a.get('replica', '')} | {a['result']} | {reason} |")
    print("\n## Decision changes (from /scaler/status every 15 s)\n")
    print("| Time (UTC) | Instances | rps / backend | Decision |\n|---|---|---|---|")
    prev = None
    for s in status:
        sc = s.get("scaler")
        if not sc or not sc.get("last_decision"):
            continue
        r = sc["last_decision"]["reason"]
        key = re.sub(r"[0-9.,]+", "#", r)
        if key != prev:
            print(f"| {s['t'][11:19]} | {sc['instances']} | {sc['signals']['rps_per_backend']} | {r[:150]} |")
            prev = key
    print("\n## Traffic split per 10 s window while two backends were live\n")
    print("| t (s) | requests | p95 ms | per backend |\n|---|---|---|---|")
    for w in windows:
        if len(w.get("backends", {})) > 1:
            print(f"| {w['t']} | {w['requests']} | {w['p95_ms']} | {w['backends']} |")
    lb_final = json.loads((d / "lb-final.json").read_text()) if (d / "lb-final.json").exists() else {}
    if lb_final:
        print("\n## Load balancer counters at the end\n")
        print("| Backend | Healthy | Requests | Errors |\n|---|---|---|---|")
        for b in lb_final.get("backends", []):
            print(f"| {b['name']} | {b['healthy']} | {b['requests']} | {b['errors']} |")
    errs = Counter()
    for w in windows:
        for k, v in w["codes"].items():
            if not k.startswith("2"):
                errs[k] += v
    print(f"\nNon-2xx during the run: `{dict(errs) or 'none'}`.")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
