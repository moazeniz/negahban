# How Negahban uses the Liara API

All calls go through `negahban/liara.py`, an async `httpx` client:

- **Base URL**: `https://api.iran.liara.ir`. This is the Iran edge; the Liara console uses it for metrics and logs.
- **Authentication**: `Authorization: Bearer $LIARA_API_TOKEN`, plus `?teamID=$LIARA_TEAM_ID` on every request.
- **Timeouts**: 30 s, or 300 s for uploads.
- **Retries**: GET requests are retried 3 times on network errors and 5xx. A POST is retried only when the connection failed before the request was sent.
- **Errors**: every error becomes a `LiaraError` with the Liara `data.code`, for example `max_deployment_count_in_day`, and a Persian explanation.
- **Secrets**: the token is redacted from everything the client logs or returns.

The DNS service uses `https://dns-service.liara.ir/api/v1`.

Legend:
- ✅ verified live on 2026-10-22 against team apps `demo-shop`, `negahban-sre`, `bazargard-ai` and `meowmeow-wallex`.
- ◻ implemented from the spec or the CLI, but not exercised live.

## Endpoints

| Area | Method and path | Used for | |
|---|---|---|---|
| Account | `GET /v1/me` | Plan catalogue (`plans.projects[planID].price`, toman per **hour**) and bundle prices and limits (`plans.projectBundlePlans[plan][basic\|standard\|pro]`) | ✅ |
| Billing | `GET /v1/billing` | Team balance (`team.balance`) | ✅ |
| Billing | `GET /v1/usage-report` | Hourly and monthly run-rate per app (cost pie chart) | ✅ |
| Apps | `GET /v1/projects` | Discover the team's apps (dynamic registry) | ✅ |
| Apps | `GET /v1/projects/{name}` | Plan, scale, status, `zeroDowntime`, envs. **Values come back in clear text**, so they are masked before they reach the UI or the LLM | ✅ |
| Apps | `POST /v1/projects` `{name, planID, bundlePlanID, platform, network}` | Create an app (new-project wizard). g2 plans need a private network | ✅ (demo-shop) |
| Apps | `DELETE /v1/projects/{name}` | Delete an app. Guarded: the owner must type the name on the card | ✅ (scratch app `ngh-accept-api` deleted through the card) |
| Apps | `POST /v1/projects/{_id}/default-subdomain/{enable\|disable}` | Turn the free `<app>.liara.run` address on or off (`set_default_subdomain`) | ◻ |
| Exec | `wss://api.iran.liara.ir/v1/exec?token&cmd&project_id&teamID`, first frame `{"action":"start","cols","rows"}` | Container shell for diagnostics (allow-list / approval / block-list). **The gateway refuses sessions from Iranian networks**, including Liara's own runtime, so Negahban connects through the team relay outside Iran (`LIARA_EXEC_URL`); the relay adds the token. `cmd` is split on spaces and `+`. Each session appears as `exec_start` / `exec_die` events | ✅ via the relay |
| Ops | `POST /v1/projects/{name}/actions/restart` | Restart | ✅ |
| Ops | `POST /v1/projects/{name}/actions/scale` `{scale:0\|1}` | Turn on or off. Liara has **no replicas**: scale is only 0 or 1 | ✅ (watchdog, read path) |
| Ops | `POST /v1/projects/{name}/resize` `{planID}` | Vertical autoscaling (≈20 s of 502 without zero-downtime) | ✅ small-g2 → medium-g2 by autopilot |
| Ops | `POST /v1/projects/{_id}/zero-downtime/{enable\|disable}` | Zero-downtime toggle. Needs the standard or pro bundle | ◻ |
| Config | `POST /v1/projects/update-envs` `{project, variables:[{key,value}]}` | Set or restore an env var. **Replaces the whole list** and creates an `ENV_UPDATE` release (restart) | ✅ (missing-env scenario and fix) |
| Deploy | `POST /v2/projects/{name}/sources` (multipart `file`, gzip tar) → `sourceID` | Upload source | ✅ |
| Deploy | `POST /v2/projects/{name}/releases` `{sourceID, port, type:'docker', message, disks?, envs?, healthCheck?}` → `releaseID` | Create a release. When `envs` is omitted, the existing envs are kept (verified). `healthCheck.command` **must be an array** (e.g. `["CMD-SHELL","python healthcheck.py"]`); a string gives a 400. Durations are validated as **seconds**, e.g. `startPeriod` must be ≤ 3000 | ✅ (template deploy READY with a health check) |
| Deploy | `GET /v1/projects/{name}/releases?page=1&count=N` | History, `currentRelease`, `state`, `failReason` | ✅ |
| Deploy | `GET /v1/releases/{id}` | Poll release state (`BUILDING`, `DEPLOYING`, `READY`, `FAILED`, `UNHEALTHY`, `CANCELED`, `TIMEDOUT`) | ✅ |
| Deploy | `GET /v2/releases/{id}/build-logs?since=` | Build output. Paged by `since` = `createdAt` of the last line (exclusive). Used to find failed `pip install` lines | ✅ |
| Deploy | `POST /v2/releases/{id}/cancel` | Cancel a build | ◻ |
| Rollback | `POST /v1/releases/{id}/rollback` | Re-activate an earlier READY release. **Console-only endpoint, not in the public spec.** If it is refused, Negahban redeploys the archived source of the previous healthy release | ◻ (archive fallback implemented) |
| Logs | `GET /v2/projects/{name}/logs?start=&end=[&search=&releaseId=]` | Runtime logs, Loki-style. **At most 100 lines per call (the newest in the window)**, so Negahban pages backwards by moving `end`. Timestamps are nanosecond strings. `limit` and `since` are rejected with 400 | ✅ |
| Events | `GET /v1/projects/{name}/events?start=&end=` | Docker events (`create`, `start`, `die`, `kill`, `oom`, …) with `release` and `tag`. A second `start` of the same release means an unplanned restart; `oom` means the container ran out of memory | ✅ |
| Metrics | `GET /v1/projects/{name}/metrics/{cpu\|memory\|network-receive\|network-transmit}?since=<unix s>` | Time series per applet. During a deploy two applets overlap, so Negahban sums per timestamp. CPU is in cores, memory in bytes | ✅ |
| Metrics | `GET /v1/projects/{name}/metrics/summary` | Current CPU, RAM and disk snapshot | ✅ |
| Disks | `GET /v1/projects/{_id}/disks`, `POST /v1/projects/{name}/disks {name,size}` | State disk for Negahban (`data` → `/data`). **GET needs the project `_id`**: by name it returns 404 | ✅ |
| Domains | `GET /v1/domains?project=` | List domains and their status | ✅ |
| Domains | `POST /v1/domains` `{name, type:'PROJECT', project}` | Attach a custom domain | ◻ |
| Domains | `GET /v1/domains/{id}`, `GET /v1/domains/{id}/check`, `POST /v1/domains/provision-ssl-certs {domain}` | Check verification, then issue SSL | ◻ |
| DNS | `GET/POST /zones`, `PUT /zones/{zone}/check`, `GET/POST /zones/{zone}/dns-records` | Zones on Liara DNS (IRNIC nameserver flow) | ◻ |

## Quirks we hit and handle

1. **Logs are capped at 100 lines per request.** Without paging, a traceback is easily lost behind health-check noise. `Liara.logs(max_lines=…)` pages backwards and de-duplicates lines that share a boundary second.
2. **Metrics during a deploy** contain two applets (old and new container). A naive merge zig-zags, so values are summed per timestamp.
3. **Docker events include deploys.** `start`, `die` and `kill` happen on every release. Negahban counts only *repeated* starts of the same release as crashes (`restarts_from_events`).
4. **Env updates restart the app and replace the full list.** Negahban always sends the full current set plus the change. It keeps a private snapshot of the last-seen values on its disk, so a deleted key can be restored without anyone retyping a secret.
5. **No webhooks.** Detection is polling: every 60 s Negahban probes health and reads metrics, new log lines, events and the latest release state.
6. **Rollback is not in the public API.** The console endpoint is tried first. Negahban archives every source tarball it deploys (`/data/archive/<app>/`) as a guaranteed fallback.
7. **Bundle limits.** basic: 20 deploys/day, no zero-downtime, 5 min build. standard: 50/day and zero-downtime. The demo app uses the standard bundle for the deploy quota; Negahban uses basic.
8. **Resize means a brief outage.** Autopilot resizes have a 10 min cooldown, a daily budget cap and hysteresis: scale-up needs ≥ 80 % for 5 min, scale-down needs < 25 % CPU and < 45 % RAM for 30 min. This prevents flapping.
9. **Scale-to-zero apps can't be deployed.** Negahban checks `scale` before acting.
10. **Disk GET by name returns 404.** Use the project `_id`.
11. **Exec from Iran is refused.** Shell sessions only work from outside Iran, so Negahban uses a relay.
12. **healthCheck validation.** `command` must be an array, and the durations are in seconds (`startPeriod` ≤ 3000).
