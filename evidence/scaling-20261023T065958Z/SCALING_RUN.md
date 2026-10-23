# Live scaling run: 2026-10-23T06:59:58Z

Load: 15 rps for 12 min against https://meow-lb.liara.run (load generator on Hetzner, outside Liara).

## Result

- Requests: **10800**, of which 10335 were 2xx (95.69 %); status codes: `{'200': 10335, 'ServerDisconnectedError': 2, '500': 462, 'TimeoutError': 1}`
- Split by backend (`X-Backend` header): `{'demo-shop': 9111, '-': 3, 'demo-shop-r1': 1686}`
- Instances during the run: max 2, final 1

## Controller audit (deterministic, no LLM)

| Time (UTC) | Event | Result | Reason |
|---|---|---|---|
| 06:59:47 | scale_in demo-shop-r1 | started | calm for 120 s (rps/backend 0.0 < 1.0) |
| 06:59:47 | delete_app demo-shop-r1 | done | calm for 120 s (rps/backend 0.0 < 1.0) |
| 06:59:47 | scale_in demo-shop-r1 | done | calm for 120 s (rps/backend 0.0 < 1.0) |
| 07:07:50 | scale_out demo-shop-r1 | started | sustained 437 s: rps/backend 15.0 > 5.0 |
| 07:08:45 | scale_out demo-shop-r1 | done | sustained 437 s: rps/backend 15.0 > 5.0 |
| 07:10:51 | scale_out demo-shop-r2 | started | sustained 619 s: rps/backend 7.5 > 5.0 |
| 07:10:52 | scale_out demo-shop-r2 | failed | create demo-shop-r2: HTTP 429 (Liara 429: one-week balance rule) |
| 07:14:53 | scale_in demo-shop-r1 | started | calm for 120 s (rps/backend 0.0 < 1.0) |
| 07:14:54 | delete_app demo-shop-r1 | done | calm for 120 s (rps/backend 0.0 < 1.0) |
| 07:14:54 | scale_in demo-shop-r1 | done | calm for 120 s (rps/backend 0.0 < 1.0) |

## Decision changes (from /scaler/status every 15 s)

| Time (UTC) | Instances | rps / backend | Decision |
|---|---|---|---|
| 06:59:59 | 1 | 0.0 | calm for 120 s (rps/backend 0.0 < 1.0) |
| 07:00:15 | 1 | 3.88 | within band |
| 07:00:46 | 1 | 11.75 | load high (rps/backend 8.2 > 5.0), waiting for 60 s sustain |
| 07:01:34 | 1 | 15.0 | load high (rps/backend 15.0 > 5.0) but cooldown (15 s left) |
| 07:01:49 | 1 | 15.0 | load high (rps/backend 15.0 > 5.0) but Liara one-week balance rule: needs 2,086,380 toman, balance 1,814,949 |
| 07:07:51 | 2 | 15.0 | sustained 437 s: rps/backend 15.0 > 5.0 |
| 07:08:07 | 2 | 14.98 | waiting for create demo-shop-r1 |
| 07:08:54 | 2 | 7.49 | load high (rps/backend 7.5 > 5.0) but cooldown (114 s left) |
| 07:11:00 | 2 | 7.51 | sustained 619 s: rps/backend 7.5 > 5.0 |
| 07:11:16 | 2 | 7.5 | load high (rps/backend 7.5 > 5.0) but cooldown (105 s left) |
| 07:12:50 | 2 | 1.13 | within band |
| 07:14:56 | 1 | 0.0 | calm for 120 s (rps/backend 0.0 < 1.0) |
| 07:15:12 | 1 | 0.0 | within band |

## Traffic split per 10 s window while two backends were live

| t (s) | requests | p95 ms | per backend |
|---|---|---|---|
| 20 | 150 | 147.2 | {'demo-shop': 149, '-': 1} |
| 520 | 149 | 152.3 | {'demo-shop': 148, '-': 1} |
| 530 | 151 | 137.2 | {'demo-shop': 116, 'demo-shop-r1': 35} |
| 540 | 150 | 134.8 | {'demo-shop': 75, 'demo-shop-r1': 75} |
| 550 | 150 | 140.1 | {'demo-shop': 75, 'demo-shop-r1': 75} |
| 560 | 150 | 137.7 | {'demo-shop': 75, 'demo-shop-r1': 75} |
| 570 | 150 | 138.3 | {'demo-shop': 75, 'demo-shop-r1': 75} |
| 580 | 150 | 137.1 | {'demo-shop': 75, 'demo-shop-r1': 75} |
| 590 | 150 | 144.8 | {'demo-shop': 75, 'demo-shop-r1': 75} |
| 600 | 150 | 140.1 | {'demo-shop': 75, 'demo-shop-r1': 75} |
| 610 | 150 | 137.3 | {'demo-shop': 75, 'demo-shop-r1': 75} |
| 620 | 150 | 141.6 | {'demo-shop': 75, 'demo-shop-r1': 75} |
| 630 | 149 | 135.4 | {'demo-shop': 74, 'demo-shop-r1': 75} |
| 660 | 151 | 139.6 | {'demo-shop-r1': 150, '-': 1} |
| 670 | 150 | 144.9 | {'demo-shop-r1': 76, 'demo-shop': 74} |
| 680 | 150 | 139.7 | {'demo-shop-r1': 75, 'demo-shop': 75} |
| 690 | 150 | 140.7 | {'demo-shop-r1': 75, 'demo-shop': 75} |
| 700 | 150 | 139.4 | {'demo-shop-r1': 75, 'demo-shop': 75} |
| 710 | 150 | 139.4 | {'demo-shop-r1': 75, 'demo-shop': 75} |

## Load balancer counters at the end

| Backend | Healthy | Requests | Errors |
|---|---|---|---|
| demo-shop | True | 9113 | 463 |

Non-2xx during the run: `{'ServerDisconnectedError': 2, '500': 421, 'TimeoutError': 1}`.
