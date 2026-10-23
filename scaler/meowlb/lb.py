"""Reverse proxy / load balancer: least-connections (round-robin tie-break), active health checks with
eject/re-admit hysteresis, one retry on another backend for idempotent requests, optional stickiness,
X-Request-ID propagation and per-backend metrics."""
from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

from . import obs
from .policy import percentile

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
       "upgrade", "host", "content-length", "content-encoding"}
IDEMPOTENT = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
RETRY_STATUS = {502, 503, 504}


@dataclass
class Backend:
    name: str
    url: str
    managed: bool = False             # created by the replica controller
    healthy: bool = False             # starts out of rotation until the first health check passes
    draining: bool = False
    in_flight: int = 0
    requests: int = 0
    errors: int = 0
    ok_streak: int = 0
    fail_streak: int = 0
    last_check: float = 0.0
    last_error: str | None = None
    healthy_since: float | None = None
    added_at: float = field(default_factory=time.time)
    latencies: deque = field(default_factory=lambda: deque(maxlen=600))   # (ts, ms)
    outcomes: deque = field(default_factory=lambda: deque(maxlen=200))    # (ts, is_5xx) for outlier detection
    ejected_until: float = 0.0        # passive (outlier) ejection: out of rotation until then

    def view(self, now: float) -> dict:
        recent = [ms for ts, ms in self.latencies if now - ts <= 60]
        return {"name": self.name, "url": self.url, "managed": self.managed, "healthy": self.healthy,
                "draining": self.draining, "in_flight": self.in_flight, "requests": self.requests, "errors": self.errors,
                "rpm": len(recent), "p95_ms": percentile(recent, 95), "last_check": self.last_check or None,
                "last_error": self.last_error, "healthy_since": self.healthy_since,
                "outlier_ejected_until": self.ejected_until or None}


class Pool:
    def __init__(self, health_path: str = "/healthz", strategy: str = "least_conn", sticky: bool = False,
                 rise: int = 2, fall: int = 2, interval: float = 5.0, timeout: float = 3.0,
                 outlier_ratio: float = 0.5, outlier_min_requests: int = 20, outlier_window: float = 30.0,
                 outlier_eject_s: float = 30.0):
        self.backends: dict[str, Backend] = {}
        self.health_path, self.strategy, self.sticky = health_path, strategy, sticky
        self.rise, self.fall, self.interval, self.timeout = rise, fall, interval, timeout
        self.outlier_ratio, self.outlier_min, self.outlier_window, self.outlier_eject_s = (
            outlier_ratio, outlier_min_requests, outlier_window, outlier_eject_s)
        self._rr = itertools.count()
        self.hits: deque = deque(maxlen=200_000)            # (ts, ms, ok) of every proxied request
        self.session: aiohttp.ClientSession | None = None

    # -- membership -------------------------------------------------------------------
    def add(self, name: str, url: str, managed: bool = False) -> Backend:
        b = self.backends.get(name)
        if b:
            b.url, b.draining = url.rstrip("/"), False
            return b
        b = self.backends[name] = Backend(name, url.rstrip("/"), managed)
        obs.log("lb.backend_added", msg=f"backend {name} added (out of rotation until healthy)", backend=name, url=b.url)
        return b

    def remove(self, name: str) -> None:
        if self.backends.pop(name, None):
            obs.log("lb.backend_removed", msg=f"backend {name} removed", backend=name)

    def healthy(self) -> list[Backend]:
        now = time.time()
        return [b for b in self.backends.values() if b.healthy and not b.draining and b.ejected_until <= now]

    def observe(self, b: Backend, is_5xx: bool) -> None:
        """Passive health (outlier detection): an active /healthz can pass while real pages fail. If at least
        `outlier_min` requests in the last `outlier_window` s were 5xx at >= `outlier_ratio`, take the backend out of
        rotation for `outlier_eject_s` s - unless it is the last healthy one."""
        now = time.time()
        b.outcomes.append((now, is_5xx))
        recent = [x for ts, x in b.outcomes if now - ts <= self.outlier_window]
        if len(recent) < self.outlier_min or b.ejected_until > now:
            return
        ratio = sum(recent) / len(recent)
        if ratio >= self.outlier_ratio and len([o for o in self.healthy() if o is not b]) >= 1:
            b.ejected_until = now + self.outlier_eject_s
            b.outcomes.clear()
            obs.log("lb.outlier_ejected", "warn", f"{b.name} ejected for {int(self.outlier_eject_s)} s: "
                    f"{ratio:.0%} 5xx over {len(recent)} requests", backend=b.name, ratio=round(ratio, 3))

    def pick(self, exclude: set[str] = frozenset(), sticky_to: str | None = None) -> Backend | None:
        live = [b for b in self.healthy() if b.name not in exclude]
        if not live:
            return None
        if sticky_to:
            for b in live:
                if b.name == sticky_to:
                    return b
        if self.strategy == "round_robin":
            return live[next(self._rr) % len(live)]
        low = min(b.in_flight for b in live)
        tied = [b for b in live if b.in_flight == low]
        return tied[next(self._rr) % len(tied)]

    # -- health checks --------------------------------------------------------------------
    async def check(self, b: Backend) -> None:
        t0 = time.monotonic()
        try:
            async with self.session.get(b.url + self.health_path, timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                ok = r.status == 200
                err = None if ok else f"HTTP {r.status}"
        except Exception as e:  # noqa: BLE001 - any failure is a failed check
            ok, err = False, type(e).__name__
        b.last_check = time.time()
        if ok:
            b.ok_streak, b.fail_streak, b.last_error = b.ok_streak + 1, 0, None
            if not b.healthy and b.ok_streak >= self.rise:
                b.healthy, b.healthy_since = True, time.time()
                obs.log("lb.backend_up", msg=f"{b.name} healthy, back in rotation", backend=b.name,
                        duration_ms=round((time.monotonic() - t0) * 1000))
        else:
            b.fail_streak, b.ok_streak, b.last_error = b.fail_streak + 1, 0, err
            if b.healthy and b.fail_streak >= self.fall:
                b.healthy, b.healthy_since = False, None
                obs.log("lb.backend_down", "warn", f"{b.name} ejected after {b.fail_streak} failed checks", backend=b.name, error=err)

    async def health_loop(self) -> None:
        while True:
            await asyncio.gather(*(self.check(b) for b in list(self.backends.values())), return_exceptions=True)
            await asyncio.sleep(self.interval)

    # -- metrics ------------------------------------------------------------------------------
    def rps_1m(self, now: float | None = None) -> float:
        now = now or time.time()
        return sum(1 for ts, _, _ in self.hits if now - ts <= 60) / 60.0

    def p95_1m(self, now: float | None = None) -> float | None:
        now = now or time.time()
        return percentile([ms for ts, ms, ok in self.hits if now - ts <= 60 and ok], 95)

    def status(self) -> dict:
        now = time.time()
        return {"generated_at": now, "strategy": self.strategy, "sticky": self.sticky, "health_path": self.health_path,
                "rps_1m": round(self.rps_1m(now), 2), "p95_ms_1m": self.p95_1m(now),
                "healthy": len(self.healthy()), "backends": [b.view(now) for b in self.backends.values()]}

    # -- proxy ----------------------------------------------------------------------------------
    async def handle(self, req: web.Request) -> web.StreamResponse:
        rid = req.headers.get("X-Request-ID") or uuid.uuid4().hex
        body = await req.read() if req.can_read_body else None
        sticky_to = None
        if self.sticky:
            sticky_to = req.cookies.get("meowlb")
        tried: set[str] = set()
        attempts = 2 if req.method in IDEMPOTENT else 1
        last_err = "no healthy backend"
        for attempt in range(1, attempts + 1):
            b = self.pick(tried, sticky_to)
            if not b:
                break
            tried.add(b.name)
            headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP}
            fwd = req.headers.get("X-Forwarded-For")
            peer = req.remote or ""
            headers.update({"X-Request-ID": rid, "X-Forwarded-For": f"{fwd}, {peer}" if fwd else peer,
                            "X-Forwarded-Proto": req.headers.get("X-Forwarded-Proto", req.scheme),
                            "X-Forwarded-Host": req.headers.get("Host", "")})
            t0 = time.monotonic()
            b.in_flight += 1
            b.requests += 1
            try:
                async with self.session.request(req.method, b.url + req.path_qs, headers=headers, data=body,
                                                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    payload = await r.read()
                    ms = (time.monotonic() - t0) * 1000
                    if r.status in RETRY_STATUS and attempt < attempts:
                        b.errors += 1
                        last_err = f"HTTP {r.status} from {b.name}"
                        obs.log("lb.retry", "warn", last_err, request_id=rid, backend=b.name, status=r.status, attempt=attempt)
                        continue
                    ok = r.status < 500
                    if not ok:
                        b.errors += 1
                    self.observe(b, not ok)
                    b.latencies.append((time.time(), ms))
                    self.hits.append((time.time(), ms, ok))
                    resp = web.Response(status=r.status, body=payload,
                                        headers={k: v for k, v in r.headers.items() if k.lower() not in HOP})
                    resp.headers["X-Request-ID"] = rid
                    resp.headers["X-Backend"] = b.name
                    if self.sticky:
                        resp.set_cookie("meowlb", b.name, max_age=3600, httponly=True, samesite="Lax")
                    obs.log("http.request", request_id=rid, method=req.method, route=req.path, status=r.status,
                            duration_ms=round(ms, 1), backend=b.name, attempt=attempt, upstream=b.url)
                    return resp
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                b.errors += 1
                last_err = f"{type(e).__name__} from {b.name}"
                obs.log("lb.upstream_error", "warn", last_err, request_id=rid, backend=b.name, attempt=attempt,
                        error_type=type(e).__name__)
            finally:
                b.in_flight -= 1
        self.hits.append((time.time(), 0.0, False))
        obs.log("http.request", "error", "no backend answered", request_id=rid, method=req.method, route=req.path,
                status=503, error=last_err)
        return web.json_response({"error": "no healthy backend available", "detail": last_err, "request_id": rid},
                                 status=503, headers={"X-Request-ID": rid, "Retry-After": "5"})
