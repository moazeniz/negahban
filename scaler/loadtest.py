#!/usr/bin/env python3
"""Bounded load generator for the scaling demo (hard caps: 50 rps, 30 minutes).

  loadtest.py URL --rps 15 --minutes 12 [--path /] [--out run.jsonl]
Prints one JSON line per 10 s window: requests, status codes, p50/p95 latency and the per-backend split
(from the X-Backend header the load balancer adds), plus a final summary.
"""
import argparse
import asyncio
import json
import math
import time
from collections import Counter

import aiohttp
import aiohttp.abc


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[max(0, math.ceil(p / 100 * len(s)) - 1)], 1)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--rps", type=float, default=15)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--path", default="/")
    ap.add_argument("--out")
    ap.add_argument("--resolve", help="HOST:IP, like curl --resolve (bypass a stale local resolver)")
    a = ap.parse_args()
    rps, secs = min(a.rps, 50.0), min(a.minutes, 30.0) * 60
    window, total = {"lat": [], "codes": Counter(), "backends": Counter()}, {"codes": Counter(), "backends": Counter(), "n": 0}
    out = open(a.out, "a") if a.out else None
    t_start = time.time()

    async def one(session):
        t0 = time.monotonic()
        try:
            async with session.get(a.url.rstrip("/") + a.path, timeout=aiohttp.ClientTimeout(total=20),
                                   headers={"User-Agent": "meow-loadtest/1.0"}) as r:
                await r.read()
                code, be = r.status, r.headers.get("X-Backend", "?")
        except Exception as e:  # noqa: BLE001
            code, be = type(e).__name__, "-"
        window["lat"].append((time.monotonic() - t0) * 1000)
        window["codes"][str(code)] += 1
        window["backends"][be] += 1
        total["codes"][str(code)] += 1
        total["backends"][be] += 1
        total["n"] += 1

    async def reporter():
        while True:
            await asyncio.sleep(10)
            rec = {"t": round(time.time() - t_start), "requests": sum(window["codes"].values()), "codes": dict(window["codes"]),
                   "p50_ms": pct(window["lat"], 50), "p95_ms": pct(window["lat"], 95), "backends": dict(window["backends"])}
            print(json.dumps(rec), flush=True)
            if out:
                out.write(json.dumps(rec) + "\n")
                out.flush()
            window["lat"], window["codes"], window["backends"] = [], Counter(), Counter()

    resolver = None
    if a.resolve:
        host, ip = a.resolve.split(":", 1)

        class Static(aiohttp.abc.AbstractResolver):
            async def resolve(self, h, port=0, family=0):
                return [{"hostname": h, "host": ip if h == host else h, "port": port, "family": 2, "proto": 0, "flags": 0}]

            async def close(self):
                pass
        resolver = Static()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=100, resolver=resolver)) as session:
        rep = asyncio.create_task(reporter())
        tasks, i = set(), 0
        while time.time() - t_start < secs:
            i += 1
            t = asyncio.create_task(one(session))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
            await asyncio.sleep(max(0.0, t_start + i / rps - time.time()))
        await asyncio.gather(*tasks)
        rep.cancel()
    summary = {"summary": True, "seconds": round(time.time() - t_start), "requests": total["n"],
               "codes": dict(total["codes"]), "backends": dict(total["backends"])}
    print(json.dumps(summary), flush=True)
    if out:
        out.write(json.dumps(summary) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
