# Limitations

Negahban tells the owner clearly when something cannot be done through Liara's API. The same list is in the app under «محدودیت‌های API».

## Liara platform and API

| Limitation | What Negahban does instead |
|---|---|
| **No horizontal scaling, replicas or autoscaling.** Scale is only 0 or 1. | Negahban's autopilot scales vertically (`resize`) within an owner-set plan range and daily budget, with hysteresis. For horizontal scaling we built **meow-lb** (https://meow-lb.liara.run), our own load balancer with a deterministic replica controller in which each replica is a separate Liara app. On 2026-10-23 at 15 rps it made 2 scale-outs and 2 scale-ins (`evidence/scaling-20261023T065958Z/`). See [SCALING.md](SCALING.md). |
| **Resize or redeploy causes about 20 s of HTTP 502** (basic bundle has no zero-downtime). | Cooldowns and a budget cap. The impact card shows the downtime, and Negahban suggests a zero-downtime bundle. The probe counts 502 as a failure; it is not hidden from the status page. |
| **No webhooks or alerts** for crashes, OOM, failed deploys or resource pressure. | Polls every 60 s: probe, metrics, logs, events, releases. Our own apps can also push events to Negahban's ingest endpoint with a per-app token, and owner-editable alert rules turn metrics and log patterns into incidents ([ALERTS.md](ALERTS.md)). |
| **Rollback is console-only** (`POST /v1/releases/{id}/rollback`, not in the public spec). | Tries it first. Every Negahban deploy is archived, so it can redeploy the previous healthy source. Releases deployed outside Negahban can only be rolled back with Liara's endpoint. |
| **Logs:** at most 100 lines per call, no streaming, retention 7 days on basic. Only **v2** works: `GET /v1/projects/{app}/logs` answers "upgrade your client". | Uses v2 only. Pages backwards and re-reads the boundary second with de-duplication; this fixed lines being lost at page boundaries. Incremental reads each cycle. |
| **Env updates replace the full list and restart the app.** | Merge, then update: reads the current list, changes one key and sends the complete list. The card warns about the restart. |
| **Deploy and build limits (basic bundle):** 20 deploys per app per 24 h (it behaves like a rolling window; standard 50), 128 MB source upload, 5-minute build timeout. Builds occasionally stall at *fetching source* and end `TIMEDOUT` with no build log. | Cooldowns on automatic deploys, and the card shows quota use. The replica controller counts the quota per replica app. A build that times out never moves traffic, so the previous release keeps serving; we then redeploy. |
| **DNS outside Liara** (IRNIC/nic.ir, ArvanCloud, Cloudflare) can't be edited through Liara's API. | The domain wizard shows the exact records and IRNIC nameserver steps, checks status and provisions SSL. The owner adds the records. |
| **Tokens are team-wide**; no scoped or per-app tokens. | Negahban's own write and env locks. |
| **Metric units are undocumented, and `since` is an absolute unix timestamp** (seconds), not a duration. | Values are interpreted as cores and bytes and compared to plan limits. The client always sends an absolute `since`, and sums per timestamp across the old and new containers during a deploy. |
| **No "cost so far" endpoint. Prices are toman per hour.** The monthly `displayPrice` is price × 720; an app costs plan + bundle, and the bundle price depends on the plan. | Shows balance plus the hourly run-rate (`/v1/usage-report`) and estimates hours left. Cost cards compute the hourly and monthly delta as plan + bundle. |
| **Container exec refuses Iranian origins.** Liara's exec gateway answers "connection failed" to sessions from Iran, including Liara's own runtime. | Negahban's terminal and container diagnostics go through the team's exec relay outside Iran, which adds the token itself. If the relay is down, the exec tools report this clearly and everything else keeps working. |
| **Virtual servers (IaaS) can't be deleted within 24 h of creation.** | The create card shows the hourly cost and the 24 h minimum cost. The delete card is refused before `deletable_from`. Root passwords never reach the UI or LLM; a reset password is shown once. |
| **New apps need one week of balance.** `POST /v1/projects` returns 429 ("You need enough balance to cover one week's cost") unless the credit covers 7 × 24 h of the team's run-rate plus the new app. | The setup card warns about this up front. Turn off or delete unused apps, or top up. |
| **healthCheck format is undocumented.** | Verified live: `command` must be an array and the durations are seconds (`startPeriod` ≤ 3000). Templates generate this format. |
| **A brand-new `*.liara.run` address takes about 3 minutes to start routing.** | Verification of a newly created app waits up to 6 minutes before reporting failure (the first live setup-wizard run timed out at the old limit). |
| **Email alerts need a paid mail plan.** The only plan, `m2`, costs 1,475,000 toman a month and needs a domain. | Not purchased. Alerts use the in-app bell and browser notifications. Bale and webhook adapters are ready but disabled until configured. |
| **The runtime is inside Iran.** Large downloads from abroad stall and many foreign hosts are filtered (for example Binance). Anthropic doesn't serve Iran. | The LLM is called through the team's Arvan LLM gate, with automatic BoxAPI fail-over. Container exec uses the relay outside Iran. Our other apps seed big files from a domestic Arvan mirror. |
| **No source-code access for apps deployed outside Negahban.** | The container shell is limited to diagnostics: allow-listed read-only commands run automatically, anything else needs an approval card, and destructive commands are blocked. Code repair only works for apps whose source lives in Negahban's workspace (the demo app). For other apps Negahban diagnoses the problem and proposes platform-level actions. |

## Negahban MVP limits

- **Single owner account.** There is no multi-user RBAC yet.
- **Single process.** The orchestrator and the API share one container. If it dies, the external watchdog restarts it within about 4–6 minutes.
- **Self-repair is aimed at small Python apps** with a test suite. The LLM can be wrong. That is why every patch must pass the tests, is shown as a diff, needs approval (unless the owner enabled auto-fix) and is verified after deploy.
- **Test sandbox isolation depends on the host.** `unshare -rn` (no network) is used where the kernel allows it. Inside Liara containers it usually isn't, so the sandbox relies on the scrubbed env, ulimits and a timeout.
- **Latency is measured from inside Liara.** The external probe on Hetzner sees about 2 s extra round-trip from Finland, so external numbers look slower.
- **LLM provider.**
  - Anthropic access goes through the team gate, which has a time window ending 2026-10-25 16:00 UTC and a kill switch. Negahban fails over to BoxAPI automatically.
  - If both providers are down, the deterministic diagnosis, autopilot and approvals still work; only the chat and the natural-language summaries stop.
