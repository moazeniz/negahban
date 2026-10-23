#!/usr/bin/env python3
"""External synthetic monitoring for the Liara apps (runs outside Liara, on the Finland host).

  probe.py run    [-f infra/liara.yaml] [--state DIR] [--no-push]   probe every app once, update SLO stats + status page
  probe.py report [--state DIR]                                     print availability / latency per app
  probe.py render [--state DIR]                                     rebuild status.json / status.html / badges only

Every minute (systemd timer) each app gets one HTTPS GET of its SLO probe path. A probe is UP when the
status code matches, the body passes the content check from the manifest (JSON subset or substring) and the
answer arrives within the timeout. Latency is measured from TCP connect to the last body byte (DNS is timed
separately and excluded); TLS expiry comes from the certificate served on the same connection.

State lives in SQLite (<state>/probes.db, 35 days). Outputs in <state>/public/ (no secrets, safe to publish):
  status.json   {generated_at, apps:[{name,url,up,latency_ms,p95_ms_24h,availability_24h,availability_7d,
                 availability_30d,error_budget_left_pct,tls_days_left,last_error,incidents_7d}], slo:{...}}
  status.html   static, self-contained status page
  badge-<app>.json  shields.io endpoint badges ("uptime 7d")
When STATUS_INGEST_URL is set, status.json is POSTed there with the bearer token read from
$CREDENTIALS_DIRECTORY/status-ingest-token (systemd LoadCredential) or STATUS_INGEST_TOKEN.
State changes (UP->DOWN after 2 consecutive DOWN verdicts, DOWN->UP) are logged as JSON and, if ALERT_WEBHOOK_URL
is set, POSTed there as {"text": ...} (Slack/Mattermost/Telegram-bridge compatible).

Accuracy rules (a single probe host on an international link must not invent outages):
  1. A failed external probe is retried once after RETRY_DELAY (5 s) before it counts as failed.
  2. If (nearly) every app fails from the external vantage in the same run with a NETWORK error (timeout, TLS or
     connection failure, i.e. no HTTP answer) - at least max(2, 75 %) of the apps and no app answering with an HTTP
     error - the run is classified `vantage_unreachable` (probe-side / network problem): those failures are listed as
     a note and excluded from app availability. HTTP errors (5xx, wrong content) always count against the app.
  3. Second vantage inside Iran: Negahban's own orchestrator probes every app from Liara every 60 s and exposes the
     latest results at INTERNAL_VANTAGE_URL (bearer = the status-ingest token). Per app and run:
        external ok                      -> up
        external fail, internal ok       -> degraded_external_only (counted as available, shown separately)
        external fail, internal fail     -> down
        external fail, internal unknown  -> down, unless rule 2 applies (then vantage_unreachable, not counted)
     Internal results older than INTERNAL_MAX_AGE (180 s) are "unknown".
Raw results of both vantages are kept per row; availability is computed from the verdicts. Rows written before the
two-vantage rules (verdict NULL) keep their raw value, except runs that match rule 2, which are reclassified.
"""
from __future__ import annotations

import argparse
import html
import http.client
import concurrent.futures
import json
import math
import os
import re
import socket
import sqlite3
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve()
for cand in (HERE.parents[2] / "infra", HERE.parents[1] / "infra", Path.cwd() / "infra"):
    if (cand / "liara_api.py").exists():
        sys.path.insert(0, str(cand))
        break
from liara_api import load_manifest, redact  # noqa: E402

DOWN_AFTER = 2
RETRY_DELAY = float(os.getenv("PROBE_RETRY_DELAY", "5"))
INTERNAL_MAX_AGE = 180
AVAILABLE = ("up", "degraded_external_only")


def vantage_failed(results: list[tuple[bool, int]]) -> bool:
    """Rule 2 over one run's external results [(ok, http_status)]: network-only failures on (nearly) every app."""
    n = len(results)
    failed = [st for ok, st in results if not ok]
    return n >= 2 and len(failed) >= max(2, math.ceil(0.75 * n)) and all(not st for st in failed)
RETENTION_DAYS = 35


def now() -> float:
    return time.time()


def jlog(event: str, **kw) -> None:
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "level": kw.pop("level", "info"),
                      "app": "meow-monitor", "event": event, **kw}, ensure_ascii=False), flush=True)


def db(state: Path) -> sqlite3.Connection:
    state.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(state / "probes.db", timeout=30)
    con.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS probes(ts REAL, app TEXT, ok INTEGER, status INTEGER, latency_ms REAL, error TEXT);
      CREATE INDEX IF NOT EXISTS probes_app_ts ON probes(app, ts);
      CREATE TABLE IF NOT EXISTS tls(app TEXT PRIMARY KEY, days_left REAL, not_after TEXT, checked REAL);
      CREATE TABLE IF NOT EXISTS state(app TEXT PRIMARY KEY, up INTEGER, since REAL, fails INTEGER);
      CREATE TABLE IF NOT EXISTS runs(ts REAL PRIMARY KEY, kind TEXT, detail TEXT);
    """)
    cols = {r[1] for r in con.execute("pragma table_info(probes)")}
    for col, ddl in (("tries", "INTEGER"), ("int_ok", "INTEGER"), ("int_status", "INTEGER"), ("int_latency_ms", "REAL"),
                     ("int_checked", "REAL"), ("int_error", "TEXT"), ("verdict", "TEXT"), ("counted", "INTEGER")):
        if col not in cols:
            con.execute(f"alter table probes add column {col} {ddl}")
    # Legacy rows (written before the two-vantage rules; tries IS NULL) are (re)classified idempotently on every open:
    # raw results are kept, verdicts follow the raw value, and runs that match rule 2 become vantage_unreachable.
    legacy: dict[float, list] = {}
    for ts, ok, st in con.execute("select ts, ok, coalesce(status, 0) from probes where tries is null"):
        legacy.setdefault(ts, []).append((bool(ok), st))
    for ts, res in legacy.items():
        if vantage_failed(res):
            con.execute("update probes set verdict=case when ok=1 then 'up' else 'vantage_unreachable' end, "
                        "counted=ok where ts=? and tries is null", (ts,))
            con.execute("insert or ignore into runs values(?, 'vantage_unreachable', 'reclassified legacy run')", (ts,))
        else:
            con.execute("update probes set verdict=case when ok=1 then 'up' else 'down' end, counted=1 "
                        "where ts=? and tries is null", (ts,))
    con.commit()
    return con


def targets(manifest: dict) -> list[dict]:
    out = []
    for a in manifest.get("apps", []):
        slo = a.get("slo") or {}
        path = slo.get("probePath", "/")
        checks = (a.get("smoke") or {}).get("checks", []) if isinstance(a.get("smoke"), dict) else (a.get("smoke") or [])
        chk = next((c for c in checks if c.get("path") == path), {"path": path})
        out.append({"name": a["name"], "url": (a.get("url") or f"https://{a['name']}.liara.run").rstrip("/"),
                    "check": chk, "slo": {"availability": float(slo.get("availability", 99.5)),
                                          "p95_ms": float(slo.get("p95LatencyMs", 1500))}})
    return out


def json_subset(want, got) -> bool:
    if isinstance(want, dict):
        return isinstance(got, dict) and all(k in got and json_subset(v, got[k]) for k, v in want.items())
    return want == got


def timed_get(url: str, timeout: float = 10.0) -> dict:
    """One GET with a phase breakdown (dns, connect, tls, ttfb, total in ms) and the served certificate's expiry.

    DNS is timed separately and excluded from the latency SLO: resolver hiccups on the probe host are not
    user-visible latency of the app."""
    u = urlparse(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    t0 = time.monotonic()
    try:  # IPv4 first: AAAA lookups on the probe host's resolver stall for ~1.8 s
        addr = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)[0][4][:2]
    except socket.gaierror:
        addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][:2]
    t_dns = time.monotonic()
    sock = socket.create_connection(addr, timeout=timeout)
    t_conn = time.monotonic()
    not_after = None
    try:
        if u.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            not_after = sock.getpeercert().get("notAfter")
        t_tls = time.monotonic()
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: meow-monitor/1.0 (+synthetic)\r\n"
                      "Accept-Encoding: identity\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n").encode())
        resp = http.client.HTTPResponse(sock)
        resp.begin()
        t_ttfb = time.monotonic()
        body = resp.read(100_000)
        t_end = time.monotonic()
    finally:
        sock.close()

    def ms(a, b):
        return round((b - a) * 1000, 1)

    return {"status": resp.status, "body": body, "not_after": not_after,
            "timing": {"dns": ms(t0, t_dns), "connect": ms(t_dns, t_conn), "tls": ms(t_conn, t_tls),
                       "ttfb": ms(t_tls, t_ttfb), "total": ms(t0, t_end), "latency": ms(t_dns, t_end)}}


def probe_http(t: dict, timeout: float = 10.0) -> tuple[bool, int, float, str, dict]:
    """Returns (ok, status, latency_ms excluding DNS, error, details)."""
    chk = t["check"]
    try:
        r = timed_get(t["url"] + chk.get("path", "/"), timeout)
    except Exception as e:  # noqa: BLE001 - any failure is a failed probe
        return False, 0, timeout * 1000, (type(e).__name__ + ": " + str(e))[:200], {}
    status, body, ms = r["status"], r["body"], r["timing"]["latency"]
    want = int(chk.get("expectStatus", 200))
    err = ""
    if status != want:
        err = f"HTTP {status}"
    elif chk.get("contains") and chk["contains"].encode() not in body:
        err = "content check failed"
    elif "expectJson" in chk:
        try:
            if not json_subset(chk["expectJson"], json.loads(body)):
                err = "JSON check failed"
        except ValueError:
            err = "body is not JSON"
    return not err, status, ms, err, r


def percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, math.ceil(p / 100 * len(vals)) - 1))  # nearest-rank
    return vals[k]


def stats(con: sqlite3.Connection, app: str, slo: dict) -> dict:
    t = now()

    def avail(sec):
        n, ok = con.execute("select count(*), coalesce(sum(verdict in ('up','degraded_external_only')),0) from probes "
                            "where app=? and ts>=? and counted=1", (app, t - sec)).fetchone()
        return (round(100.0 * ok / n, 3) if n else None), n

    a1h, _ = avail(3600)
    a24, n24 = avail(86400)
    a7, _ = avail(7 * 86400)
    a30, n30 = avail(30 * 86400)
    lat = [r[0] for r in con.execute("select latency_ms from probes where app=? and ts>=? and ok=1", (app, t - 86400))]
    lat_int = [r[0] for r in con.execute("select int_latency_ms from probes where app=? and ts>=? and int_ok=1 "
                                         "and int_latency_ms is not null", (app, t - 86400))]
    p95 = percentile(lat, 95)
    budget = None
    if a30 is not None:
        allowed = 100 - slo["availability"]
        budget = round(100 * (1 - (100 - a30) / allowed), 1) if allowed > 0 else None
    # incidents: maximal runs of failed probes in the last 7 days
    incidents, start, last_fail = [], None, None
    for ts, ok, err in con.execute("select ts, verdict in ('up','degraded_external_only'), coalesce(error, int_error) "
                                   "from probes where app=? and ts>=? and counted=1 order by ts", (app, t - 7 * 86400)):
        if not ok and start is None:
            start, last_fail = ts, (ts, err)
        elif not ok:
            last_fail = (ts, err)
        elif ok and start is not None:
            incidents.append({"start": iso(start), "end": iso(ts), "minutes": round((ts - start) / 60, 1), "error": last_fail[1]})
            start = None
    if start is not None:
        incidents.append({"start": iso(start), "end": None, "minutes": round((t - start) / 60, 1), "error": last_fail[1]})
    last = con.execute("select ts, ok, status, latency_ms, error, verdict, int_ok, int_status, int_latency_ms, int_checked, int_error, tries "
                       "from probes where app=? order by ts desc limit 1", (app,)).fetchone()
    tls = con.execute("select days_left, not_after from tls where app=?", (app,)).fetchone()
    st = con.execute("select up, since from state where app=?", (app,)).fetchone()
    return {"up": bool(st[0]) if st else (bool(last[1]) if last else None), "since": iso(st[1]) if st else None,
            "checked_at": iso(last[0]) if last else None, "status": last[2] if last else None,
            "latency_ms": round(last[3]) if last else None, "last_error": (last[4] or None) if last else None,
            "availability_1h": a1h, "availability_24h": a24, "availability_7d": a7, "availability_30d": a30,
            "samples_24h": n24, "samples_30d": n30, "p95_ms_24h": round(p95) if p95 is not None else None,
            "slo_met": (a30 is None or a30 >= slo["availability"]) and (p95 is None or p95 <= slo["p95_ms"]),
            "error_budget_left_pct": budget, "tls_days_left": round(tls[0], 1) if tls else None,
            "tls_not_after": tls[1] if tls else None, "incidents_7d": incidents[-10:],
            "verdict": last[5] if last else "unknown",
            "vantages": {
                "external": {"vantage": "hetzner-helsinki", "ok": bool(last[1]), "status": last[2], "latency_ms": round(last[3]),
                             "checked_at": iso(last[0]), "error": last[4] or None, "tries": last[11]} if last else None,
                "internal": ({"vantage": "liara-internal", "ok": bool(last[6]), "status": last[7],
                              "latency_ms": round(last[8]) if last[8] is not None else None, "checked_at": iso(last[9]),
                              "error": last[10] or None} if last and last[6] is not None else None)},
            "p95_ms_24h_internal": round(percentile(lat_int, 95)) if lat_int else None}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def alert(text: str) -> None:
    url = os.getenv("ALERT_WEBHOOK_URL")
    if not url:
        return
    try:
        req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001
        jlog("alert.failed", level="warn", error=redact(str(e)))


def probe_with_retry(x: dict) -> tuple[bool, int, float, str, dict, int]:
    ok, status, ms, err, det = probe_http(x)
    if ok:
        return ok, status, ms, err, det, 1
    time.sleep(RETRY_DELAY)
    ok2, status2, ms2, err2, det2 = probe_http(x)
    if ok2:
        return ok2, status2, ms2, err2, det2, 2
    return False, status2, ms2, err2 or err, det2 or det, 2


def ingest_token() -> str | None:
    cred = os.getenv("CREDENTIALS_DIRECTORY")
    if cred and (Path(cred) / "status-ingest-token").exists():
        return (Path(cred) / "status-ingest-token").read_text().strip() or None
    return os.getenv("STATUS_INGEST_TOKEN")


def internal_vantage(names: list[str]) -> dict:
    """Latest in-Liara probe results from Negahban, {app: {...}}; empty when unavailable or stale."""
    url = os.getenv("INTERNAL_VANTAGE_URL")
    token = ingest_token()
    if not url or not token:
        return {}
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                                                   "User-Agent": "meow-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            doc = json.loads(r.read(200_000))
    except Exception as e:  # noqa: BLE001
        jlog("vantage.internal_unavailable", level="warn", error=redact(str(e))[:200])
        return {}
    out, t = {}, now()
    for a in doc.get("apps", []) if isinstance(doc, dict) else []:
        name = a.get("name")
        if name not in names:
            continue
        try:
            checked = datetime.fromisoformat(str(a.get("checked_at")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if t - checked > INTERNAL_MAX_AGE:
            continue
        out[name] = {"ok": bool(a.get("ok")), "status": a.get("status"), "latency_ms": a.get("latency_ms"),
                     "checked": checked, "error": a.get("error")}
    return out


def verdict_for(ext_ok: bool, internal: dict | None, all_external_failed: bool) -> str:
    if ext_ok:
        return "up"
    if internal is not None:
        return "degraded_external_only" if internal["ok"] else "down"
    return "vantage_unreachable" if all_external_failed else "down"


def run(manifest: dict, state: Path, push: bool) -> dict:
    con = db(state)
    tg = targets(manifest)
    t = now()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(tg))) as pool:
        results = dict(zip([x["name"] for x in tg], pool.map(probe_with_retry, tg), strict=True))
    internal = internal_vantage([x["name"] for x in tg])
    all_failed = vantage_failed([(r[0], r[1]) for r in results.values()])
    if all_failed:
        con.execute("insert or replace into runs values(?, 'vantage_unreachable', ?)",
                    (t, "network failures on (nearly) all apps from hetzner-helsinki" + ("" if not internal else "; internal vantage reported")))
        jlog("vantage.unreachable", level="warn", vantage="hetzner-helsinki", apps=len(tg))
    for x in tg:
        ok, status, ms, err, det, tries = results[x["name"]]
        iv = internal.get(x["name"])
        verdict = verdict_for(ok, iv, all_failed)
        counted = 0 if verdict == "vantage_unreachable" else 1
        con.execute("insert into probes(ts, app, ok, status, latency_ms, error, tries, int_ok, int_status, int_latency_ms, "
                    "int_checked, int_error, verdict, counted) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t, x["name"], int(ok), status, ms, err, tries,
                     None if iv is None else int(iv["ok"]), None if iv is None else iv["status"],
                     None if iv is None else iv["latency_ms"], None if iv is None else iv["checked"],
                     None if iv is None else iv["error"], verdict, counted))
        jlog("probe", app_name=x["name"], ok=ok, status=status, duration_ms=round(ms), error=err or None, tries=tries,
             internal_ok=None if iv is None else iv["ok"], verdict=verdict, timing=det.get("timing"),
             level="info" if verdict in AVAILABLE else "warn")
        if det.get("not_after"):
            exp = ssl.cert_time_to_seconds(det["not_after"])
            days = (exp - t) / 86400
            con.execute("insert or replace into tls values(?,?,?,?)", (x["name"], days, iso(exp), t))
            if days < 14:
                jlog("tls.expiring", level="warn", app_name=x["name"], days_left=round(days, 1))
        if not counted:
            continue  # a vantage problem says nothing about the app: no state change
        row = con.execute("select up, since, fails from state where app=?", (x["name"],)).fetchone()
        up, since, fails = (row if row else (1, t, 0))
        bad = verdict == "down"
        fails = fails + 1 if bad else 0
        if up and fails >= DOWN_AFTER:
            up, since = 0, t
            jlog("app.down", level="error", app_name=x["name"], error=err, internal_error=None if iv is None else iv["error"])
            alert(f"DOWN {x['name']} ({x['url']}): {err}" + ("" if iv is None else " (confirmed from inside Liara)"))
        elif not up and not bad:
            jlog("app.up", app_name=x["name"], downtime_min=round((t - since) / 60, 1))
            alert(f"RECOVERED {x['name']} after {round((t - since) / 60, 1)} min")
            up, since = 1, t
        con.execute("insert or replace into state values(?,?,?,?)", (x["name"], up, since, fails))
    con.execute("delete from probes where ts < ?", (t - RETENTION_DAYS * 86400,))
    con.execute("delete from runs where ts < ?", (t - RETENTION_DAYS * 86400,))
    con.commit()
    doc = render(con, tg, state)
    con.close()
    if push:
        push_status(doc)
    return doc


def vantage_notes(con: sqlite3.Connection, days: int = 7) -> list[dict]:
    """Merge consecutive vantage_unreachable runs into periods."""
    notes, cur = [], None
    for (ts,) in con.execute("select ts from runs where kind='vantage_unreachable' and ts>=? order by ts", (now() - days * 86400,)):
        if cur and ts - cur["_last"] <= 150:
            cur["_last"] = ts
            cur["runs"] += 1
        else:
            cur = {"kind": "vantage_unreachable", "vantage": "hetzner-helsinki", "_start": ts, "_last": ts, "runs": 1}
            notes.append(cur)
    return [{"kind": n["kind"], "vantage": n["vantage"], "start": iso(n["_start"]), "end": iso(n["_last"]), "runs": n["runs"],
             "minutes": round((n["_last"] - n["_start"]) / 60 + 1, 1),
             "note": "(nearly) every app timed out from the probe host at once (network / probe-side); excluded from availability"}
            for n in notes]


def render(con: sqlite3.Connection, tg: list[dict], state: Path) -> dict:
    slo = {"availability": min(x["slo"]["availability"] for x in tg) if tg else 99.5,
           "p95_ms": max(x["slo"]["p95_ms"] for x in tg) if tg else 1500, "window": "30d rolling",
           "probe": "HTTPS GET every 60 s from Hetzner Helsinki (outside Liara), plus Negahban's in-Liara probes",
           "rules": "failed probes are retried once after 5 s; an app counts as down only when the external probe fails and "
                    "the in-Liara probe also fails (or is unavailable); runs where (nearly) every app fails from the probe host "
                    "with network errors are vantage_unreachable and excluded"}
    apps = []
    for x in tg:
        apps.append({"name": x["name"], "url": x["url"], "probe_path": x["check"].get("path", "/"),
                     "slo": x["slo"], **stats(con, x["name"], x["slo"])})
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "apps": apps, "slo": slo,
           "notes": vantage_notes(con),
           "overall": "operational" if all(a["up"] for a in apps) else ("outage" if not any(a["up"] for a in apps) else "degraded")}
    pub = state / "public"
    pub.mkdir(parents=True, exist_ok=True)
    write_atomic(pub / "status.json", json.dumps(doc, ensure_ascii=False, indent=1))
    write_atomic(pub / "status.html", page(doc))
    for a in apps:
        v = a["availability_7d"]
        color = "lightgrey" if v is None else ("brightgreen" if v >= a["slo"]["availability"] else ("yellow" if v >= 99 else "red"))
        write_atomic(pub / f"badge-{re.sub(r'[^A-Za-z0-9_.-]', '_', a['name'])}.json", json.dumps(
            {"schemaVersion": 1, "label": "uptime 7d", "message": "no data" if v is None else f"{v:.2f}%", "color": color}))
    return doc


def write_atomic(p: Path, text: str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def page(doc: dict) -> str:
    e = html.escape

    def pct(v):
        return "-" if v is None else f"{v:.2f}%"

    def vant(v):
        if not v:
            return '<span class="mut">n/a</span>'
        txt = "ok" if v["ok"] else (v.get("error") or f"HTTP {v.get('status')}")
        ms = f" {v['latency_ms']} ms" if v.get("ok") and v.get("latency_ms") is not None else ""
        return f'<span class="{"okt" if v["ok"] else "badt"}">{e(str(txt)[:40])}{ms}</span>'

    rows = []
    for a in doc["apps"]:
        state = "up" if a["up"] else ("down" if a["up"] is False else "unknown")
        vs = a.get("vantages") or {}
        rows.append(f"""<tr><td><span class="dot {state}"></span><a href="{e(a['url'])}">{e(a['name'])}</a></td>
<td>{e(state.upper())}</td><td>{vant(vs.get('external'))}</td><td>{vant(vs.get('internal'))}</td><td>{pct(a['availability_24h'])}</td><td>{pct(a['availability_7d'])}</td><td>{pct(a['availability_30d'])}</td>
<td>{'-' if a['p95_ms_24h'] is None else str(a['p95_ms_24h']) + ' ms'}</td><td>{'met' if a['slo_met'] else 'MISSED'}</td>
<td>{'-' if a['error_budget_left_pct'] is None else str(a['error_budget_left_pct']) + '%'}</td>
<td>{'-' if a['tls_days_left'] is None else str(a['tls_days_left']) + ' d'}</td></tr>""")
    notes = "".join(f"<li>{e(n['start'])} &rarr; {e(n['end'])} ({n['minutes']} min): probe host could not reach any app "
                    f"(<code>{e(n['kind'])}</code>, {e(n['vantage'])}); not counted against app availability.</li>"
                    for n in doc.get("notes", [])) or "<li>None in the last 7 days.</li>"
    inc = "".join(f"<li><b>{e(a['name'])}</b> {e(i['start'])} &rarr; {e(i['end'] or 'ongoing')} ({i['minutes']} min) {e(i['error'] or '')}</li>"
                  for a in doc["apps"] for i in a["incidents_7d"]) or "<li>No incidents in the last 7 days.</li>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeowMeow Status</title><meta http-equiv="refresh" content="60">
<style>:root{{--bg:#fff;--fg:#1d2433;--mut:#667085;--line:#e4e7ec;--ok:#12b76a;--bad:#f04438;--unk:#98a2b3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1219;--fg:#e6e9ef;--mut:#98a2b3;--line:#262b36}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}main{{max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}}.mut{{color:var(--mut)}}.banner{{padding:12px 16px;border-radius:10px;margin:16px 0;font-weight:600}}
.operational{{background:#12b76a22}}.degraded{{background:#f7900922}}.outage{{background:#f0443822}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:860px}}th,td{{text-align:left;padding:8px;border-bottom:1px solid var(--line)}}
th{{font-size:12px;text-transform:uppercase;color:var(--mut)}}a{{color:inherit}}.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px}}
.okt{{color:var(--ok)}}.badt{{color:var(--bad)}}.up{{background:var(--ok)}}.down{{background:var(--bad)}}.unknown{{background:var(--unk)}}</style></head>
<body><main><h1>MeowMeow platform status</h1><div class="mut">Generated {e(doc['generated_at'])} &middot; {e(doc['slo']['probe'])}</div>
<div class="banner {e(doc['overall'])}">All systems: {e(doc['overall'])}</div>
<div class="wrap"><table><thead><tr><th>App</th><th>Now</th><th>External</th><th>In Liara</th><th>24 h</th><th>7 d</th><th>30 d</th>
<th>p95 (24 h)</th><th>SLO</th><th>Error budget</th><th>TLS</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="mut">SLO: availability &ge; {doc['slo']['availability']}% and p95 latency &le; {int(doc['slo']['p95_ms'])} ms
over a {e(doc['slo']['window'])} window.</p>
<p class="mut">{e(doc['slo'].get('rules', ''))}</p>
<h2>Incidents (7 days)</h2><ul>{inc}</ul>
<h2>Probe-side notes (7 days)</h2><ul>{notes}</ul></main></body></html>"""


def push_status(doc: dict) -> None:
    url = os.getenv("STATUS_INGEST_URL")
    if not url:
        return
    token = os.getenv("STATUS_INGEST_TOKEN")
    cred = os.getenv("CREDENTIALS_DIRECTORY")
    if cred and (Path(cred) / "status-ingest-token").exists():
        token = (Path(cred) / "status-ingest-token").read_text().strip()
    if not token:
        jlog("status.push_skipped", level="warn", error="no ingest token")
        return
    body = json.dumps(doc, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            jlog("status.pushed", status=r.status, bytes=len(body))
    except Exception as e:  # noqa: BLE001
        jlog("status.push_failed", level="warn", error=redact(str(e))[:200])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "report", "render"])
    ap.add_argument("-f", "--file", default=os.getenv("MONITOR_MANIFEST", "infra/liara.yaml"))
    ap.add_argument("--state", default=os.getenv("STATE_DIRECTORY", "/var/lib/meow-monitor"))
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args(argv)
    manifest = load_manifest(a.file)
    state = Path(a.state)
    if a.cmd == "run":
        doc = run(manifest, state, not a.no_push)
        return 0 if doc["overall"] == "operational" else 1
    con = db(state)
    doc = render(con, targets(manifest), state)
    con.close()
    if a.cmd == "report":
        print(f"{'app':18} {'now':5} {'24h':>8} {'7d':>8} {'30d':>8} {'p95':>7} {'budget':>7} {'tls':>6}")
        for x in doc["apps"]:
            f = lambda v: "-" if v is None else f"{v:.2f}%"  # noqa: E731
            print(f"{x['name']:18} {('UP' if x['up'] else 'DOWN'):5} {f(x['availability_24h']):>8} {f(x['availability_7d']):>8} "
                  f"{f(x['availability_30d']):>8} {str(x['p95_ms_24h'] or '-'):>7} {str(x['error_budget_left_pct'] or '-'):>7} "
                  f"{str(x['tls_days_left'] or '-'):>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
