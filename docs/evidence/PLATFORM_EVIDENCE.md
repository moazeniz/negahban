# Platform evidence (2026-10-22)

What was run to validate the platform, and what came out. Operational jobs are built and installed but
**deliberately not enabled** (team decision for the judging window), so the evidence comes from read-only
calls, dry runs, unit tests against fakes, and one manual validation run of each tool.

## Infrastructure as code: read-only plan against the live account

The full output is in [`liara-plan.txt`](liara-plan.txt).

- **Result:** `No changes. Liara infrastructure matches the configuration.`
- **Undeclared env vars:** the plan lists them as notes (names only) and leaves them untouched.
- **Bundle notes:** zero-downtime and native backups are unavailable on the basic bundle; the plan says so
  for each app.
- **Cost and runway:** 8,333 toman/h, and a balance of about 1.97M toman, which is about 237 h (9.9 days).

## Release pipeline: dry runs against the live account (nothing deployed)

`python3 infra/liara_deploy.py deploy --manifest ... --dry-run`

| App | Package (tracked files minus .liaraignore) | Preflight (read-only) | Required env names |
|---|---|---|---|
| bazargard-ai | 91 files, 0.29 MB | pass (scale=1, current release recorded) | all present |
| meowmeow-wallex | 84 files, 0.22 MB | pass | all present |
| negahban-sre | 56 files, 0.54 MB (from the authoritative code tree) | pass | all present |
| demo-shop | 10 files from `./demo-shop` | pass | all present |

Rendered release body for `bazargard-ai` (abridged). It carries no `envs`, so live variables are never
replaced:

```json
{"sourceID": "<sourceID>", "type": "docker", "port": 8080, "message": "<tag>",
 "disks": [{"name": "data", "mountTo": "/data"}],
 "build": {"location": "germany", "dockerfile": "./Dockerfile", "cache": true},
 "healthCheck": {"command": ["CMD", "python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/health', timeout=5)"],
                 "interval": 30, "timeout": 10, "retries": 3, "startPeriod": 60}   # seconds (corrected 2026-10-23),
 "gitInfo": {"branch": "main", "commit": "<40-hex>", "message": "...", "committedAt": "...", "remote": "https://github.com/<owner>/<repo>",
             "author": {"name": "...", "email": "..."}, "tags": ["v..."]}}
```

## Unit tests (run in CI on every push)

| Suite | Tests | Covers |
|---|---|---|
| `infra/tests/test_platform.py` | 22 | packaging (determinism, ignore rules, sub-directory context, dirty tree refused); deploy happy path; dry run makes zero writes; smoke failure → rollback; UNHEALTHY with traffic → rollback; build failure keeps the old release; rollback refused → exit 3; preflight refusals (missing env, stopped app, daily quota); job summary; IaC plan read-only and Terraform-style output; capability validation; apply executes and converges; no delete without `--allow-delete`; env values only from the secret store; redaction |
| `ops/tests/test_ops.py` | 11 | backup export + verify (SQLite `integrity_check`); corruption detected; path traversal rejected; retention (7 daily + 4 weekly); restore is a dry run by default and non-destructive; native backups refused on basic; job status file; probe UP/DOWN transitions and incidents; content checks; nearest-rank p95; status page HTML escaping and absence of secrets |

## Synthetic monitor: one manual validation run (timer not enabled)

`python3 ops/monitor/probe.py run --state <scratch> --no-push` from Hetzner (Helsinki) against production:

| App | Probe path | Result | Latency (connect to last byte) | DNS (excluded) | TLS days left |
|---|---|---|---|---|---|
| bazargard-ai | /api/health | UP 200 | 436-504 ms | 64-235 ms | 58.6 |
| negahban-sre | /healthz | UP 200 | 403-519 ms | 94-218 ms | 58.6 |
| demo-shop | /healthz | UP 200 | 402-466 ms | 100-1,799 ms | 58.6 |
| meowmeow-wallex | /healthz | UP 200 | 407-458 ms | 118-1,799 ms | 58.6 |

The DNS outliers (1.8 s) come from the probe host's resolver. That is why DNS is timed separately and
excluded from the latency SLI.

## systemd units (installed on Hetzner, not enabled)

```
$ systemd-analyze verify meow-{monitor,backup}.{service,timer}     -> no errors
$ systemd-analyze security meow-monitor.service  -> Overall exposure level: 1.2 OK
$ systemd-analyze security meow-backup.service   -> Overall exposure level: 1.2 OK
$ systemctl list-unit-files 'meow-*'
meow-backup.timer    disabled
meow-monitor.timer   disabled
```

The credentials sit in `/etc/meow-platform/` (0700 directory, 0600 files) and reach the units through
`LoadCredential=`.

## Arvan LLM gate TLS: staged, not applied

`enable-tls.sh preflight`:

- Let's Encrypt ACME and PyPI are reachable from Arvan.
- Port 8443 is free; port 443 belongs to the stream relay.
- Status code snapshot of every public route on the host: `/` 307, `/hamrah/` 200, `/sms-relay/` 401,
  `/pay` 404, `/ig-feed/feed.json` 200, `/api/` 404, `/liara-data/catalog.db.gz` 401, `/meow-llm/health` 401,
  `/meow-llm/nope` 404.

`enable-tls.sh stage-test` works on a full copy of `/etc/nginx`. It applies the gate-block refactor, the ACME
location and the new `:8443` TLS server, then runs `nginx -t` on the copy:

```
nginx: the configuration file /root/nginx-stage.XQ46BW/nginx.conf syntax is ok
nginx: configuration file /root/nginx-stage.XQ46BW/nginx.conf test is successful
```

The first attempt caught `http2 on;`, which nginx 1.24 does not support. The config was switched to
`listen 8443 ssl http2;`. The live configuration was not modified, and its `nginx -t` still passes.

**Hygiene finding (not changed):** `/etc/nginx/sites-enabled/` on Arvan holds two `onboarding.conf.bak-*`
files. nginx loads them, which causes `conflicting server name` warnings on every reload. Recommendation:
move them to `/root/nginx-backups/`.

Clients trust the short-lived IP certificate chain (`YE2` ← `Root YE`). This was verified with
`python:3.12-slim` (OpenSSL store) and `httpx` + certifi against the Hetzner host, which uses the same
mechanism.

## Container review (images built locally from each repo)

| Image | Size | PID 1 user | Health | Secret-like files (`.env`, `*.pem`, `*.key`, `.git`) |
|---|---|---|---|---|
| bazargard | 220 MB | uid 10001 | `/api/health` 200 in 5 s | none |
| negahban | 387 MB | uid 1000 | `/healthz` 200 in 5 s | none |
| demo-shop | 200 MB | root (demo target) | `/healthz` 200 in 4 s | none |
| meowmeow-wallex | 215 MB | root (fix proposed) | `/healthz` 200 in 2 s | none |

## Secret scanning before the first push

The history was scanned with gitleaks 8.24.3 and 8.30.1 before the repos went to GitHub.

| Repo | Findings |
|---|---|
| Bazargard | 0 |
| Negahban | 0 |
| Wallex | 4, all reviewed false positives: `curl -u user:"$(cat file)"` placeholders, `admin:'<password>'` and a public ERC-20 contract address used as test data |

The Wallex false positives are pinned by fingerprint in `.gitleaksignore`. CI enforces a clean scan on every
push.

## Dependency audit

`pip-audit` found nothing for Bazargard or Wallex. For Negahban and demo-shop it reports
`fastapi==0.115.6` → `starlette 0.41.3` with 6 advisories (PYSEC-2026-1941/1942/2280/2281/248/249). The
owner was notified with the fixed versions.

## GitHub

- Private repos: `moazeniz/bazargard`, `moazeniz/negahban`, `moazeniz/wallex-ai-trader`.
- Actions secrets `LIARA_API_TOKEN` and `LIARA_TEAM_ID` were set with libsodium sealed boxes; values were
  never printed.
- Dependabot alerts and automated security fixes are enabled.
- Branch protection and rulesets are **not available**: GitHub answered *"Upgrade to GitHub Pro or make this
  repository public"*. This is a plan limit, not a token permission. Once a repo is public, see
  `docs/PLATFORM.md` §9 for the one-line protection call.

## Monitor: two vantage points (enabled 2026-10-22)

- **The blip.** At 18:20 UTC three of four apps timed out from Helsinki at once (TLS/connect timeouts, no HTTP
  answer); the fourth answered. Under the new rules that run is `vantage_unreachable` and is listed as a probe-side
  note. Bazargard, Negahban and demo-shop are back at 100.00 % availability.
- **A real outage stays counted.** `meowmeow-wallex` returned HTTP 502 at 18:32 UTC during a redeploy (basic
  bundle, no zero-downtime). It still counts against Wallex at 98.2 %, because that was a real outage.
- **Second vantage.** Negahban's in-Liara probes (`GET /api/status/vantage`) are merged into every run. At
  18:41 UTC both vantages were `ok` for all four apps. https://negahban-sre.liara.run/status shows both
  columns (پایش بیرونی / پایش داخل لیارا) and the notes.
- **Tests.** 19 unit tests for the backup and monitor jobs, including 8 for the accuracy rules.
- **History.** Raw results are kept. Pre-change rows are reclassified idempotently, and the database was
  backed up before the upgrade.
