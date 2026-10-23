# Limitations

Negahban tells the owner clearly when something cannot be done through Liara's API. The same list is in the app under «محدودیت‌های API».

## Liara platform and API

| Limitation | What Negahban does instead |
|---|---|
| **No horizontal scaling, replicas or autoscaling.** Scale is only 0 or 1. | "Autoscaling" means vertical `resize` within an owner-set plan range and daily budget, with hysteresis. |
| **Resize causes about 20 s of HTTP 502** (basic bundle has no zero-downtime). | Cooldowns and a budget cap. The impact card shows the downtime, and Negahban suggests a zero-downtime bundle. |
| **No webhooks or alerts** for crashes, OOM, failed deploys or resource pressure. | Polls every 60 s: probe, metrics, logs, events, releases. |
| **Rollback is console-only** (`POST /v1/releases/{id}/rollback`, not in the public spec). | Tries it first. Every Negahban deploy is archived, so it can redeploy the previous healthy source. Releases deployed outside Negahban can only be rolled back with Liara's endpoint. |
| **Logs:** at most 100 lines per call, no streaming, retention 7 days on basic. | Pages backwards; incremental reads each cycle. |
| **Env updates replace the full list and restart the app.** | Always sends the complete list. The card warns about the restart. |
| **Deploy quota:** basic 20/day, standard 50/day. | Cooldowns on automatic deploys. The card shows quota use. |
| **DNS outside Liara** (IRNIC/nic.ir, ArvanCloud, Cloudflare) can't be edited through Liara's API. | The domain wizard shows the exact records and IRNIC nameserver steps, checks status and provisions SSL. The owner adds the records. |
| **Tokens are team-wide**; no scoped or per-app tokens. | Negahban's own write and env locks. |
| **Metric units are undocumented.** | Interpreted as cores and bytes, compared to plan limits. |
| **No "cost so far" endpoint.** | Shows balance plus the hourly run-rate (`/v1/usage-report`) and estimates hours left. |
| **Container exec refuses Iranian origins.** Liara's exec gateway answers "connection failed" to sessions from Iran, including Liara's own runtime. | Negahban's terminal and container diagnostics go through the team's exec relay outside Iran, which adds the token itself. If the relay is down, the exec tools report this clearly and everything else keeps working. |
| **Virtual servers (IaaS) can't be deleted within 24 h of creation.** | The create card shows the hourly cost and the 24 h minimum cost. The delete card is refused before `deletable_from`. Root passwords never reach the UI or LLM; a reset password is shown once. |
| **New apps need one week of balance.** `POST /v1/projects` returns 429 ("You need enough balance to cover one week's cost") unless the credit covers 7 × 24 h of the team's run-rate plus the new app. | The setup card warns about this up front. Turn off or delete unused apps, or top up. |
| **healthCheck format is undocumented.** | Verified live: `command` must be an array and the durations are seconds (`startPeriod` ≤ 3000). Templates generate this format. |
| **No source-code access for apps deployed outside Negahban.** | The container shell is limited to diagnostics: allow-listed read-only commands run automatically, anything else needs an approval card, and destructive commands are blocked. Code repair only works for apps whose source lives in Negahban's workspace (the demo app). For other apps Negahban diagnoses the problem and proposes platform-level actions. |

**Data services (verified live in a test team, 2026-10-23):**
- Liara's database inspector refuses Postgres ("not a supported database server"), so read-only queries through Negahban do not work for Postgres databases.
- The basic database bundle has no manual backups; the backup card is refused up front and suggests the standard/pro bundle.
- A team needs a private network for g2 apps and databases. Negahban uses the team's first network or creates a free `negahban-net`; Liara has no API to delete a network.
- The S3 endpoint is the bucket's `cluster.endPoint` (`storage.iran.liara.site`); it is reachable from outside Iran, unlike some Liara endpoints.

## Negahban MVP limits

- **Multi-team.** Policies and locks are keyed by app name. That is safe because Liara app names are global, but database names are per team.
  The status page, horizontal scaling and GitOps cover the production team only. Test-team apps are monitored read-only (autopilot off by default).

- **GitHub token scope (GitOps).** The token provided (`NEGAHBAN_GITHUB_TOKEN`, a Liara env var on `negahban-sre`, read from a 600-mode file on Hetzner) has more power than Negahban needs. Negahban restricts it in code: `negahban/gitops.py` allows only read endpoints (repo, commits, compare, tags, releases, workflow runs/jobs/logs, check runs, statuses, the deploy workflow file) and exactly one write, `POST /repos/moazeniz/{bazargard|negahban|wallex-ai-trader}/actions/workflows/deploy.yml/dispatches`. Everything else is refused before any network call (`tests/test_gitops.py`, 27 blocked routes). Negahban never commits, pushes, or creates files, branches or PRs in these public repos. A token with only `actions:write` + `contents:read` on the three repos would remove this residual risk.
- **Live commit of older releases is unknown.** Releases deployed before the GitHub pipeline existed carry no `gitInfo`, and the public repos have fresh history, so "N commits behind main" shows «نامشخص» until the first release through the pipeline. `meowmeow-wallex` is monitor-only: Negahban never dispatches its deploy.
- **GitHub "production" environment.** The deploy job runs in the repo's `production` environment. If reviewers are required there, the run waits for approval in GitHub after the Negahban card is approved; Negahban reports the wait.

- **Single owner account.** There is no multi-user RBAC yet.
- **Single process.** The orchestrator and the API share one container. If it dies, the external watchdog restarts it within about 4–6 minutes.
- **Self-repair is aimed at small Python apps** with a test suite. The LLM can be wrong. That is why every patch must pass the tests, is shown as a diff, needs approval (unless the owner enabled auto-fix) and is verified after deploy.
- **Test sandbox isolation depends on the host.** `unshare -rn` (no network) is used where the kernel allows it. Inside Liara containers it usually isn't, so the sandbox relies on the scrubbed env, ulimits and a timeout.
- **Latency is measured from inside Liara.** The external probe on Hetzner sees about 2 s extra round-trip from Finland, so external numbers look slower.
- **LLM provider.**
  - Anthropic access goes through the team gate, which has a time window ending 2026-10-25 16:00 UTC and a kill switch. Negahban fails over to BoxAPI automatically.
  - If both providers are down, the deterministic diagnosis, autopilot and approvals still work; only the chat and the natural-language summaries stop.
