# Negahban diagnostics toolkit (`negahban/diag/`)

The toolkit adds 29 tools to Negahban's registry. Each tool has a name, a Persian step label, an English description for the LLM, a JSON schema, a risk class and an async handler. `negahban/tools.py` calls `diag.register(tool)`, which wraps every handler with:

- a timeout,
- one audit row (`diag.<tool>`, with actor, args, duration and error),
- secret redaction,
- a 24 KB output cap.

Risk classes:

| Class | Behaviour |
|---|---|
| `read`, `safe` | Runs automatically. |
| `sensitive` | Becomes an approval card. It runs only after the owner approves. |

Each sensitive tool has `title(args)` and `impact(args)`. For a blocked command or a locked app, `impact()` raises an error, so no card is created.

| Module | Purpose |
|---|---|
| `exec.py` + `snippets.py` | Run commands inside an app container through the Liara exec websocket |
| `probes.py` + `dnswire.py` | HTTP/TLS/DNS probes and a bounded load test |
| `liara_data.py` | Liara endpoints the core client does not wrap |
| `analytics.py` | Anomalies, forecasts, deploy correlation, cost, SLO |
| `logintel.py` | Structured and plain-text log intelligence |
| `runbooks.py` | Scripted evidence → hypotheses → actions |
| `websearch.py` | Web search for unknown errors, with citations |
| `api.py` + `static/diag.js` | Dashboard API (`/api/diag`) and the «ترمینال» and «کاوشگر لاگ» views |

## Tools

### Container (exec.py)

| Tool | Risk | What it does | Example args |
|---|---|---|---|
| `container_diag` | read | Runs vetted stdlib Python checks inside the container (see the list below). | `{"app":"demo-shop","check":"overview"}` · `{"app":"bazargard-ai","check":"sqlite_check","path":"/data/places.db"}` · `{"app":"demo-shop","check":"env_names","expect":["PAYMENT_GATEWAY_KEY"]}` |
| `container_exec` | read | Runs one command from the read-only allowlist. | `{"app":"demo-shop","command":"df -h"}` · `{"app":"demo-shop","command":"tail -n 100 /data/app.log"}` |
| `container_exec_any` | **sensitive** | Runs any non-blocked command after the owner approves. Refused on locked apps. | `{"app":"demo-shop","command":"kill 7","reason":"stuck worker"}` |

`container_diag` checks:

| Check | What it reports |
|---|---|
| `overview` | memory, cpu, disk and the top processes together |
| `processes` | RSS, CPU seconds, threads and fds per process |
| `memory` | cgroup usage vs limit, OOM kills, PSI |
| `cpu` | limit, current cores, throttling |
| `disk` | all mounts |
| `data_usage` | biggest directories and files under a path (default `/data`) |
| `open_fds` | open file descriptors per process |
| `ports` | listening sockets, TCP states, remote endpoints |
| `env_names` | variable names only, empty variables, missing expected names |
| `sqlite_check` | read-only `quick_check` (`full=true` runs `integrity_check`), journal mode, WAL size |
| `log_tracebacks` | tracebacks grouped from local `*.log` files |
| `http_local` | GET `127.0.0.1:<port><path>` from inside the container |
| `runtime_info` | versions and installed packages |
| `limits` | process limits, pids, read-only root |

### Probes (probes.py)

| Tool | Risk | What it does | Example args |
|---|---|---|---|
| `http_probe` | read | DNS, connect, TLS, TTFB, download and total timings; status; key headers; the full redirect chain; the TLS certificate; an optional body or status check. GET and HEAD only, public addresses only. | `{"app":"demo-shop","path":"/healthz","expect_status":200}` · `{"url":"https://example.ir/","expect_text":"فروشگاه"}` |
| `tls_check` | read | Certificate verification result (with the exact error), issuer, expiry, `days_left`, SANs, TLS version. | `{"host":"demo-shop.liara.run"}` |
| `dns_lookup` | read | A, AAAA, CNAME, TXT, MX, NS, CAA and SOA records through DoH, plain DNS and the system resolver. `compare=true` shows each resolver's answer. | `{"name":"shop.example.ir","types":["A","CNAME","TXT"],"compare":true}` |
| `load_test` | **sensitive** | Team apps only. At most 50 GET requests, concurrency ≤ 5. Reports p50, p95, p99, status counts, errors and rps. | `{"app":"demo-shop","path":"/","requests":40,"concurrency":4}` |

### Liara data (liara_data.py)

| Tool | Risk | What it does | Example args |
|---|---|---|---|
| `release_details` | read | State, failReason, queue, duration, image, whether the release is current, STDERR lines and a summary of the complete paged build log. | `{"app":"demo-shop","tag":"v9"}` |
| `events_timeline` | read | Container start, die, oom, kill and restart events with counts. Exec sessions are counted but hidden. | `{"app":"demo-shop","hours":24}` |
| `network_metrics` | read | Receive and transmit KB/s (now, average, peak), with a chart. | `{"app":"bazargard-ai","hours":6}` |
| `disks_info` | read | Disks, mount paths, usage % and the backup list. | `{"app":"bazargard-ai"}` |
| `disk_files` | read | File browser listing (read-only). | `{"app":"bazargard-ai","disk":"data","path":"/","sort":"size"}` |
| `disk_file_read` | read | One text file of at most 200 KB, redacted. Binary files and secret stores are refused. | `{"app":"bazargard-ai","path":"/logs/app.log"}` |
| `databases_status` | read | DBaaS list. With `db_id`: metrics and backups. | `{}` · `{"db_id":"..."}` |
| `db_query` | **sensitive** | One read-only SQL statement through the database inspector, capped at 100 rows. | `{"db_id":"...","query":"SELECT count(*) FROM orders"}` |
| `storage_buckets` | read | Object storage buckets. | `{}` |
| `billing_details` | read | Balance, low-credit threshold, run-rate, hours and days left, per-app cost, invoices. | `{}` |
| `dns_zones` | read | Liara DNS zones, or the records of one zone. | `{"zone":"example.ir"}` |
| `domains_health` | read | Custom domains with Liara verification and SSL state, plus a live TLS check. | `{"app":"demo-shop"}` |

### Analytics (analytics.py)

| Tool | Risk | What it does | Example args |
|---|---|---|---|
| `metric_anomalies` | read | EWMA + z-score spikes and drops in cpu, memory or latency, with a chart. | `{"app":"demo-shop","metric":"memory","hours":6}` |
| `resource_forecast` | read | Linear trend vs the plan limit, a leak verdict, and "reaches the limit in ~X h". | `{"app":"demo-shop","metric":"memory"}` |
| `deploy_impact` | read | 5xx rate, tracebacks and latency before vs after each release, significance-tested, plus the suspect release. | `{"app":"demo-shop","releases":3,"window_minutes":30}` |
| `cost_forecast` | read | Credit runway, observed burn from balance history, budget use. | `{"monthly_budget_toman":3000000}` |
| `slo_report` | read | Availability, error budget, 1 h and 6 h burn rates, latency, and outages from probe samples. | `{"app":"demo-shop","target":99.5}` |

### Logs (logintel.py)

| Tool | Risk | What it does | Example args |
|---|---|---|---|
| `log_insights` | read | Error groups (fingerprint = type + innermost app frame) with first and last seen, request ids and samples; 5xx rate; slowest and busiest routes; LLM usage; OOM; restart loops. | `{"app":"demo-shop","minutes":180}` |
| `log_trace` | read | Every event of one `request_id` or `trace_id`, in order. | `{"app":"bazargard-ai","request_id":"req-abc123"}` |
| `llm_usage` | read | Calls, tokens and cost per provider and model, from the structured logs. | `{"app":"bazargard-ai","hours":6}` |

### Runbooks and web search

| Tool | Risk | What it does | Example args |
|---|---|---|---|
| `run_runbook` | safe | Gathers evidence and returns findings, ranked hypotheses and recommended actions. Runbooks: `auto`, `oom`, `crash_loop`, `slow_responses`, `build_failure`, `missing_env`, `disk_full`, `tls_problem`, `dependency_outage`. `propose=true` turns the top sensitive actions into approval cards. Nothing is executed. | `{"app":"demo-shop","runbook":"auto"}` · `{"app":"demo-shop","runbook":"tls_problem","domain":"shop.example.ir"}` |
| `web_search_error` | read | Searches the web for an error message and returns cited snippets (url, title, cited_text). The query is scrubbed of secrets, emails, IPs and ids first. Results are cached for 24 h; the limit is 30 searches per hour. | `{"error":"sqlite3.OperationalError: database is locked","context":"FastAPI on Liara"}` |

## Exec policy (`exec.classify`)

Facts verified live on 2026-10-22:

- **No shell.** The API splits the command on spaces *and* `+`. Quotes are literal. There are no pipes or redirects.
- **Output.** stdout and stderr arrive merged through a PTY. There is no exit code.
- **Logged.** Every exec appears in the app events as `exec_start` / `exec_die`.
- **Size limit.** URLs over about 12 KB get HTTP 414.
- **Slim images.** `python:*-slim` images have no `ps`, `free`, `top`, `ss`, `curl` or `uptime`. Use `container_diag`: its snippets travel zlib+hex-encoded and run with `python3`, or `python` as a fallback. Without Python, a limited `cat`-based fallback is used.

The policy has three levels:

- **read** (runs automatically):
  - `ps`, `top -b -n1`, `free`, `df`, `du -s… <path>`, `uptime`, `id`, `date`, `uname`, `nproc`
  - `cat` of `/proc/{meminfo,loadavg,uptime,pressure/*,net/tcp*,<pid>/status|limits|…}`, cgroup `memory.*`, `cpu.*`, `io.*` and `pids.*` files, and `/etc/{os-release,resolv.conf,hosts}`
  - `ss`, `netstat`
  - `ls`, `stat`, `wc`, `file`
  - `tail` and `head -n ≤2000 <file>`
  - `printenv` and `env` (names only; values stripped)
  - `pip list`, `pip freeze`, `pip show`; `npm ls --depth=0`; `--version` of runtimes
  - `curl` and `wget` to `localhost` / `127.0.0.1` only
  - `getent hosts`
  - `find <path>` with `-maxdepth`, `-type`, `-name`, `-size`, `-mmin` (no `-exec` or `-delete`)

  Paths that look like secret stores are excluded and become **sensitive**: `.env`, `*.pem`, `*.key`, `id_rsa`, `/proc/*/environ`, `/etc/shadow`, `credential`, `secret`.
- **blocked** (never runs, even with approval):
  - `rm -r` / `rm -f`, `mkfs`, `wipefs`, `fdisk`, `shred`, `dd`
  - `shutdown`, `reboot`, `halt`, `init 0|6`
  - `kill` of PID 1 or -1, fork bombs
  - `chmod -R`, `chown -R`
  - `curl`/`wget | sh`
  - writes or redirects into `/etc`, `/usr`, `/bin`, `/lib`, `/boot`, `/proc`, `/sys`
  - `iptables`, `sysctl -w`, `mount`
  - raw `/dev/sd*`
  - `passwd`, `useradd`, `crontab -r`
  - `find -delete`
  - reverse shells
  - obfuscated payloads (`bytes.fromhex`, `b64decode`, `base64 -d`, `\x..`)
  - any `+`
- **sensitive**: everything else. It needs approval through `container_exec_any`.
- **Locked apps.** `meowmeow-wallex`, `negahban-sre` and any app with `write_lock` accept read-only commands only.

### Exec from inside Liara: the relay

The Liara exec gateway answers `Sorry, connection failed` to sessions that come from Iranian networks: Liara's own runtime and Arvan were both tested. Sessions from Finland work. The websockets version is not the cause; 15.0.1 and 17.3 behave the same.

When Negahban runs on Liara, set these two variables:

- `LIARA_EXEC_URL=wss://<relay>/v1/exec`
- `LIARA_EXEC_RELAY_TOKEN=<secret>`

The relay is `deploy/exec_relay.py`. Run it outside Iran behind TLS. It is an authenticated pipe:

- It holds no Liara credential.
- It checks the `X-Relay-Token` header.
- It pins `RELAY_TEAM_ID` and allowlists `RELAY_APPS`.
- It caps each session at 120 s and 4 MB.
- It never logs tokens or arguments.

Without the relay, exec tools fail with a clear error and every other tool keeps working.

## Safety properties

- All outputs pass through `redact`: the configured secrets, bearer/JWT/`sk-` keys, `password=`, `token=` and DSN passwords. They are also cleaned of ANSI, invisible and bidi characters, and capped.
- The core agent fences tool results as untrusted data.
- Probes refuse private, loopback and link-local targets (SSRF guard). The load test only targets the team's own apps.
- File downloads are text-only, 200 KB at most, with secret stores refused. SQL must be one read-only statement: SELECT, SHOW, EXPLAIN or WITH…SELECT, with no write keywords, `pg_sleep` or `INTO OUTFILE`.
- Web search scrubs secrets, emails, IPs, long ids and personal paths before a query leaves the server.

## Integration

- `negahban/tools.py`: `from .diag import register; register(tool)`. `diag.LABELS` maps names to Persian labels; `diag.SENSITIVE` lists the sensitive tools.
- `negahban/main.py`: `from .diag.api import router as diag_router; app.include_router(diag_router)`.
- `static/index.html`: `<script src="/static/diag.js">` after `app.js`. Nav entries: `data-view="terminal"` (ترمینال) and `data-view="logs"` (کاوشگر لاگ). The terminal view accepts an app argument, e.g. `go('terminal', 'demo-shop')`.

## Tests and smoke

- Unit tests: `.venv/bin/python -m pytest -q tests/test_diag_*.py`. They cover mocked HTTP and websocket, fixture logs, every snippet executed for real, policy tables, the API router and registration.
- Live read-only smoke run (Hetzner): `ENV_FILE=/root/.liara.env DATA_DIR=/tmp/ngh-smoke .venv/bin/python deploy/diag_smoke.py demo-shop bazargard-ai`. Pass `--only=tool1,tool2` to run a subset. Only read tools run.

## Virtual servers (IaaS) and server templates (`iaas.py`, `server_templates.py`, `static/servers.js`)

The IaaS API (`https://iaas-api.liara.ir`) uses the same bearer token as the rest of Liara and needs `?teamID=` on every call. I verified it live on 2026-10-22 from Hetzner and from inside Liara. From inside Liara the first call takes about 4 s.

What the OpenAPI spec does not tell you:

- **24-hour delete lock.** `DELETE /vm/{id}` answers **409** until 24 h after creation (the console shows "۲۴ ساعت از ساخته شدن آن گذشته باشد"). A new server therefore costs at least 24 × its hourly price. Because of this:
  - every create card shows `min_cost_toman`;
  - the delete card is refused before the earliest delete time;
  - server lists show `deletable_from`.
- **Create body.** The console sends `{name, plan, OS, config: {SSHKeys?, sshPort?, InitScriptID?}}` and gets back `{VMID, taskID}`.
- **First-boot scripts.** These are console-only `/init-scripts`, created with `{name, description, type: "shell", content}`.
- **One-click images** are OS ids such as `docker-29.1.2`, `gitlabce-…` and `supabase-…`.
- **Power actions** are `start`, `stop`, `reboot` and `shutdown` (shutdown is a hard power cut).
- **Missing `power` field.** Right after creation, `/vm/{id}` can omit `power` for a few minutes while `/vm` already has it. `status()` merges the two.
- **Price.** A server costs the plan's hourly price plus IPv4 (277.77 toman/h). The cheapest plan, standard-base-g2 (1 core, 2 GB, 20 GB), costs 2,326 toman/h.
- **SSH access.** Servers get Iranian IPs. SSH was reachable from Arvan but not from Finland.

| Tool | Risk | What it does | Example args |
|---|---|---|---|
| `list_servers` | read | State, power, plan with cores/RAM/disk, OS, IPs, toman per hour and month, protection, `deletable_from`. | `{}` |
| `server_details` | read | Specs, IPs, SSH port, disks, free disk space, operations, 24 h traffic. The root password is never returned. | `{"server":"web1"}` |
| `server_traffic` | read | Traffic chart and top destinations. | `{"server":"web1","hours":24}` |
| `server_plans` | read | Live plan prices, IPv4 included, cheapest first. | `{}` |
| `server_images` | read | OS and one-click images. | `{}` |
| `server_templates` | read | Server templates with live prices. | `{}` |
| `save_server_template` | safe | Creates a custom template, or edits one (a new version). | `{"name":"وب‌سرور من","plan":"standard-plus-g2","os":"ubuntu-24.04","init_script":"#!/bin/bash\napt-get install -y nginx"}` |
| `create_server` | **sensitive** | Creates from a template, from plan+os, or from cpu+ram_gb (the cheapest matching plan is chosen). Optional SSH keys, SSH port, first-boot script and extra disk. Servers created from explicit specs are auto-saved as templates. | `{"name":"web1","template":"ubuntu-2c4g"}` · `{"name":"lab","cpu":2,"ram_gb":4,"os":"ubuntu-24.04"}` |
| `server_power` | **sensitive** | `start`, `stop`, `reboot` or `shutdown`. | `{"server":"web1","action":"stop"}` |
| `resize_server` | **sensitive** | Changes the plan; the card shows the price delta. | `{"server":"web1","plan":"pro-g2"}` |
| `server_add_disk` / `server_extend_disk` | **sensitive** | Adds a disk, or grows one (disks cannot shrink). | `{"server":"web1","size_gb":10}` |
| `server_reset_password` | **sensitive** | The server must be powered off. The new root password is stored for 15 min behind a one-time token and revealed once with `POST /api/diag/servers/secret/{token}`. It never appears in chat, the LLM context or the audit log. | `{"server":"web1"}` |
| `server_reset_network` | **sensitive** | Resets the server's network. | `{"server":"web1"}` |
| `delete_server` | **sensitive** | `confirm_name` is not a tool parameter, so the LLM cannot pre-fill it. The owner types the name in the dashboard (`POST /api/diag/servers/approve-delete/{pid}`). | `{"server":"web1"}` |

**Protection.** Protected servers refuse every change. A server is protected if it is listed in the `PROTECTED_SERVERS` env var or has the dashboard toggle on (`policy vm:<name>.write_lock`).

**Builtin templates:**

| Template | Plan | Notes |
|---|---|---|
| `ubuntu-mini` | 1 core / 2 GB / 20 GB | Cheapest |
| `ubuntu-2c4g` | 2 cores / 4 GB / 40 GB | ufw, fail2ban, unattended-upgrades |
| `docker-host` | — | docker-29.1.2 one-click image |
| `db-server` | 4 cores / 8 GB / 80 GB | PostgreSQL on localhost, ufw SSH only |
| `debian-mini` | 1 core / 2 GB / 20 GB | Debian 12 |

Custom templates support create, edit (each edit is a new version), clone, restore and delete (type-the-name).

**Dashboard:**

- **«سرورها» (`data-view="servers"`).** Lists servers with power, resize, disk, password, network, protect and delete actions. Every change goes through an approval card. The page also shows one-time password reveals and the template gallery.
- **Templates page.** `servers.js` wraps `VIEWS.templates` and adds a «قالب‌های سرور مجازی» section. Load it after `templates.js`.

**Chat examples:**

- «یک سرور اوبونتو ۲ هسته ۴ گیگ بساز» → `create_server {cpu:2, ram_gb:4, os:"ubuntu-24.04"}` (the card shows price and minimum cost).
- «سرورها را نشان بده» → `list_servers`.
- «سرور X را خاموش کن» → `server_power {server:"X", action:"stop"}`.

**Live proof (2026-10-22):** `deploy/iaas_e2e.py`. Evidence is in `evidence/iaas-e2e-*.json` and `evidence/iaas-e2e-readtools-20261022.txt`.

## Managed databases (`dbaas.py`) and object storage (`storage.py`), plus add-ons (`addons.py`)

No real database or bucket has been created. Everything here is tested against a mocked API (`tests/test_diag_dbaas.py`, `tests/test_diag_storage.py`). The only live calls were read-only: lists, plans and prices, all on 2026-10-23.

### API facts

**Hosts and prices**

| Service | Base URL | Prices |
|---|---|---|
| Databases | `https://api.iran.liara.ir`, same token, `?teamID=` | `/v1/me` `plans.databases[plan].price` + `plans.databaseBundlePlans[plan][bundle].price` (toman/h) |
| Object storage | `https://storage-service.liara.ir/api/v1`, same token, `?teamID=` | `/v1/me` `plans.objectStorage[plan].price` (toman/h) |

- **Database versions:** `/v1/me` `databaseVersions`. Engines available to the team: postgres, mysql, mariadb, mongodb, redis, mssql, elasticsearch, rabbitmq and clickhouse.
- **S3 endpoint:** `https://storage.iran.liara.space`. Override it with `LIARA_S3_ENDPOINT`.

**Database calls that the console uses but the spec does not list**

| Call | Notes |
|---|---|
| `POST /v1/databases` | Body `{hostname, type, version, planID, bundlePlanID, network, publicNetwork}`; returns `{databaseID}`. g2 plans need `network`. |
| `PATCH /v2/databases/{id}/reset-password` | Rate-limited (429). |
| `POST /v1/databases/{id}/{type}-bak {url}` | Restores from a backup file URL (the backup's download link). |
| `POST /v1/databases/{id}/resize-bundle-plan` | Changes the bundle. |

**Storage calls that the console uses but the spec does not list**

| Call | Notes |
|---|---|
| `POST /buckets` | Body `{name, plan, permission}`. The permission is `private` or `public`; the default plan is `20g-g2`. |
| `PATCH /buckets/{b}/type/{private\|public}` | Changes the permission. |
| `PATCH /buckets/{b}/upgrade/{plan}` | Upgrades the plan. |
| `POST /keys` | Body `{buckets:[names], description, permissions:[read,put,list,delete]}`; returns `{data:{accessKey, secretKey}}`. |
| `PATCH /keys/{accessKey}/revoke` | Returns a new secret. |
| `DELETE /keys/{accessKey}` | Deletes the key. |
| `GET /buckets/{b}/download/{key}?expiry=` | Returns a signed URL. |

**Platform rules**

- **No restart endpoint.** A database restart is scale 0 then scale 1.
- **No backups on the basic bundle.** The basic database bundle includes no backups.
- **Bucket plans only grow.** A bucket plan can be upgraded but not downgraded.
- **One-week balance rule.** Liara answers **429** to any new resource unless the credit covers one week of the team's run-rate, new resource included. `liara_data.week_precheck(extra_hourly)` checks this before every create card and again at execution. On 2026-10-23 the balance was 1,786,953 toman. One week at the run-rate including a small Postgres was 1,748,048 toman, so there is room for about one more small resource.

### Secrets

| Secret | How it is handled |
|---|---|
| `root_password`, `password`, connection strings and `user:pass@` in any string | Removed by `strip_secrets` before any output: tools, UI, LLM, audit and logs. |
| New database password, backup download link, storage secret key, signed file link | Revealed once: `POST /api/diag/secret/{token}` (15 min, one use). Never sent to chat, the LLM context or the audit log. |
| Credentials for `connect_*` | Read at execution time and written straight into the app's env through `update-envs`, merged with the existing variables (Liara replaces the whole list). Cards and results show variable **names** only and list which existing names will be overwritten. |

`connect_bucket` creates a dedicated key limited to that bucket, with read, put, list and delete permissions.

### Tools

| Tool | Risk | What it does |
|---|---|---|
| `list_databases`, `database_details`, `database_plans`, `database_backups` | read | List, details (host and port to use, no password), live prices and versions, backups. |
| `create_database` | **sensitive** | Creates a database. Optional `connect_app` creates and connects in one card. The card shows cost per hour and per month, the balance check, and backup retention. |
| `connect_database` | **sensitive** | Writes `DATABASE_URL` (or `MONGODB_URI` / `REDIS_URL`) plus `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` and `DB_NAME` into the app, merged. The prefix is configurable. The app restarts once. |
| `database_power` | **sensitive** | `start`, `stop` or `restart`. |
| `resize_database` | **sensitive** | Changes the plan; the card shows the price delta. |
| `backup_database` | **sensitive** | Takes a backup now. |
| `database_backup_link` | **sensitive** | Creates a backup download link (revealed once). |
| `restore_database_backup` | **sensitive** | Restores from a backup. The owner must type the name. |
| `reset_database_password` | **sensitive** | Resets the password (revealed once). Apps must be reconnected afterwards. |
| `database_public_network` | **sensitive** | Turns public network access on or off. |
| `delete_database` | **sensitive** | Deletes the database. The owner must type the name. |
| `db_query` | **sensitive** | Existing read-only database-inspector query (see above). |
| `list_buckets`, `storage_plans`, `bucket_objects`, `storage_keys` | read | Usage and object count, live prices, the first N objects, keys with masked access keys. |
| `create_bucket` | **sensitive** | Creates a bucket. Optional `connect_app`. |
| `connect_bucket` | **sensitive** | Writes `LIARA_ENDPOINT`, `LIARA_BUCKET_NAME`, `LIARA_ACCESS_KEY` and `LIARA_SECRET_KEY` into the app. With `style=aws` it writes `AWS_ENDPOINT_URL_S3`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_DEFAULT_REGION` instead. |
| `bucket_permission` | **sensitive** | Makes a bucket private or public. |
| `resize_bucket` | **sensitive** | Upgrades the plan. |
| `delete_bucket` | **sensitive** | Deletes the bucket. The owner must type the name, and the bucket must be empty. |
| `create_storage_key` / `revoke_storage_key` | **sensitive** | Creates a key or rotates its secret (revealed once). |
| `delete_storage_key` | **sensitive** | Deletes a key. |
| `bucket_file_link` | **sensitive** | Creates a signed temporary link (revealed once). |
| `propose_addons` | safe | Only creates cards: `create_database` and/or `create_bucket`, each with `connect_app`. |

**Typed-name approvals.** For `delete_server`, `delete_database`, `restore_database_backup` and `delete_bucket`, `confirm_name` is not a tool parameter, so the LLM cannot fill it in. The dashboard approves with `POST /api/diag/approve-typed/{pid} {confirm_name}`; `diag.TYPED_CONFIRM` maps each tool to the argument the owner must type.

**Add-ons.**

- **Adding them:** templates and the setup wizard can carry `addons = {'database': {engine, plan, bundle, prefix}, 'storage': {plan, permission, style}}`. `addons.validate()` normalises them.
- **Suggestions:** `addons.suggest(analysis)` derives add-ons from `analyze_project` output. For example, psycopg or `DATABASE_URL` suggests postgres, and `S3_*`/`AWS_*` variables suggest storage.
- **Cards:** `await addons.propose_addons(app, addons, ctx)` creates the cards.

**Dashboard.** `static/data.js` provides `VIEWS.databases` («پایگاه‌داده») and `VIEWS.storage` («ذخیره‌سازی»). Both show lists, details, actions through cards, typed approvals, one-time reveals and the access keys.

**Chat examples:**

- «یک پایگاه‌داده‌ی Postgres برای demo-shop بساز و وصلش کن» → `create_database {name:"demo-shop-db", engine:"postgres", connect_app:"demo-shop"}`.
- «یک فضای ذخیره‌سازی برای bazargard-ai بساز» → `create_bucket {name, connect_app}`.
- «این پروژه Postgres لازم دارد» → `propose_addons {app, database:"postgres"}`.

**In-app limitations.** `diag.limitations()` returns `(title_fa, text_fa)` rows for these limits.
