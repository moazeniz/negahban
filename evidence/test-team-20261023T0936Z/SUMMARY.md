# Test-team end-to-end run, 2026-10-23 09:36–09:56 UTC

Separate Liara team «لیارا» (`6accfde10841df6d775a401b`), driven through a local **staging** Negahban v1.10.0 on Hetzner
(port 18940, own data dir `/root/ngh-staging-data`, `LIARA_API_TOKEN_2`, LLM via the Anthropic gate). Every change went
through Negahban's real approval cards (`/api/diag/run` / `/api/projects/*` → approve, or typed approval). The main team was not touched.
No VMs were created.

| # | Test | Result | Time (from approve) | Evidence |
|---|---|---|---|---|
| 1 | Setup wizard: `github.com/render-examples/fastapi` → `ngh-test-app` small-g2 basic | READY + verified (HTTP 200, 349 ms, 0 5xx) after 2 bugs fixed | 141 s (build 75 s + routing/verify 66 s); analysis 1.4 s | `1-launch-*.json`, `shots/*launch-card.png` |
| 2 | Postgres small-g2 basic `ngh-test-pg` (18.4): create + connect | RUNNING + 6 env names injected; values masked in the dashboard; no secret in API views, audit, proposals, server log or SQLite | 70 s | `2-db.json`, `2-db-verify.json`, `2-db-round2.json` |
| 2b | Inspector query | Liara's inspector refuses Postgres ("not a supported database server") → clean Persian message | 5 s | `2-db-round2.json` |
| 2c | Manual backup | basic bundle has none → now refused when the card is created | – | `2-db-round2.json` |
| 2d | Typed delete | wrong name and plain approve refused; exact name → deleted | 5 s | `2-db-delete.json` |
| 3 | Bucket 20g-g2 private + connect (scoped key) + owner key → S3 upload → objects list → temp link (fetched, content matches) → public → private → empty → key revoke/delete → typed delete | all passed in round 2 after 3 bugs fixed; no secret leaked | 70 s for the whole sequence | `3-storage.json` (round 1), `3-storage-round2.json` |
| 4 | Add-ons «this project needs Postgres» | `propose_addons` made DB + bucket cards (rejected); the chat answer noticed the app already had Postgres and made no card. Fixed `propose_addons` to skip connected apps the same way | chat 24 s | `4-addons.json` |
| 5 | Delete `ngh-test-app` (card created from chat, typed name) and check the team | deleted; team has 0 apps, 0 databases, 0 buckets, 0 keys, 0 VMs; one free private network `negahban-net` remains (Liara has no network delete endpoint) | 5 s | `5-cleanup.json` |

**Cost:** balance 2,000,000 → 1,997,402.8 toman = **2,597 toman** for the whole run (≈20 minutes of app + Postgres + bucket).

## Bugs found and fixed (all in /opt/negahban, with regression tests)

1. `create_app` sent an empty `network` in a team without a private network → Liara 400 `"network" is not allowed to be empty`.
   Now: `LIARA_NETWORK_ID`, else the team's first network, else a new free `negahban-net` (`POST /v1/networks {networkName}`; the field name was a 2nd bug). Same for g2 databases.
2. One-time reveal links (`secret_once`) were masked to `***` by the generic redaction (key contains "secret") → the dashboard's "show once" button could never work for DB passwords, storage keys, temp links or VM passwords.
3. S3 endpoint was `storage.iran.liara.space` (does not answer); the live one is the bucket's `cluster.endPoint` = `storage.iran.liara.site`. Apps would have received a wrong `LIARA_ENDPOINT`. Now read from the bucket.
4. Object listing sent `page=1` → storage-service 500 → also blocked the empty-check before `delete_bucket`.
5. `DB_NAME` was empty and `DATABASE_URL` ended with `/` for a fresh Postgres → now the server default (`postgres`).
6. Database price per hour: details showed the plan part only (659.72) while the list showed plan+bundle (1041.66) → both total now. Delete card showed "- تومان" → now the saved amount.
7. Backup card on a basic bundle was accepted and then failed → refused at card time with the reason.
8. Inspector "not a supported database server" → clean Persian error.
9. `propose_addons` proposed a second database/bucket for an app that already had `DATABASE_URL` / storage variables → skipped with a note.
