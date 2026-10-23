#!/usr/bin/env python3
"""Controllable, bounded load for the scaling demo: it ramps up, holds and stops, and prints one status line every 5 s.

  demo_load.py --profile ramp --target https://meow-lb-lab.liara.run --max-rps 30 --minutes 8
  demo_load.py --stop                  stop a running instance (SIGTERM through its pid file)

Profiles (share of --max-rps over the run):
  ramp      0 -> 100 % over the first 25 %, hold, back to 0 over the last 15 %
  demo      0 -> 100 % over the first 60 s, hold, back to 0 over the last 30 s
  step      25 / 50 / 75 / 100 % in four equal steps
  spike     20 % baseline with a 100 % spike in the middle 40 %
  constant  100 % the whole time

Safety: --max-rps is capped at 40 and --minutes at 12; at most 150 requests are in flight; every request times out
after 15 s; only our own apps are allowed as targets; Ctrl-C, SIGTERM, --stop and the hard deadline always stop it.
Requests: 70 % GET /api/report (CPU-heavy on purpose, n=--report-n) and 30 % GET / by default (--mix).
Status: one line every 5 s on stdout and the same data as JSON in --status-file (atomic writes), for an on-screen panel.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import signal
import sys
import time
from collections import Counter
from pathlib import Path

import aiohttp

ALLOWED = {"https://meow-lb.liara.run", "https://meow-lb-lab.liara.run", "https://demo-shop.liara.run",
           "https://demo-shop-lab.liara.run"}
MAX_RPS, MAX_MINUTES, MAX_IN_FLIGHT, TIMEOUT_S = 40.0, 12.0, 150, 15
PIDFILE = Path(os.getenv("DEMO_LOAD_PIDFILE", "/tmp/meow-demo-load.pid"))  # noqa: S108


def share(profile: str, t: float, dur: float) -> float:
    """Fraction of max rps at second t of a dur-second run."""
    x = t / dur
    if profile == "constant":
        return 1.0
    if profile == "step":
        return min(4, int(x * 4) + 1) / 4
    if profile == "spike":
        return 1.0 if 0.3 <= x < 0.7 else 0.2
    if profile == "demo":
        up, down = min(60.0, dur * 0.3), min(30.0, dur * 0.2)
        return max(0.0, min(1.0, t / up, (dur - t) / down))
    return max(0.0, min(1.0, x / 0.25, (1 - x) / 0.15))  # ramp


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[max(0, math.ceil(p / 100 * len(s)) - 1)])


def phase(profile: str, t: float, dur: float) -> str:
    a, b = share(profile, max(0.0, t - 1), dur), share(profile, t, dur)
    if b == 0:
        return "stopped"
    return "ramp-up" if b > a + 1e-9 else "ramp-down" if b < a - 1e-9 else "hold"


async def run(a) -> int:
    dur = min(a.minutes, MAX_MINUTES) * 60
    max_rps = min(a.max_rps, MAX_RPS)
    target = a.target.rstrip("/")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    PIDFILE.write_text(str(os.getpid()))
    win = {"lat": [], "codes": Counter(), "backends": Counter()}
    tot = {"codes": Counter(), "backends": Counter(), "n": 0, "lat": []}
    sem = asyncio.Semaphore(MAX_IN_FLIGHT)
    t0 = time.monotonic()
    started = time.time()
    status_file = Path(a.status_file)
    status_file.parent.mkdir(parents=True, exist_ok=True)

    def path() -> str:
        return f"/api/report?n={a.report_n}" if random.random() < a.mix else "/"  # noqa: S311 - not crypto

    async def one(session):
        async with sem:
            t = time.monotonic()
            try:
                async with session.get(target + path(), headers={"User-Agent": "meow-demo-load/1.0"},
                                       timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as r:
                    await r.read()
                    code, be = str(r.status), r.headers.get("X-Backend", "direct")
            except Exception as e:  # noqa: BLE001
                code, be = type(e).__name__, "-"
            ms = (time.monotonic() - t) * 1000
            for d in (win, tot):
                d["codes"][code] += 1
                d["backends"][be] += 1
                d["lat"].append(ms)
            tot["n"] += 1

    def snapshot(final=False) -> dict:
        el = time.monotonic() - t0
        n = sum(win["codes"].values())
        errors = sum(v for k, v in win["codes"].items() if not k.startswith(("2", "3")))
        rec = {"t_s": round(el), "of_s": round(dur), "phase": "done" if final else phase(a.profile, el, dur),
               "target_rps": round(share(a.profile, min(el, dur), dur) * max_rps, 1), "rps": round(n / 5, 1),
               "ok": n - errors, "errors": errors, "p50_ms": pct(win["lat"], 50), "p95_ms": pct(win["lat"], 95),
               "codes": dict(win["codes"]), "backends": dict(win["backends"]), "target": target, "profile": a.profile,
               "max_rps": max_rps, "started_at": started, "updated_at": time.time(),
               "total": {"requests": tot["n"], "codes": dict(tot["codes"]), "backends": dict(tot["backends"]),
                         "p50_ms": pct(tot["lat"], 50), "p95_ms": pct(tot["lat"], 95)}}
        tmp = status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False))
        tmp.replace(status_file)
        return rec

    async def reporter():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except asyncio.TimeoutError:
                pass
            r = snapshot()
            be = " ".join(f"{k}:{v}" for k, v in sorted(r["backends"].items())) or "-"
            print(f"[t={r['t_s']:4d}s/{r['of_s']}s] {r['phase']:<9} target {r['target_rps']:4.1f} rps | sent {r['rps']:4.1f} rps | "
                  f"ok {r['ok']:3d} err {r['errors']:2d} | p50 {r['p50_ms'] or '-'} ms p95 {r['p95_ms'] or '-'} ms | backends {be}",
                  flush=True)
            win["lat"], win["codes"], win["backends"] = [], Counter(), Counter()

    print(f"demo_load: {a.profile} to {target}, max {max_rps:g} rps for {dur / 60:g} min "
          f"({a.mix:.0%} /api/report?n={a.report_n}); Ctrl-C or `demo_load.py --stop` stops it", flush=True)
    tasks: set[asyncio.Task] = set()
    rep = asyncio.create_task(reporter())
    conn = aiohttp.TCPConnector(limit=MAX_IN_FLIGHT, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=conn) as session:
        credit = 0.0
        last = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            el = now - t0
            if el >= dur:
                break
            credit += share(a.profile, el, dur) * max_rps * (now - last)
            last = now
            while credit >= 1 and len(tasks) < MAX_IN_FLIGHT:
                credit -= 1
                task = asyncio.create_task(one(session))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            credit = min(credit, max_rps)  # never burst to catch up
            try:
                await asyncio.wait_for(stop.wait(), 0.05)
            except asyncio.TimeoutError:
                pass
        stop.set()
        if tasks:
            await asyncio.wait(tasks, timeout=TIMEOUT_S + 1)
            for task in tasks:
                task.cancel()
    await rep
    r = snapshot(final=True)
    print(json.dumps({"summary": r["total"], "seconds": round(time.monotonic() - t0)}, ensure_ascii=False), flush=True)
    try:
        PIDFILE.unlink()
    except FileNotFoundError:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=["ramp", "demo", "step", "spike", "constant"], default="ramp")
    ap.add_argument("--target", default="https://meow-lb-lab.liara.run")
    ap.add_argument("--max-rps", type=float, default=30)
    ap.add_argument("--minutes", type=float, default=8)
    ap.add_argument("--mix", type=float, default=0.7, help="share of requests that hit /api/report (0..1)")
    ap.add_argument("--report-n", type=int, default=40000, help="size of the CPU-heavy report (1000..200000)")
    ap.add_argument("--status-file", default=os.getenv("DEMO_LOAD_STATUS", "/tmp/meow-demo-load/status.json"))  # noqa: S108
    ap.add_argument("--stop", action="store_true", help="stop a running instance")
    a = ap.parse_args()
    if a.stop:
        try:
            os.kill(int(PIDFILE.read_text()), signal.SIGTERM)
            print("stop signal sent")
        except (FileNotFoundError, ProcessLookupError, ValueError):
            print("no running demo load")
        return 0
    if a.target.rstrip("/") not in ALLOWED:
        print(f"refusing: target must be one of {sorted(ALLOWED)}", file=sys.stderr)
        return 2
    if not (0 < a.max_rps and 0 < a.minutes and 0 <= a.mix <= 1 and 1000 <= a.report_n <= 200000):
        print("refusing: max-rps and minutes must be > 0, mix in 0..1, report-n in 1000..200000", file=sys.stderr)
        return 2
    if a.max_rps > MAX_RPS or a.minutes > MAX_MINUTES:
        print(f"note: capped to {MAX_RPS:g} rps and {MAX_MINUTES:g} min", file=sys.stderr)
    if PIDFILE.exists():
        try:
            os.kill(int(PIDFILE.read_text()), 0)
            print("refusing: another demo load is running (demo_load.py --stop)", file=sys.stderr)
            return 2
        except (ProcessLookupError, ValueError):
            pass
    return asyncio.run(asyncio.wait_for(run(a), min(a.minutes, MAX_MINUTES) * 60 + 60))


if __name__ == "__main__":
    sys.exit(main())
