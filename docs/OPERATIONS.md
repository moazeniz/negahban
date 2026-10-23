# Operations

Runbook for keeping `negahban-sre` (and the apps it watches) healthy. Deployment steps are in
[DEPLOYMENT.md](DEPLOYMENT.md). All `liara_cli.py` commands need `LIARA_ENV_FILE` (or the env vars) set.

## 1. Health signals

| Signal | How to check | Healthy |
|---|---|---|
| App up | `curl -fsS https://negahban-sre.liara.run/healthz` | HTTP 200; orchestrator lag well below 2 × `ORCH_INTERVAL` |
| Current release | `python3 tools/liara_cli.py releases negahban-sre 1` | `READY` |
| Container events | `get "/v1/projects/negahban-sre/events?start=<unix-1h>"` | no `oom`, `die`, `kill` without a deploy |
| CPU / memory | `get /v1/projects/negahban-sre/metrics/summary` | memory < 80 % of plan |
| Disk | `disksUsage` in the metrics summary | `data` usage < 80 % |
| Account credit | `get /v1/billing` (or dashboard balance alarm) | enough for several days at current hourly rate |
| LLM | dashboard chat answers; no repeated BoxAPI errors in logs | — |

## 2. Monitoring and alerts

Liara has **no webhooks or app alerts** (only account notifications for credit, SSL, backups). Therefore:

- **Negahban watches the other apps** (orchestrator, every `ORCH_INTERVAL` seconds) and raises incidents in the dashboard.
- **Something external must watch Negahban.** Use an external watchdog (cron on any host, or an uptime monitor) that:
  1. polls `/healthz` every minute;
  2. sends `POST /api/watchdog/ping` with `WATCHDOG_TOKEN` (authenticated liveness);
  3. after N consecutive failures restarts the app: `python3 tools/liara_cli.py post /v1/projects/negahban-sre/actions/restart`.
- Enable Liara **account notifications** (email/SMS/Telegram) for low credit — a frozen account stops every app.

Suggested alert thresholds:

| Alert | Threshold | Action |
|---|---|---|
| `/healthz` failing | 3 consecutive minutes | restart; if it persists, roll back (DEPLOYMENT §6) |
| Orchestrator lag | > 3 × `ORCH_INTERVAL` | check logs for a stuck Liara/BoxAPI call; restart |
| OOM events | any | resize plan up; investigate memory growth |
| Disk `data` | > 80 % | prune archives or grow disk (`post /v1/projects/negahban-sre/disks/data/resize '{"size":N}'`) |
| Credit | < 3 days of run-rate | top up |

## 3. Logs

```bash
liara app:logs --app negahban-sre -f
python3 tools/liara_cli.py get "/v2/projects/negahban-sre/logs?start=$(date -d '-30 min' +%s)&search=Traceback"
```

The app does not log secrets (values are redacted before logging and before SSE). Access logs are disabled in the
container (`--no-access-log`); request-level detail comes from the app's own events/audit log in the dashboard.

## 4. Routine tasks

| Frequency | Task |
|---|---|
| Daily | Check dashboard incidents and pending approvals; check credit. |
| Weekly | Review the audit log for sensitive actions; check disk usage; verify a backup exists (Silver/Gold). |
| Per release | Run `make test`, deploy, verify `/healthz`, note release ID for rollback. |
| Per key rotation | Update the full env set (`update-envs` replaces everything) and redeploy env; confirm chat works. |

## 5. Safety switches

| Switch | Effect |
|---|---|
| `LIARA_DRY_RUN=1` | No write call reaches Liara (read-only observer). |
| `ORCH_ENABLED=0` | Stops background monitoring/autopilot; manual dashboard still works. |
| Autopilot policy (dashboard) | Per-app plan range, daily toman cap, cooldowns; remove the policy to require approval for everything. |
| App write/env locks (dashboard) | Hard-block changes to a specific app (e.g. production apps of other teams). |
| Scale to 0 | `post /v1/projects/negahban-sre/actions/scale '{"scale":0}'` stops the agent entirely. |

## 6. On-call checklist

1. **Is it Negahban or Liara?** Open `https://negahban-sre.liara.run/healthz`, then the Liara console status of the app.
2. **Recent change?** `releases negahban-sre 5` — if the failure started with the newest release, roll back first,
   investigate second.
3. **Events:** look for `oom` / `die` / restart loops → resize or roll back.
4. **Logs:** search for `Traceback` and for Liara HTTP errors (401 token, 400 `frozen_project`, 428 quota).
5. **Credit:** a frozen account looks like every app failing at once.
6. **Contain:** if the agent is acting wrongly, set `ORCH_ENABLED=0` (or `LIARA_DRY_RUN=1`) via env update, or scale it to 0.
7. **Communicate and record:** note the incident, release IDs and actions in `CHANGELOG.md` / the team channel.

## 7. Capacity and cost

Prices are toman per hour (`GET /v1/me` → `plans.projects[plan].price`, bundle extra in
`plans.projectBundlePlans`). Resizing causes ~20 s of 502s on bundles without zero-downtime. Keep `demo-shop` scaled
to 0 when no demo is running.

<!-- platform:begin (maintained by the platform kit) -->
## Platform operations: SLOs, backups, runbooks

### SLOs and monitoring

**SLOs** (30-day rolling): availability **≥ 99.5 (demo-shop 99.0) %** and p95 latency **< 1.5 s** on `/healthz`.

**Monitoring (live).** `meow-monitor.timer` on Hetzner (Helsinki) probes every 60 s:

- status, content, latency with DNS excluded, and TLS expiry;
- a failed probe is retried once after 5 s;
- the result is cross-checked with Negahban's in-Liara probes, and the app counts as down only when both
  vantages fail;
- a run where every app fails from Helsinki is a probe-side `vantage_unreachable` note, not an outage.

**Alerts.** An app is marked DOWN after 2 consecutive down verdicts; alerts go to a JSON log and an optional
webhook.

**Status page:** https://negahban-sre.liara.run/status shows both vantages for each app.

**Error-budget policy:**

- While more than 50 % of the budget remains, deploy freely.
- Below 50 %, deploy only with a reviewer.
- When the budget is exhausted, freeze features and allow only fixes and rollbacks.

### Backups and restore

The `data` disk holds Negahban's SQLite state (approvals, incidents, settings). The bundle is basic, so there are no native Liara backups. `meow-backup.timer` (Hetzner, daily) exports and verifies it. `demo-shop` has no disk. The tooling lives in this repo: `ops/backup/liara_backup.py`, `ops/systemd/meow-backup.{service,timer}`. **Status: ready, not enabled.** Enable with `systemctl enable --now meow-backup.timer`. The restore procedure is in [PLATFORM.md §8](PLATFORM.md#8-backups-and-restore).

### Runbooks

Full commands: [PLATFORM.md §10](PLATFORM.md#10-runbooks-top-failure-modes). Shortest paths for Negahban:

| # | Symptom | First action |
|---|---|---|
| R1 | Errors right after a release / Deploy exit 3 | `python3 infra/liara_deploy.py rollback --manifest infra/liara.yaml --app negahban-sre` then `... smoke` |
| R2 | Probe DOWN, 502/503, crash loop | Read events (`oom`/`die`) and logs (`GET /v2/projects/negahban-sre/logs`), then `POST /v1/projects/negahban-sre/actions/restart`. For OOM, raise `plan` within `scaling.max`, then `liara_apply.py plan` / `apply` |
| R3 | Broke after an env change | `liara_apply.py plan --app negahban-sre` (missing names), then roll back to the previous ENV_UPDATE release (`rollback --to <id>`) |
| R4 | LLM errors / timeouts | Keys rotate automatically; switch `LLM_PROVIDER`; Arvan gate kill switch `sudo touch /etc/nginx/meow-llm.disabled` |
| R5 | Disk full or data damaged | Read `metrics/summary` → `disksUsage`; grow `sizeGB` and apply; or scale 0, restore (above), scale 1 |
| R6 | `frozen_project` / low credit | `liara_apply.py plan` (runway), top up, `POST /v1/projects/negahban-sre/unfreeze` |

### On-call checklist (platform)

1. Check the status page and the last CI/Deploy run.
2. Check `liara_deploy.py status`. A release in the last hour points to R1.
3. Mitigate first (rollback / restart / provider switch), then debug.
4. Verify with `liara_deploy.py smoke` and confirm the probe is UP.
5. File an *Incident / postmortem* issue: UTC timeline, cause, actions.
<!-- platform:end -->
