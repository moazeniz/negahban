# Liara Platform API + CLI Reference (for an SRE/deploy agent)

Compiled 2026-10-22. Read-only research; no authenticated calls were made.

## Source tags
| Tag | Source |
|---|---|
| [SPEC] | OpenAPI specs, github.com/liara-cloud/openapi `spec/*.yaml` (this repo is also developers.liara.ir, a Mintlify site with `pages/*.mdx`) |
| [CLI] | github.com/liara-cloud/cli v9.6.0 (commit 14ec35f, 2026-10-18), `src/` |
| [CONSOLE] | Public console bundle `https://console.liara.ir/static/js/main.25f709b7.js`. These are the calls the web console makes. Methods and paths are verified from code; response shapes come from the fields the code reads. |
| [DOCS] | github.com/liara-cloud/docs (source of docs.liara.ir, Persian) |
| [MEM] | Earlier live observations from your memory notes (api.iran.liara.ir, team mode) |
| [INFER] | My inference. Not confirmed. |

---

## 1. Hosts, auth, teams

| Service | Base | Notes |
|---|---|---|
| PaaS / DBaaS / me / billing / domains / teams | `https://api.liara.ir` | [SPEC]. CLI: `REGIONS_API_URL = {iran: api.liara.ir, germany: api.liara.ir}` [CLI]. Console default `REACT_APP_API_URL=https://api.liara.ir` [CONSOLE] |
| Same API, Iran edge | `https://api.iran.liara.ir` | The console uses it explicitly for metrics, logs v2, applets, disk metrics, `/v1/payments/pay`, and the exec websocket [CONSOLE]. CLI `API_IR_URL` is used for the shell websocket [CLI]. All calls in [MEM] worked here. **Prefer this host from Iran-side runners.** |
| DNS | `https://dns-service.liara.ir/api/v1` | [SPEC][CONSOLE]. The CLI uses `https://dns-service.iran.liara.ir` [CLI] |
| Object storage | `https://storage-service.liara.ir/api/v1` (console also uses `storage-service.iran.liara.ir`) | [SPEC][CONSOLE] |
| Mail | `https://mail-service.liara.ir/api/v1` | [SPEC] |
| IaaS (VMs) | `https://iaas-api.liara.ir` | [SPEC] |
| AI | `https://ai.liara.ir` | OpenAI-compatible: `/api/{workspaceID}/v1/...` [SPEC] |
| File browser / DB inspector | per-resource host (`{filebrowser-url}`, `{database-inspector-url}`) | [SPEC] |

- **Auth:** `Authorization: Bearer <api_token>` (a JWT) on all services [SPEC][DOCS][CLI].
- **Getting a token:**
  - Console menu "API": `POST /v1/api-token?expiresIn=<dur>` → `{api_token}` [CONSOLE].
  - CLI login: `POST /v1/login?expiresIn=1y {email,password,twoFAType?,totp?,recoveryCode?}` → `{api_token,email,fullname,...}` [CLI].
- **Team scoping:** add the query param `teamID=<id>` to every request [DOCS][SPEC].
  - The CLI sets `?teamID=` on **every** request, empty for the personal account [CLI].
  - The console sets the `teamID` param on api, storage, iaas, dns, mail and AI calls. For AI it also sends the header `x-teamid` [CONSOLE].
  - A missing team permission returns `403 {data:{code:"no_permission"}}` [CONSOLE].
- **Token scope:** no per-app or limited-scope tokens exist. A token is account-wide or team-wide. Team RBAC lives under `/v2/teams/{id}/...` (roles, custom-role, assign-permission) [CLI][CONSOLE].
- **Rate limits:** not documented ("refer to individual service docs") [SPEC].
- **Common errors:**

| Status | Meaning |
|---|---|
| 401 | Bad token |
| 404 | App not found (often a wrong `teamID`) |
| 400 `message:"frozen_project"` | Out of credit |
| 402 | Payment required on create |
| 408 | Timeout |
| 409 | Conflict |
| 413 | Source too large |
| 428 `data.code` | Bundle-plan limits, e.g. `max_deployment_count_in_day`, `germany_builder_not_allowed`, `rollback_not_supported` |

  Source: [CLI][CONSOLE].

---

## 2. PaaS apps ("projects")

| Method | Path | Body / params | Response / notes | Src |
|---|---|---|---|---|
| GET | `/v1/projects` | none | `{projects:[{_id,project_id,type,status,scale,planID,bundlePlanID,network:{_id,name},created_at,isDeployed}]}`. Console also uses `?isManagedWordpress=true` | SPEC CLI |
| POST | `/v1/projects` | `{name, planID, platform, bundlePlanID, network:<networkId>, readOnlyRootFilesystem?}` | g2 plans need a private network | SPEC CLI MEM |
| GET | `/v1/projects/{name}` | none | `{project:{_id,project_id,type,status,defaultSubdomain,zeroDowntime,scale,envs:[{key,value,encrypted,_id}],planID,bundlePlanID,fixedIPStatus,node:{_id,IP},hourlyPrice,isDeployed,reservedDiskSpace,readOnlyRootFilesystem,network}}` | SPEC CLI |
| DELETE | `/v1/projects/{name}` | none | none | SPEC |
| GET | `/v1/projects/{name}/check-availability` | none | Checks whether the name is free | CONSOLE |
| POST | `/v1/projects/{name}/actions/scale` | `{scale: 0\|1}` | **On/off only.** The CLI start/stop commands send 1 or 0 | SPEC CLI CONSOLE |
| POST | `/v1/projects/{name}/actions/restart` | none | none | SPEC CLI |
| POST | `/v1/projects/{name}/resize` | `{planID}` | Changes the hardware plan. About 20 s of 502 [MEM]. The new plan must fit the app's disks [DOCS] | SPEC MEM |
| POST | `/v1/projects/{name}/resize-bundle-plan` | `{bundlePlanID: basic\|standard\|pro}` | Changes the feature bundle. Downgrading requires meeting the lower bundle's limits, e.g. domain count | CONSOLE DOCS |
| POST | `/v1/projects/{name}/unfreeze` | none | Reactivates the app after a credit freeze | CONSOLE |
| POST | `/v1/projects/update-envs` | `{project, variables:[{key,value}]}` | See §6 | SPEC CLI CONSOLE |
| POST | `/v1/projects/update-port` | `{project, port:number}` | none | CLI CONSOLE |
| POST | `/v1/projects/update-dns` | `{projectID, dns:[ip,...]}` | Sets custom DNS resolvers | CONSOLE |
| POST | `/v1/projects/{id}/zero-downtime/{enable\|disable}` | none | none | SPEC CONSOLE |
| POST | `/v1/projects/{id}/default-subdomain/{enable\|disable}` | none | Turns `{app}.liara.run` on or off | SPEC CONSOLE |
| POST | `/v1/projects/{id}/fixed-ip/{enable\|disable}` | none | `{IP}`. Shared static **egress** IP | SPEC DOCS |
| POST | `/v1/projects/{id}/read-only/{enable\|disable}` | none | Read-only root filesystem (default on, except docker and next) | SPEC DOCS |
| GET | `/v1/projects/{name}/applets` | none | `{applets:[{id,name,timestamp,state,reason,release:{releaseID,tag}}]}`. Old-infrastructure view; the console hides it when the app has a `network` | SPEC CONSOLE |
| GET | `/v1/projects/{name}/npm-audit-report` | none | none | CONSOLE |
| GET/POST | `/v1/networks` | `{networkName}` | `{networks:[{_id,name}]}` | CLI MEM |

`{name}` vs `{id}`: the spec uses `id` for the settings, disks and domain routes. The console passes `project._id` for disks GET and for some settings, and the CLI uses the name for `POST /v1/projects/{name}/disks`. [INFER] Most routes accept either; verify each one.

---

## 3. Deploy flow (source or image)

### 3.1 Upload the source
`POST /v2/projects/{name}/sources` with multipart field `file` (a .tar.gz) → `{sourceID}` [SPEC][CLI].
- The console also accepts .zip uploads [CONSOLE].
- The CLI disables the request timeout for uploads [CLI].
- [MEM] Use content type `application/gzip`.

### 3.2 Create the release
`POST /v2/projects/{name}/releases` → `{releaseID}` [SPEC][CLI]. Full body as built by the CLI:

```json
{
  "sourceID": "...",              // OR "image": "nginx:1.27" for an image deploy (no upload)
  "type": "docker",               // platform: node|next|laravel|php|python|django|flask|dotnet|react|angular|vue|static|docker|go
  "port": 8080,
  "message": "release msg",
  "envs": {"K": "V"},             // OBJECT. Replaces ALL existing envs (docs warning)
  "disks": [{"name": "data", "mountTo": "/app/data"}],
  "args": ["sh", "-c", "..."],    // container entrypoint/cmd override
  "cron": ["*/5 * * * * cmd"],    // stored on the release as buildArgs.__CRON
  "build": {"cache": true, "args": {"K": "V"}, "dockerfile": "./Dockerfile", "location": "iran|germany"},
  "healthCheck": {"command": ["CMD","curl","--fail","http://localhost:8080"], "interval": 30, "timeout": 15, "retries": 2, "startPeriod": 5},
  "gitInfo": {"branch":..,"message":..,"commit":..,"committedAt":..,"remote":..,"author":{"name","email"},"tags":[]},
  "platformConfig": {...}         // liara.json[platform], e.g. {"version":"22"}
}
```

- The spec only documents `sourceID`, `port` and `type`; everything else above comes from [CLI].
- The console sends header `x-user-agent: liara/console` (optional).
- The CLI refuses to deploy when `project.scale == 0`: "app isn't running. Please start the app first". Start the app first.

### 3.3 Poll status and build logs [CLI][CONSOLE]

| Call | Response / behaviour |
|---|---|
| `GET /v1/releases/{releaseID}` | `{release:{state,status,failReason?,queue?}}`. `queue` = number of builds ahead of yours (CLI polls every 3 s while `queue>0`) |
| `GET /v2/releases/{releaseID}/build-logs?since=<createdAt of last line>` | `{release:{state,failReason?}, buildOutput:[{_id,line,stream:"STDOUT"\|"STDERR",releaseID,createdAt}]}` |
| `POST /v2/releases/{releaseID}/cancel` | Cancels a build |

Build-log details:
- Start with `since` empty (the console uses `1`). After each page, set `since` to the `createdAt` of the last line.
- The CLI polls every 1 s and the console every 3 s.
- **Any `STDERR` line means the build failed.**
- The CLI treats `Successfully tagged` and `Pushing...` lines as the push phase.
- The console stops polling at a terminal state (READY, FAILED, CANCELED, TIMEDOUT) once the output is empty.

### 3.4 Release states
| State | Meaning |
|---|---|
| (queued) | `release.queue > 0` |
| `BUILDING` | Image building |
| (PUSHING / PUSHED) | Not real server states. The CLI derives them from log lines |
| `DEPLOYING` | Container starting; health check running |
| `READY` | Live. Terminal |
| `UNHEALTHY` | Deployed but the health check failed. The CLI treats this as failure |
| `FAILED` | See `failReason`, e.g. `disk_not_found <name>` |
| `CANCELED` | Canceled |
| `TIMEDOUT` | Build exceeded the bundle limit (5, 10 or 20 min) |

Release types seen: `DEPLOY`, `ENV_UPDATE` [CONSOLE].

### 3.5 Image-only deploy
For `body.image` without a source, the CLI polls only `GET /v1/releases/{id}` until READY, FAILED or UNHEALTHY [CLI].

### 3.6 Other release endpoints
| Call | Response / notes | Src |
|---|---|---|
| `GET /v1/projects/{name}/releases?page=1&count=10` | `{total, currentRelease:<releaseId>, readyReleasesCount, releases:[{_id,type,imageName,projectType,state,port,buildLocation,buildTimeout,gitInfo,client,disks,createdAt,finishedAt,tag:"v12",sourceAvailable}], platform}`. Both params are required by the spec | SPEC CLI |
| `GET /v1/projects/{name}/releases/current` | `{currentRelease:{..., buildArgs:{__CRON:"[...]"}}}` | CONSOLE |
| `POST /v2/releases/{id}/source` | `{url}`. Download link for a release's uploaded source | CONSOLE |

### 3.7 GitHub integration
`POST /v1/projects/{_id}/git-repository-integration/github/manual-deploy` triggers a deploy from the connected repo [CONSOLE].

### 3.8 Limits per bundle [DOCS software-plans]
| | Bronze (`basic`) | Silver (`standard`) | Gold (`pro`) |
|---|---|---|---|
| Deploys per day | 20 | 50 | unlimited |
| Max source upload | 128 MB | 256 MB | 256 MB |
| Build timeout | 5 min | 10 min | 20 min |
| Zero-downtime deploy | **no** | yes | yes |
| Roll back to a previous deploy | yes | yes | yes |
| Germany builder | yes | yes | yes |

- The CLI also checks client-side `MAX_SOURCE_SIZE = 256 MiB` [CLI]. The server returns 413 above the bundle limit.
- CLI error text still uses the old bundle IDs `free`, `standard` and `enterprise`. Its `free` limits are 10 deploys/day and 128 MB.
- Current bundle IDs are `basic`, `standard` and `pro`, shown as Bronze, Silver and Gold [CLI set-bundle-plan-names][CONSOLE].
- **Build location:** the default is `iran` (package downloads restricted, fast push). `germany` has unrestricted downloads and a slow push [DOCS]. [MEM] Builds were observed running in Germany.
- **Archive ignore files:** the CLI reads `.liaraignore`, else `.dockerignore`, else `.gitignore`, per directory. It always ignores `.git`, `node_modules`, `liara.json`, `.next`, `.idea`, `.vscode`, `.DS_Store` [CLI].

---

## 4. Rollback (answer: yes, a real endpoint exists, console-only)

**`POST /v1/releases/{releaseID}/rollback`** with no body [CONSOLE]. It is not in the spec or the CLI.

- **Who can roll back:** the console enables it for a release only when `type ∈ {DEPLOY, ENV_UPDATE}` and `state == "READY"`. The release must also be different from `currentRelease`.
- **Errors:**

| Status | Meaning |
|---|---|
| 409 | Release is already the current one |
| 404 | Cannot roll back to this release |
| 428 `rollback_not_supported` | The project does not support rollback |
| 403 `no_permission` | Missing team permission |

- **Agent recipe:**
  1. `GET /v1/projects/{app}/releases?page=1&count=20`.
  2. Pick the newest release with `state=READY`, `type ∈ {DEPLOY, ENV_UPDATE}` and `_id != currentRelease`.
  3. `POST /v1/releases/{_id}/rollback`.
  4. Poll the releases list or `/v1/releases/{id}` until a new READY release appears.

  [INFER] Rollback probably creates a new release record. Verify on first use.
- **Fallbacks:**
  - Redeploy an old release's image: `POST /v2/projects/{app}/releases {image:<release.imageName>, type:"docker", port, ...}`. [INFER] The image may not be pullable outside Liara's private registry.
  - Re-upload source: `POST /v2/releases/{id}/source` → url → download → re-upload.
- **"Redeploy current":** there is no explicit endpoint. Use `actions/restart`, or a no-op env update (this creates an `ENV_UPDATE` release).

---

## 5. Logs, events, metrics

### 5.1 Runtime logs
**v2 (current):** `GET /v2/projects/{name}/logs` [CLI][CONSOLE]. The console calls it on `api.iran.liara.ir` with a 30 s timeout.

| Param | Meaning |
|---|---|
| `start` | Unix **seconds**. CLI default: now minus the bundle's `maxLogsRetention` |
| `end` | Unix seconds |
| `direction=forward` | Read order |
| `releaseId` | Filter by release `_id` |
| `appletId` | Filter by instance |
| `search` | Text search, at least 3 characters |
| `last_lines` | `true` returns only the tail |

Response (Loki style):
```json
{"data":[{"metaData":{"releaseId":"..."},"values":[["1729600000123456789","{\"_entry\":\"log line\",\"type\":\"stdout|stderr\"}"]]}]}
```
- The timestamp is a nanosecond string; `slice(0,10)` gives seconds.
- Groups are not globally sorted. Flatten them and sort by the BigInt timestamp.

**Follow mode:** there is **no websocket or streaming** for logs. The CLI `--follow` polls every 1 s with `start = lastTs + 1` [CLI].

**Retention:**
- From [DOCS]: Bronze 7 days, Silver 30 days, Gold 60 days. The CLI reads it from `/v1/me` → `plans.projectBundlePlans[planID][bundlePlanID].maxLogsRetention` (days).
- Old-infrastructure apps are limited to 7 days.
- Asking for an older range triggers the `max_logs_period` error.

**v1 (deprecated):** `GET /v1/projects/{name}/logs?since=<ts>` → `[{type,datetime,message}]` [SPEC]. [MEM] It now returns only "upgrade your client". The CLI rejects apps without a `network` (old infrastructure) for logs and shell.

### 5.2 Build logs
See §3.3.

### 5.3 Events (container lifecycle)
`GET /v1/projects/{name}/events?start=<unix s>&end=<unix s>&search=` [SPEC][CONSOLE].
- **Spec shape:** `{data:[{metaData:{}, values:[[string]]}]}`.
- **Console reads:** `data[]` items with `id`, `action` and `time` (unix seconds). The console polls every 5 s and moves `start` to `lastEvent.time + 2`.
- **Actions shown:** `oom`, `start`, `create`, `destory` (sic), `die`, `stop`, `kill`, `restart`, `exec_die`, `exec_start*`. These are Docker container events [CONSOLE].
- **Docs meaning:** kill, stop, waiting-to-run, running, app-off (crash), stopped-by-high-RAM (OOM) [DOCS].
- Only available for apps on the new infrastructure (with a network). DBs have the same route: `/v1/databases/{id}/events`.
- **Agent use:** watch for `oom`, `die` or `kill` without a matching deploy → crash or OOM alert.

### 5.4 Metrics [SPEC][CONSOLE]
| Call | Notes |
|---|---|
| `GET /v1/projects/{name}/metrics/{cpu\|memory\|network-receive\|network-transmit}?since=<unix seconds>` | `since` is an **absolute unix timestamp in seconds, not a duration**. Console presets: now-1h, -3h, -24h, -48h, -1w, -30d, -60d. It polls every 15 s on `api.iran.liara.ir`. Response: `{end:number, result:[{applet:string, values:[[ts, value], ...]}]}` |
| `GET /v1/projects/{name}/metrics/summary` | No params. `{cpuUsage:[{applet, value:[ts, val]}], memoryUsage:[{applet, value:[ts,val]}], disksUsage:[{name,size,usage,reportedAt}]}`. A current snapshot per instance. The console caches it for 5 min |
| `GET /v1/projects/{_id}/disks/{disk}/metrics` | `{result}`. Disk usage | 

- Units are not documented. [INFER] CPU is in cores, memory in bytes, network in bytes/s.
- Metric history follows the bundle: 7, 30 or 60 days. Too old a range gives 428 [DOCS][CONSOLE].
- DB equivalents: `/v1/databases/{id}/metrics/...`; the DB summary has `diskUsage`.

---

## 6. Environment variables
- **Read:** `GET /v1/projects/{name}` → `project.envs[] {key,value,encrypted}` [SPEC][CLI].
- **Write:** `POST /v1/projects/update-envs {project, variables:[{key,value}]}`. This is a **full replacement**:
  - CLI `env:set` sends existing + new variables.
  - CLI `env:unset` sends the existing list without the removed keys [CLI].
- **Does it restart the app? Yes.**
  - CLI prompt: "Your app will be restarted due to these configuration changes"; then "applied and restarting" [CLI].
  - Console: a **201** response means "changes applied, a deployment will start shortly" [CONSOLE].
  - It creates an `ENV_UPDATE` release, which can be rolled back. [INFER] It follows the zero-downtime and health-check path when enabled.
- `envs` in the release body or in liara.json also replaces all existing envs [DOCS].

---

## 7. Scaling, health checks, zero downtime
- **No horizontal scaling, replica count or autoscaling** anywhere in the spec, CLI or console.
  - `actions/scale` only takes `0` (off) or `1` (on).
  - The "automatic scaling" wording on the developers site intro is marketing; no API exists.
- **Vertical scaling:**
  - `POST /v1/projects/{name}/resize {planID}` for hardware.
  - `POST /v1/projects/{name}/resize-bundle-plan {bundlePlanID}` for the feature bundle.
  - [MEM] A resize causes about 20 s of 502s.
- **Health check** (liara.json `healthCheck`, sent in the release body) [CLI schema][DOCS]:
  - Fields: `command` (string or array; required when healthCheck is present; a string is split on spaces), `interval`, `timeout`, `retries`, `startPeriod` (numbers).
  - Doc example: `{"command":"CMD curl --fail http://localhost:3000 || exit 1","interval":30,"timeout":15,"retries":2,"startPeriod":5}`.
  - **Units conflict:** the docs say milliseconds, but the example values look like seconds (Docker HEALTHCHECK). [INFER] Test this.
  - **Behaviour [DOCS]:** during a deploy, traffic does not move to the new release until the check passes; otherwise the old release keeps serving and the release becomes `UNHEALTHY`.
  - If the check later fails at runtime (hours or days after), Liara **automatically restarts** the app.
  - `curl` is installed on all built-in platforms. A Docker image must provide its own.
- **Zero downtime:** on by default. The new release starts, and once it is healthy traffic switches and the old one is killed [DOCS].
  - The docs table says it is **not available on the Bronze/basic bundle**.
  - Toggle: `POST /v1/projects/{id}/zero-downtime/{enable|disable}`.
- **Cron:** liara.json `"cron": ["<schedule> <cmd>", ...]`, sent as `body.cron` [CLI][DOCS]. It is stored as `currentRelease.buildArgs.__CRON` [CONSOLE]. For Docker, the docs recommend supercronic inside the image.

---

## 8. liara.json (complete field list) [CLI schemas/json/liara.json + types][DOCS]

| Field | Type | Notes |
|---|---|---|
| `app` | string | App name (lowercased) |
| `platform` | string | See the §3.2 list |
| `port` | number | Port the app listens on |
| `team-id` | string | Team scoping |
| `args` | string[] | Entrypoint/cmd override |
| `cron` | string[] | Cron lines |
| `disks` | `[{name, mountTo}]` | Disk mounts |
| `envs` | `{K:V}` | Replaces all envs |
| `build` | `{dockerfile, cache:bool, args:["K=V"], location:"iran"\|"germany"}` | Build options |
| `healthCheck` | `{command, interval, timeout, retries, startPeriod}` | See §7 |
| `node` | `{version, mirror, npmAudit, npmCiFallback, timezone}` | Platform block |
| `next` | `{nodeVersion, mirror, modifyConfig, npmCiFallback}` | Platform block |
| `laravel` | `{phpVersion, timezone, composerMirror, npmMirror, configCache, routeCache, buildAssets, installDevDependencies, modifyHtaccess, modifyImagickPolicy, ssr, reverb, reverbPath}` | Platform block |
| `php` | `{version, timezone, composerMirror}` | Platform block |
| `python` | `{version, timezone, mirror}` | Platform block |
| `django` | `{pythonVersion, nodeVersion, timezone, mirror, collectStatic, compileMessages, modifySettings, geospatial, pipTimeout, settingsFile}` | Schema key typo: `djnago` |
| `flask` | `{pythonVersion, nodeVersion, appModule, timezone, mirror}` | Platform block |
| `dotnet` | `{version, timezone, csprojectFile, finalDllName}` | Platform block |
| `go` | `{version, timezone, mainFile, cgoEnabled}` | Platform block |
| `react`, `angular` | `{mirror, sourceMap}` | Platform block |
| `vue` | `{mirror}` | Platform block |
| `docker` | `{timezone}` | Docs only |

- CLI flags override liara.json.
- `--build-arg K=V` takes priority over `build.args`. Both are sent as an object `{K:V}`.
- Default timezone is Asia/Tehran. The `/tmp` directory has 100 MB [DOCS].

---

## 9. Disks [SPEC][CLI][DOCS]
| Method | Path | Body / response |
|---|---|---|
| GET | `/v1/projects/{id}/disks` | `{disks:[{_id,name,size,createdAt,updatedAt}], mounts:[{name,mountedTo}]}` |
| POST | `/v1/projects/{name}/disks` | `{name, size}` (size is a string/number in GB). Error `not_enough_storage_space` means the plan volume is full |
| POST | `/v1/projects/{name}/disks/{d}/resize` | `{size}`. **Increase only** |
| DELETE | `/v1/projects/{id}/disks/{name}` | none |
| GET / POST | `/v1/projects/{id}/disks/{name}/backups` | GET lists `{backups:[{name,lastModified,etag,size}]}`; POST creates a manual backup |
| POST | `/v1/projects/{id}/disks/{d}/backups/{b}/download` | `{link}` |
| POST | `/v2/projects/{id}/disks/{d}/restore` | `{backupName, targetDiskName, targetProjectID}` [CONSOLE] |
| GET / POST | `/v1/projects/{name}/disks/{d}/ftp` | POST `{username, readOnly}`; DELETE via `/v1/ftp/{fname}` |

- Disks consume the plan's disk volume.
- File browser: `{filebrowser-url}/v1/projects/{id}/disks/{d}/list|search|upload|create|rename|extract|delete|createLink|serve`.
- **Backups by bundle [DOCS]:**

| Bundle | Automatic backups | Manual backups | Backup space |
|---|---|---|---|
| Bronze | none | none | none |
| Silver | daily, kept 7 days | 2 per hour | 45 GB |
| Gold | hourly (12 h), daily (7 d), weekly (4 w), monthly (3 m) | 10 per hour | 155 GB |

- **Disk and FTP counts by bundle:**

| Bundle | Disks | FTP accounts |
|---|---|---|
| Bronze | 2 | 2 |
| Silver | 5 | 2 |
| Gold | 10 | 100 |

---

## 10. Domains, SSL, DNS

### 10.1 App domains [SPEC][CONSOLE][DOCS]
| Method | Path | Notes |
|---|---|---|
| GET | `/v1/domains?project=<app>` | Also accepts `page`, `count`, `domain`. Returns `{domains:[{_id,name,type,project,status,certificatesStatus,redirectTo,redirectStatus,CNameRecord,created_at}]}` |
| POST | `/v1/domains` | `{name, type:"PROJECT", project:<app name>}` → 201 `{domain:{_id}}`. Without `project`, attach later with `POST /v1/domains/set-project {domainID, projectID}` |
| GET | `/v1/domains/{id}` | Full object: `name, status, certificatesStatus, target, verificationCode, isRoot, isWildcard, subdomain, CNameRecord, issuedAt, punycodeName` [CONSOLE] |
| GET | `/v1/domains/{id}/check` | Re-verifies DNS; returns `{domain:{...status, certificatesStatus...}}` |
| POST | `/v1/domains/provision-ssl-certs` | `{domain:"<domain name>"}` → `certificatesStatus` becomes `REQUESTING`, then `ACTIVE`. 409 "Record has conflict with other records." Renewal is automatic |
| POST | `/v1/domains/{id}/ssl/disable` | none |
| POST | `/v1/domains/{id}/set-redirect` | `{redirectTo:"https://x", redirectStatus:301}` |
| DELETE | `/v1/domains/{id}` | none |

**Records to create at the DNS provider** (shown by the console; the docs list the same three):

| # | Type | Name | Value | Purpose |
|---|---|---|---|---|
| 1 | `ALIAS` (root) or `CNAME` (subdomain) | the domain | `domain.target` from GET /v1/domains/{id} | Routes traffic |
| 2 | `TXT` | `liara-challenge` (root) or `liara-challenge.<sub>` | `domain.verificationCode` | Ownership |
| 3 | `CNAME`, no CDN proxy | `_acme-challenge.<name>` | `<name with "." replaced by "-">._acme-challenge.liara.cloud` | DNS-01 for SSL; wildcard uses the base name |

- Then call `GET /v1/domains/{id}/check` until `status == "ACTIVE"`, then provision SSL.
- With a CDN (Cloudflare or Arvan) in proxy mode, ignore the red check marks, keep `_acme-challenge` unproxied, and turn off Cloudflare Universal SSL while Liara issues the certificate. 502 errors behind a CDN usually mean an HTTP/HTTPS mismatch with Liara's SSL setting [DOCS].
- Default URL: `https://{app}.liara.run`. Turn it off with default-subdomain/disable.
- Wildcard domains need the Gold bundle. Domain limit: 5 / 5 / unlimited [DOCS].
- If the zone is hosted on Liara DNS, the console creates records 1 and 3 automatically through the DNS API. A `www` helper adds an ALIAS `www` and a 301 redirect [CONSOLE].

### 10.2 Liara DNS service (free)
Base `https://dns-service.liara.ir/api/v1` (CLI: `dns-service.iran.liara.ir`) [SPEC][CLI][DOCS].

| Method | Path | Notes |
|---|---|---|
| GET / POST | `/zones` | POST `{name}` → `{status, data:{id,name,status,nameServers[],currentNameServers[],lastCheckAt}}` |
| GET / DELETE | `/zones/{zone}` | none |
| PUT | `/zones/{zone}/check` | Re-checks nameservers (asynchronous) |
| GET / POST | `/zones/{zone}/dns-records` | `{name, type, ttl, contents:[...]}` |
| GET / PUT / DELETE | `/zones/{zone}/dns-records/{id}` | The record type cannot be edited |

- **Record types:** A, AAAA, CNAME, ALIAS, MX, TXT, SRV, SPF, PTR, CAA.
- **Allowed TTLs:** 120, 180, 300, 600, 900, 1800, 3600, 7200, 18000, 43200, 86400, 172800, 432000.
- **Contents formats:** A/AAAA `{"ip":...}`; CNAME/ALIAS `{"host":...}`; MX `{"priority":10,"host":...}`. [INFER] TXT uses `{"text":...}`.
- `name` is `@` for the root, `*` for a wildcard, or a label.
- **Zone status:** CREATING → PENDING (waiting for NS change) → ACTIVE; also DELETING and SUSPENDED.
- **IRNIC (.ir) steps [DOCS]:**
  1. Create the zone.
  2. Copy `data.nameServers`. These are per zone; the docs list no fixed names.
  3. Log in at nic.ir → domain → DNS settings, and replace the nameservers.
  4. `PUT /zones/{zone}/check`, then poll `GET /zones/{zone}` until `ACTIVE`. NS changes can take up to 24 h.
  5. Add records.

---

## 11. Plans and pricing
- **No dedicated PaaS plans endpoint.** Use `GET /v1/me` → `plans` [SPEC me.yaml][CLI]:
  - `plans.projects[planID] = {available, region:"iran"|..., RAM:{amount}, CPU:{amount}, volume, price, ...}`. `price` is **toman per hour** [MEM]. The CLI shows `price*720` as the monthly figure [CLI].
  - `plans.projectBundlePlans[planID][basic|standard|pro] = {available, displayPrice (monthly toman), maxLogsRetention (days), ...}`. **The bundle price depends on the hardware plan.**
  - Also `plans.databases`, `databaseBundlePlans`, `mail`, `objectStorage`, `subscription`.
  - The CLI lists plans where `(planID=="free" || includes("g2")) && available && region=="iran"`.
- **Plan names:**

| planID | Console name | RAM | CPU | Disk |
|---|---|---|---|---|
| small-g2 | Earth | 0.5 GB | 0.5 | 5 GB |
| medium-g2 | Mars | 1 GB | 1 | 10 GB |
| standard-base-g2 | Jupiter | 2 GB | 1 | 20 GB |
| standard-plus-g2 | Saturn | 4 GB | 2 | 40 GB |
| pro-g2 | Uranus | 8 GB | 4 | 80 GB |
| pro-plus-g2 | Neptune | 16 GB | 8 | 160 GB |
| express-g2 | Pluto | 32 GB | 16 | 320 GB |

  Hardware sizes are from [DOCS] and console names from [CONSOLE]. The small-g2 = Earth and medium-g2 = Mars mapping is [INFER] from [MEM] prices and order.
- **Billing is hourly** ("costs in Liara are calculated hourly") [DOCS]. An app costs plan hourly price + bundle hourly price. `GET /v1/projects/{name}` returns `hourlyPrice` [SPEC].
- **Account-level subscription:** `GET /v1/me/subscription` (free / standard / enterprise) [SPEC].

---

## 12. Billing, balance, usage [SPEC][DOCS][CONSOLE]
| Call | Returns | Src |
|---|---|---|
| `GET /v1/billing` | `{user:{balance (toman), accountType, minCreditAmount, isManualMinCredit, minCreditLastNotifiedAt}}`. In team mode the console reads the team balance from `/v1/me` `team.balance` | SPEC |
| `GET /v1/usage-report` | Console "cost estimate" page: `{projects:[{project_id, planID, bundlePlanID, planHourlyPrice, bundlePlanHourlyPrice}], databases:[...], totalMonthlyPrice, ...}`. This is the **current run-rate**, not cost so far | CONSOLE |
| `GET /v1/billing-invoices?page=1&count=10` | `{billingInvoices:[{createdAt,...}], totalBillingInvoices}`. Single invoice: `GET /v1/billing-invoices/{id}` → `{billingInvoice}` | CONSOLE |
| `GET /v1/payments` | `{payments:[{hasInvoice,...}]}`. Single: `/v1/payments/{id}/invoice`, `/v1/payments/{id}/billing-receipt` | CONSOLE |
| `POST /v1/notifications/set-min-credit` | Sets the low-credit alert threshold. [INFER] Body `{minCreditAmount}` | CONSOLE |
| `GET /v1/me/free-credit` | `{remainingFreeCredit}` | SPEC |

- **No "cost so far this month" endpoint was found.**
- [INFER] Two ways to compute spend:
  - Diff `balance` over time.
  - Integrate `usage-report` hourly prices.
- When the balance runs out, services are frozen: deploys return `frozen_project`, and `POST /v1/projects/{name}/unfreeze` reactivates them.

---

## 13. Notifications, alerts, webhooks
- **No webhooks and no app-level alerting** (no CPU, down or deploy-failed alerts) in the spec, CLI or console.
- **Account notifications** [CONSOLE]:
  - `GET /v1/notifications[?unread=true]`, `POST /v1/notifications/{id}/read`, `POST /v1/notifications/read-all`.
  - Channels: `GET/POST /v1/channels`, `GET /v1/channels/{id}`. Body: `{type:"sms"|"email"|"telegram", interestedNotifications:[...], metaData:{phone|email|chatID, token}}`. Tokens come from `/v1/channels/send-phone-token` and `/v1/channels/send-email-token`.
  - Event types: AccountLogin, LowCredit, ServicesHaveBeenFrozen, DomainActivated, SSLProvisioned, SSLRenewFailed, DiskBackupFailed, DatabaseDumpFailed, Ticket*, UnusedFreeServicesNotice, Subscription*, Mailserver*, FreezeFreeVM.
- **For SRE alerting** you must poll:
  - events (oom, die)
  - releases (FAILED, UNHEALTHY)
  - metrics
  - your own HTTP probes
  - balance
- **Remote shell (websocket):** `wss://api.iran.liara.ir/v1/exec?token=<api_token>&cmd=/bin/bash&project_id=<app>&teamID=<team>` [CLI][CONSOLE]. The spec shows the root path `wss://api.liara.ir?...`.
  1. After the socket opens, send `{"action":"start","cols":N,"rows":N}` (the CLI waits 1 s first).
  2. Send input as `{"type":"input","data":"..."}` and resizes as `{"type":"resize",...}`.
  3. Output arrives as raw frames.
  - The app must be running. Packages cannot be installed with apt [DOCS].

---

## 14. Other services (summary) [SPEC]
- **DBaaS** (`api.liara.ir`):
  - `GET/POST /v1/databases`. Create body: `{hostname, type, planID, version, publicNetwork, network?, options:{standaloneReplicaSet}}`.
  - `GET/DELETE /v1/databases/{id}`.
  - `POST .../actions/scale {scale:0|1}`, `POST .../resize {planID, disk:bool}`, `POST .../resize-bundle-plan`.
  - Backups: `GET/POST .../backups`, `POST .../backups/{name}/download`.
  - `PATCH .../settings`, `POST .../control-panel/{enable|disable}`, `PATCH .../public-network/{enable|disable}`.
  - `.../metrics/{cpu|memory|network-*|summary}?since=`, `.../events`, `POST .../unfreeze`.
  - Console-only: `/v2/databases/{id}/reset-password`, `/v2/databases/{id}/operations-status`.
  - Versions come from `/v1/me` `databaseVersions` (postgres, mysql, mariadb, mssql, mongodb, redis, elasticsearch, rabbitmq, clickhouse).
- **Object storage:** buckets, keys, objects and metrics under `storage-service.liara.ir/api/v1`. S3-compatible endpoint for data.
- **Mail:** servers, accounts, SMTP credentials, messages, dns-check and plans under `mail-service.liara.ir/api/v1`.
- **IaaS:** `/vm`, `/vm/{id}`, `PATCH /vm/power/{id}`, `/vm/resize/{id}`, `/vm/operation/{id}`, `/plans`, `/oss`, `/vm/{id}/metrics/{cpu|memory|disk|diskIO}?since=` [CONSOLE].
- **AI:** OpenAI-compatible at `ai.liara.ir/api/{workspaceID}/v1/{chat/completions,embeddings,images/*,audio/*,models}`; workspaces, keys and logs under `ai.liara.ir/v1/...`.
- **Teams (v2):**
  - `GET/POST /v2/teams`
  - `/v2/teams/{id}/members|roles|invitations|assigned-permissions` (paged: `page`, `count`)
  - `/v2/teams/{id}/change-team-name/{name}`, `/v2/teams/{id}/custom-role`, `/v2/teams/{id}/{memberId}/assign-permission`, `/v2/teams/{id}/{grantId}/revoke-permission`
  - `/v2/teams/invitations/join?token=`

---

## 15. CLI command → API map (v9.6.0)
| Command | API |
|---|---|
| `deploy [--app --port --image --detach --no-app-logs --build-arg --build-location --dockerfile --no-cache -d disk:path --message --liara-json]` | GET project → POST v2 sources → POST v2 releases → poll build-logs or v1/releases → GET domains → logs --follow |
| `app:logs [-s "1 hour ago" -f -t -r v12 -l]` | GET v1 project, GET v1/me (retention), GET v2 logs (poll 1 s), GET v1 releases (tag map) |
| `app:start` / `app:stop` / `app:restart` | actions/scale 1, actions/scale 0, actions/restart |
| `app:shell -c cmd` | wss api.iran /v1/exec |
| `app:update-port` | POST v1/projects/update-port |
| `env:list` / `env:set K=V [-f]` / `env:unset K` | GET project / POST update-envs (full list) |
| `disk:create`, `network:create\|list`, `plan:list`, `app:create\|delete\|list` | See the sections above |
| `db:*`, `bucket:*`, `mail:*`, `vm:*`, `zone:*`, `zone:record:*`, `team:*`, `account:*`, `login`, `init`, `whoami` | Respective services |

The CLI has **no rollback command, no metrics command, no domain commands** (even though docs list domain CLI pages), and no release-list command.

---

## 16. Quick answers
| Question | Answer |
|---|---|
| Rollback endpoint? | Yes: `POST /v1/releases/{releaseID}/rollback` [CONSOLE]. Eligible releases: READY with type DEPLOY or ENV_UPDATE, not the current one |
| Build logs and status? | `GET /v2/releases/{id}/build-logs?since=` and `GET /v1/releases/{id}`. States: BUILDING, DEPLOYING, READY, FAILED, UNHEALTHY, CANCELED, TIMEDOUT, plus `queue` |
| Runtime logs? | v2 Loki-style, polled; `start`/`end` in unix seconds; nanosecond timestamps; no websocket. v1 is dead |
| Metrics? | `since` is an absolute unix-seconds timestamp; per-instance series. The summary is a current snapshot plus disk usage |
| Autoscaling / replicas? | None. Scale is 0 or 1. Vertical changes via resize |
| Health check? | liara.json `healthCheck`. It gates the zero-downtime cutover and auto-restarts on runtime failure. Events are Docker container events |
| Domains? | Add domain → ALIAS/CNAME to `target` + TXT `liara-challenge` + CNAME `_acme-challenge` → check → provision SSL. For Liara DNS, set the zone's NS at IRNIC |
| Pricing? | `/v1/me` plans: price in toman per hour; bundle price depends on the plan; hourly billing. Balance via `/v1/billing`; run-rate via `/v1/usage-report` |
| Env update restarts? | Yes. It creates an ENV_UPDATE release. Full replace |
| Upload limit? | 128 MB (Bronze), 256 MB (Silver/Gold). CLI hard cap 256 MiB |
| Webhooks / alerts? | None for apps. Only account notifications (email, SMS, Telegram) for credit, SSL, backups and logins. Team scoping is the `teamID` query param |
