# Deployment

Negahban runs in production as a **Docker app on Liara** (`negahban-sre`) with a persistent disk at `/data`.
The demo target `demo-shop` is a second, separate Liara app.

Commands below assume the repository root as the working directory and the synced application code
(`Dockerfile`, `negahban/`, `deploy/`). Verified Liara API behaviour is in
[LIARA_API_REFERENCE.md](LIARA_API_REFERENCE.md); section numbers below (§) refer to it.

- [1. Prerequisites](#1-prerequisites)
- [2. First-time setup on Liara](#2-first-time-setup-on-liara)
- [3. Deploy](#3-deploy)
- [4. Verify (health checks)](#4-verify-health-checks)
- [5. Upgrades](#5-upgrades)
- [6. Rollback](#6-rollback)
- [7. Backups and restore](#7-backups-and-restore)
- [8. Logs and metrics](#8-logs-and-metrics)
- [9. Demo target app](#9-demo-target-app-demo-shop)
- [Local Docker](#local-docker)
- [Troubleshooting](#troubleshooting)

---

## 1. Prerequisites

| Item | Notes |
|---|---|
| Liara account + team | API token from the console menu "API". Token is team-wide. Note the team ID. |
| Liara network | g2 plans need a private network (`LIARA_NETWORK_ID`). |
| Credit | Plans are billed **per hour in toman** (`GET /v1/me` → `plans`). Check balance first. |
| BoxAPI keys | 1–3 keys for `BOXAPI_KEYS`. |
| Operator host | Python 3.10+; optional Liara CLI (`npm i -g @liara/cli`). |

Create an operator env file once; it is used by `tools/liara_cli.py` and the deploy script:

```bash
install -m 600 /dev/null ~/.liara.env
cat >> ~/.liara.env <<'EOF'
LIARA_API_BASE=https://api.iran.liara.ir
LIARA_API_TOKEN=<liara-token>
LIARA_TEAM_ID=<team-id>
LIARA_NETWORK_ID=<network-id>
EOF
export LIARA_ENV_FILE=~/.liara.env
python3 tools/liara_cli.py get /v1/me | head -5     # 200 = token and team OK
```

## 2. First-time setup on Liara

1. **Create the app** (console, or API — plan IDs from `GET /v1/me`):

   ```bash
   python3 tools/liara_cli.py post /v1/projects \
     '{"name":"negahban-sre","planID":"<plan>","bundlePlanID":"<basic|standard>","platform":"docker","network":"<network-id>"}'
   ```

2. **Create the disk** for SQLite, archives and workspaces (2 GB is enough to start; disks can only grow):

   ```bash
   python3 tools/liara_cli.py post /v1/projects/negahban-sre/disks '{"name":"data","size":2}'
   ```

3. **Prepare the env set.** On Liara a release's `envs` (and `env:set` / `update-envs`) **replaces all variables**
   (§6), so always maintain the complete set in one place, e.g. a 600-mode `negahban.env.json` outside the repo:

   ```json
   {"LIARA_API_TOKEN":"…","LIARA_TEAM_ID":"…","LIARA_NETWORK_ID":"…","LIARA_API_BASE":"https://api.iran.liara.ir",
    "BOXAPI_BASE_URL":"https://ai.boxapi.ir/ai/v1","BOXAPI_KEYS":"k1,k2,k3","NEGAHBAN_MODEL":"openai/gpt-6-luna",
    "ADMIN_PASSWORD":"…","SESSION_SECRET":"<openssl rand -hex 32>","MCP_TOKEN":"…","WATCHDOG_TOKEN":"…",
    "DEMO_APP":"demo-shop","DEMO_CHAOS_TOKEN":"…","MONITOR_APPS":"demo-shop,bazargard-ai","DATA_DIR":"/data"}
   ```

   Generate random secrets with `openssl rand -hex 32`. See the [configuration table](../README.md#configuration).

## 3. Deploy

Pick **one** method. All three upload the source, Liara builds the `Dockerfile` (port **8080**) and mounts disk `data`
at `/data`.

### Option A — project deploy script (recommended)

`deploy/deploy_negahban.py` reads secrets from 600-mode env files on the operator host, creates the `data` disk if
missing, uploads the source (excluding `data/ evidence/ docs/ deploy/ tests/`), creates the release with the full env
set and waits for the result. Check the env-file paths at the top of `main()` before first use.

```bash
make deploy MSG="release 1.0.0"          # = python3 deploy/deploy_negahban.py "release 1.0.0"
```

### Option B — Liara CLI

`liara.json` sets app, platform, port and disk. It deliberately contains **no `envs`** (they would replace the app's
variables); set env vars once via the console or Option C's `update-envs` call.

```bash
liara login                              # once
liara deploy --message "release 1.0.0"   # uses ./liara.json
```

### Option C — raw API with `tools/liara_cli.py`

```bash
python3 tools/liara_cli.py deploy negahban-sre . 8080 ~/negahban.env.json '[{"name":"data","mountTo":"/data"}]'
python3 tools/liara_cli.py wait negahban-sre      # BUILDING → DEPLOYING → READY (or FAILED/UNHEALTHY)
```

Notes:

- Builds run on Liara's builders (default location from the account; buildTimeout is a few minutes).
- Daily deploy quota depends on the bundle (e.g. 20/day on basic, 50/day on standard); HTTP 428
  `max_deployment_count_in_day` means it is used up.
- The CLI refuses to deploy an app that is scaled to 0 — start it first (`post /v1/projects/negahban-sre/actions/scale '{"scale":1}'`).

## 4. Verify (health checks)

```bash
curl -fsS https://negahban-sre.liara.run/healthz        # 200, reports orchestrator lag
python3 tools/liara_cli.py releases negahban-sre 3      # newest release READY
python3 tools/liara_cli.py get /v1/projects/negahban-sre/metrics/summary
```

Then sign in to the dashboard and confirm the monitored apps appear with fresh samples.

Optionally add a Liara health check to `liara.json` (gates zero-downtime cutover and restarts on failure, §7):
`"healthCheck":{"command":"CMD curl --fail http://localhost:8080/healthz || exit 1","interval":30,"timeout":15,"retries":2,"startPeriod":10}`
— the image installs `curl`. Units are ambiguous in Liara's docs; test once before relying on it.

## 5. Upgrades

1. `git pull` (or re-sync) and run `make test`.
2. Note the current release ID: `python3 tools/liara_cli.py releases negahban-sre 1`.
3. Take a disk backup if the bundle allows it ([§7](#7-backups-and-restore)).
4. Deploy (section 3) and verify (section 4).
5. Record the version in `CHANGELOG.md`.

Env-only change (restarts the app and creates an `ENV_UPDATE` release, which can be rolled back):

```bash
python3 tools/liara_cli.py post /v1/projects/update-envs '{"project":"negahban-sre","variables":[{"key":"K","value":"V"}, …all others…]}'
```

## 6. Rollback

Liara has a rollback endpoint used by its console (not in the CLI or OpenAPI spec, §4):

```bash
python3 tools/liara_cli.py releases negahban-sre 10       # pick the newest READY DEPLOY/ENV_UPDATE that is not current
python3 tools/liara_cli.py rollback <releaseID>           # POST /v1/releases/{id}/rollback
python3 tools/liara_cli.py wait negahban-sre
```

| Response | Meaning / action |
|---|---|
| 409 | That release is already current. |
| 404 | Release cannot be rolled back to. Pick another. |
| 428 `rollback_not_supported` | Bundle does not support rollback → redeploy the previous git tag with section 3. |

Fallback that always works: `git checkout <previous-tag>` and deploy again.
Rollback does **not** roll back the `/data` disk — restore a disk backup if the data format changed.

## 7. Backups and restore

`/data` holds the SQLite database (incidents, approvals, audit log, policies, chat), deploy archives and workspaces.

- **Silver/Gold bundles:** automatic disk backups (daily / hourly). Manual backup:
  `python3 tools/liara_cli.py post /v1/projects/<project-_id>/disks/data/backups`
  List: `get /v1/projects/<project-_id>/disks/data/backups`; download link: `post …/backups/<name>/download`.
- **Bronze/basic bundle:** no disk backups. Export manually through the Liara file browser / FTP
  (`post /v1/projects/negahban-sre/disks/data/ftp '{"username":"backup","readOnly":true}'`) on a schedule.
- **Restore:** `POST /v2/projects/<project-_id>/disks/data/restore {"backupName","targetDiskName","targetProjectID"}`,
  then restart the app.

`<project-_id>` is `project._id` from `get /v1/projects/negahban-sre` (some disk routes need the ID, not the name).

## 8. Logs and metrics

```bash
liara app:logs --app negahban-sre -f                         # CLI, polls every second
python3 tools/liara_cli.py get "/v2/projects/negahban-sre/logs?start=$(date -d '-1 hour' +%s)&last_lines=true"
python3 tools/liara_cli.py get "/v1/projects/negahban-sre/events?start=$(date -d '-1 day' +%s)"   # oom/die/kill/restart
python3 tools/liara_cli.py get "/v1/projects/negahban-sre/metrics/memory?since=$(date -d '-3 hours' +%s)"
```

`since`/`start` are **absolute unix seconds**, not durations. More in [OPERATIONS.md](OPERATIONS.md).

## 9. Demo target app (`demo-shop`)

`demo-shop/` is a small Persian store with token-protected fault switches, used to demonstrate incidents.
It is deployed as its own Liara app (`demo-shop/Dockerfile`, port 8080) with env `CHAOS_TOKEN` equal to Negahban's
`DEMO_CHAOS_TOKEN`. Negahban itself redeploys it from `DEMO_SRC` during self-repair demos.

```bash
python3 tools/liara_cli.py deploy demo-shop demo-shop 8080 '{"CHAOS_TOKEN":"…","APP_VERSION":"1.0.0"}'
```

## Local Docker

```bash
make docker-build                         # docker build -t negahban:local .
make docker-run                           # runs with --env-file .env, ./data mounted at /data, port 8080
```

Set `SECURE_COOKIES=0` in `.env` for plain-HTTP localhost.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `404` on every API call | Wrong or missing `teamID` | Check `LIARA_TEAM_ID`. |
| `401` | Token expired / wrong | Create a new API token; update env set; redeploy env. |
| `400 frozen_project` | Account out of credit | Top up; app resumes. |
| `428 max_deployment_count_in_day` | Bundle deploy quota used | Wait for the next day or upgrade bundle. |
| Release `FAILED` | Build error | `get /v2/releases/<id>/build-logs` — any STDERR line fails the build. |
| Release `UNHEALTHY` | Health check failing | Check `/healthz` path/port 8080 and startup logs; old release keeps serving. |
| App starts, then `die`/`oom` events | Memory limit | Inspect `metrics/memory`; resize plan (`post /v1/projects/negahban-sre/resize '{"planID":"…"}'`, ~20 s of 502s). |
| Dashboard login loops | `SECURE_COOKIES=1` over HTTP, or `SESSION_SECRET` changed | Use HTTPS (`*.liara.run`), keep a fixed `SESSION_SECRET`. |
| Sessions lost after each deploy | `SESSION_SECRET` unset (random per start) | Set it in the env set. |
| Chat says LLM unavailable | `BOXAPI_KEYS` empty or all keys cooling down | Check keys; see app logs for redacted provider errors. |
| Nothing is written to Liara | `LIARA_DRY_RUN=1` | Set to `0` in production. |
| Data gone after redeploy | Disk not mounted | Release must include `disks:[{"name":"data","mountTo":"/data"}]`. |

<!-- platform:begin (maintained by the platform kit; edit infra/ and .github/ rather than this block) -->
## Release pipeline, infrastructure as code and health checks

The platform overview is in [PLATFORM.md](PLATFORM.md). It
covers the architecture, SLOs, backups and why the setup is built this way. This section is the
Negahban-specific part.

### Pipeline: GitHub Actions to Liara

| Workflow | Trigger | What it does |
|---|---|---|
| `CI` (`.github/workflows/ci.yml`) | every push and PR; reused by Deploy | make lint + make test (once the application code is synced), CLI smoke, platform-kit tests + actionlint, gitleaks over the full history, pip-audit, image build + container smoke + non-root check + SBOM (SPDX/CycloneDX) + Trivy, and on `main` a read-only Liara dry run + drift plan |
| `Deploy to Liara` (`.github/workflows/deploy.yml`) | tag `v*`, or manual (defaults to dry run) | CI gate, then `infra/liara_deploy.py`: package the tagged commit, upload, release with `gitInfo` + tag message, watch the build and health check, smoke tests, **automatic rollback** to the previous release on failure, job summary |

**Status: ready, not enabled.** No deploy has been run from GitHub yet. To release:

```bash
git tag vX.Y.Z && git push origin vX.Y.Z          # or Actions -> "Deploy to Liara" -> dry_run=false
```

**Same pipeline from a workstation** (needs `LIARA_API_TOKEN` and `LIARA_TEAM_ID` in the environment):

```bash
pip install pyyaml
python3 infra/liara_deploy.py deploy   --manifest infra/liara.yaml --app negahban-sre --dry-run            # nothing deployed
python3 infra/liara_deploy.py deploy   --manifest infra/liara.yaml --app negahban-sre --message vX.Y.Z
python3 infra/liara_deploy.py status   --manifest infra/liara.yaml --app negahban-sre                      # history, * = current
python3 infra/liara_deploy.py rollback --manifest infra/liara.yaml --app negahban-sre [--to <releaseId>]
python3 infra/liara_deploy.py smoke    --manifest infra/liara.yaml --app negahban-sre
```

**Exit codes** of `deploy`:

| Code | Meaning |
|---|---|
| 0 | Deployed |
| 1 | Failed before traffic moved |
| 2 | Smoke tests failed, rolled back automatically |
| 3 | Rollback failed: follow runbook R1 |
| 4 | Refused by preflight (stopped app, missing env name, daily quota, size) |

Release bodies never contain `envs`, because on Liara they would replace every variable. Values stay in Liara.

### Infrastructure as code

`infra/liara.yaml --app negahban-sre` declares the app's desired state:

- plan, bundle, network
- zero-downtime, read-only root filesystem
- disks and their backup policy
- required and optional env var **names**
- health check, smoke checks, SLO, and vertical scaling bounds

```bash
python3 infra/liara_apply.py plan  -f infra/liara.yaml --app negahban-sre      # read-only diff; exit 0 in sync, 2 drift, 1 invalid
python3 infra/liara_apply.py apply -f infra/liara.yaml --app negahban-sre      # prompts for "yes"; never deletes without --allow-delete
```

The plan is validated against the bundle's real capabilities, read from `/v1/me`. CI runs it read-only on
every push to `main` and puts it in the job summary.

### Health checks and zero downtime

| | Value |
|---|---|
| Liara health check (liveness) | `python -c urlopen('http://127.0.0.1:8080/healthz') (sent to the owner for deploy/deploy_negahban.py)`, interval 30 s, timeout 10 s, 3 retries, start period 30 s. Values are in seconds: Liara's API rejects `interval < 30` and `startPeriod > 3000`, so the CLI's "ms" label is wrong |
| Post-deploy smoke tests | `/healthz` must return `ok: true`; `/` must serve the Persian dashboard (contains «نگهبان»). For `demo-shop`: `/healthz` must return `ok: true` |
| Zero-downtime | `demo-shop` runs the standard bundle with zero-downtime **on**. `negahban-sre` is basic (upgrade about +486 toman/h) |

Traffic moves to a new release only after its health check passes. Otherwise the release turns `UNHEALTHY`
and the pipeline rolls back. At runtime, a failing check makes Liara restart the container.

### Container hardening

`negahban` runs as uid 1000 via `setpriv` (387 MB image: it ships git and curl for its own tooling). `demo-shop` runs as root; it is a deliberately fragile demo target, and CI reports a warning. Neither image contains `.env`, keys or `.git`, and CI checks this. Dependency finding (2026-10-22): `fastapi==0.115.6` pulls `starlette 0.41.3`, which has 6 advisories. Bump fastapi; pip-audit in CI flags it until then.
<!-- platform:end -->
