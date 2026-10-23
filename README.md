# 🛡️ نگهبان (Negahban): همکار هوشمند SRE برای سایت‌های روی لیارا

[![CI](https://github.com/moazeniz/negahban/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/moazeniz/negahban/actions/workflows/ci.yml) [![Deploy](https://github.com/moazeniz/negahban/actions/workflows/deploy.yml/badge.svg)](https://github.com/moazeniz/negahban/actions/workflows/deploy.yml) [![Liara](https://img.shields.io/badge/Liara-negahban--sre-5a2be2)](https://negahban-sre.liara.run) [![status](https://img.shields.io/badge/status-page-success)](https://negahban-sre.liara.run/status) ![secrets](https://img.shields.io/badge/gitleaks-full%20history-informational) ![sbom](https://img.shields.io/badge/SBOM-SPDX%20%2B%20CycloneDX-informational)

**نسخه‌ی زنده:** https://negahban-sre.liara.run · **برنامه‌ی نمایشی:** https://demo-shop.liara.run · **وضعیت عمومی:** https://negahban-sre.liara.run/status

## فارسی

نگهبان مثل یک همکار فنی کنار صاحب سایت کار می‌کند. سایت را راه می‌اندازد و مستقر می‌کند، آن را شبانه‌روز پایش می‌کند، علت خطا را پیدا و رتبه‌بندی می‌کند، راه‌حل پیشنهاد می‌دهد و **با اجازه‌ی شما** آن را اجرا می‌کند. بعد از اجرا هم نتیجه را خودش راستی‌آزمایی می‌کند.

- **راه‌اندازی اولیه با یک کارت**: آدرس git یا فایل zip را بدهید (از داشبورد یا با جمله‌ای مثل «این ریپو رو دیپلوی کن»). نگهبان پروژه را تحلیل می‌کند، پلن را با قیمت و دلیل انتخاب پیشنهاد می‌دهد و مقادیر متغیرها را می‌گیرد؛ مقادیر محرمانه پنهان می‌مانند. بعد از تأیید شما، برنامه را می‌سازد و مستقر می‌کند، لاگ بیلد را زنده نشان می‌دهد، سلامت را بررسی می‌کند، زیردامنه یا راهنمای دامنه را می‌دهد و پروژه را به‌عنوان قالب ذخیره می‌کند.
- **هسته‌ی قاعده‌محور + هوش مصنوعی اختیاری**: پایش، مقیاس خودکار، ری‌استارت، قوانین هشدار، اعلان‌ها، صفحه‌ی وضعیت و گزارش روزانه بدون مدل زبانی کار می‌کنند. با کلید «حالت بدون هوش مصنوعی» می‌توان هوش مصنوعی را خاموش کرد. با رسیدن به سقف هزینه‌ی مدل زبانی، این حالت خودکار فعال می‌شود.
- **قوانین هشدار و اعلان‌ها**: قوانین متریک و الگوی لاگ را خودتان تعریف می‌کنید. برنامه‌ها می‌توانند رویدادهای مهم را فوراً به نگهبان بفرستند. اعلان‌ها در داشبورد و مرورگر نمایش داده می‌شوند و اگر دیده نشوند یادآوری می‌شوند. برای ارسال اعلان، بله، وب‌هوک و سرویس ایمیل لیارا آماده‌اند و پس از تنظیم فعال می‌شوند.
- **گزارش روزانه و معرفی گام‌به‌گام** برای کاربران تازه.
- **راه‌اندازی و استقرار**
  - تحلیل پروژه (zip یا git) و پیشنهاد پلن با قیمت واقعی لیارا.
  - ساخت برنامه و استقرار با کارت تأیید.
  - کتابخانه‌ی ۹ قالب آماده‌ی استقرار، به‌اضافه‌ی ذخیره‌ی خودکار هر پروژه‌ی جدید به‌صورت قالب.
  - راهنمای دامنه و رکوردهای DNS و ایرنیک.
- **پایش و نگهداری**
  - هر ۶۰ ثانیه: سلامت HTTP، CPU و RAM و شبکه، لاگ، رویدادهای کانتینر و وضعیت ریلیز.
  - پیش‌بینی نشت حافظه پیش از OOM.
  - هزینه و اعتبار تیم.
- **عیب‌یابی هوشمند**
  - موتور قطعی تشخیص که این موارد را رتبه‌بندی‌شده و همراه با شواهد گزارش می‌کند:
    - OOM و نشت حافظه؛
    - پر شدن CPU؛
    - crash loop؛
    - نبود متغیر محیطی؛
    - شکست بیلد؛
    - خطای کد پس از یک ریلیز؛
    - کندی؛
    - مشکل سرویس وابسته.
  - مدل زبانی توضیح ساده می‌نویسد و موارد تازه را تحلیل می‌کند.
  - ابزارهای عمیق‌تر: ترمینال کانتینر، کاوشگر لاگ ساخت‌یافته، TLS، DNS، runbook و جستجوی وب برای خطاهای ناشناخته.
- **سرورهای مجازی (IaaS لیارا)**: فهرست، جزئیات و ترافیک سرورها؛ ساخت سرور از قالب، روشن و خاموش کردن، تغییر پلن، دیسک و رمز یک‌بارمصرف. همه‌ی این کارها با کارت تأیید انجام می‌شود و حداقل هزینه‌ی ۲۴ ساعته روی کارت نمایش داده می‌شود.
- **چند تیم لیارا**: تیم اصلی (دوخت) و یک تیم آزمایشی (لیارا) در یک داشبورد. انتخابگر تیم در بالای صفحه نام و موجودی هر تیم را نشان می‌دهد. هر کارت تأیید تیم خودش را با رنگ مشخص نشان می‌دهد، و بررسی موجودی یک‌هفته‌ای برای هر تیم جداست. در گفتگو بگویید «در تیم لیارا …».
- **گیت‌آپس (GitHub + CI/CD)**: برای هر برنامه‌ی دارای مخزن، وضعیت آخرین CI، نسخه‌ی روی لیارا در برابر main («N کامیت عقب‌تر»)، «چه چیزی تغییر کرده؟» و ارتباط استقرار با رخداد را نشان می‌دهد. انتشار نسخه‌ی جدید از طریق workflow «Deploy to Liara» همان مخزن (CI، تست دود، بازگشت خودکار) و با کارت تأیید انجام می‌شود. دسترسی نگهبان به گیت‌هاب در کد محدود شده است: فقط خواندن و اجرای workflow استقرار؛ هیچ کامیت یا PR ای نمی‌سازد.
- **پایگاه‌داده و ذخیره‌سازی ابری**: فهرست، ساخت، اتصال به برنامه، پشتیبان‌گیری و بازیابی پایگاه‌داده‌ها و باکت‌ها؛ همه با کارت تأیید و نمایش هزینه‌ی ساعتی و ماهانه.
- **حافظه‌ی بلندمدت**: واقعیت‌ها و ترجیحات مالک، علت و راه‌حل رخدادهای گذشته (که در تشخیص بعدی هم استفاده می‌شود) و خلاصه‌ی گفتگوهای قبلی.
- **خودترمیمی کد** (برای برنامه‌ی نمایشی): بازتولید خطا با تست، نوشتن وصله، تست در سندباکس، کارت تأیید با diff، استقرار و راستی‌آزمایی.
- **خودران با بودجه**: ارتقا و کاهش خودکار پلن در بازه و سقف تومانی تعیین‌شده توسط مالک، همراه با دوره‌ی خنک‌سازی.
- **انسان در حلقه**: هر کار حساس فقط با تأیید شما انجام می‌شود. کارت‌ها:
  - یک‌بارمصرف‌اند؛
  - تاریخ انقضا دارند؛
  - به وضعیت برنامه گره خورده‌اند؛
  - برای برنامه‌های حساس مثل ربات معامله قفل «فقط پایش» دارند.
- **صداقت درباره‌ی محدودیت‌ها**: صفحه‌ای جدا می‌گوید چه کارهایی با API لیارا ممکن نیست.

## English summary

Negahban is an AI SRE and deployment assistant for websites hosted on Liara (Iranian PaaS). It is a FastAPI service with a Persian RTL dashboard and runs on Liara itself. Its main features:

- **Always-on orchestrator.** Detects incidents from probes, metrics, paged logs, Docker events and release states.
- **Deterministic troubleshooting engine.** Produces ranked root causes with evidence; the LLM explains them and handles novel cases.
- **Approval cards for every sensitive action.** Each card is one-use, expires, is bound to an app-state fingerprint, and shows downtime and cost impact. Every approved action gets automatic post-action verification.
- **Autopilot autoscaling within an owner budget.** Vertical resize with hysteresis and cooldowns.
- **Self-repair of code** with sandboxed tests and a diff on the approval card.
- **Templates library** of deploy-ready Liara stacks, plus virtual-server templates (Liara IaaS: create, power, resize, disks and a one-time password reveal, all behind approval cards).
- **Long-term memory.** Owner facts, incident memory (reused in diagnoses) and summaries of past sessions.
- **Diagnostics toolkit.** Container exec via a relay, TLS/DNS/HTTP probes, Liara data sources, anomaly detection, SLO, runbooks and web search.
- **Observability.** Structured JSON logs and request tracing; circuit breakers and retries; a durable job queue with dead-letter and replay.
- **MCP server** exposing the same tools.
- **External watchdog** that keeps Negahban itself alive.

LLM: Anthropic `claude-haiku-5-5` through the team gate (default), with automatic fail-over to BoxAPI `gpt-6-luna`.

### New in the final round
- **Initial-setup wizard:** analyse → recommend → one card → live build log → verify → domain → template.
- **Deterministic control plane with an AI-off switch.** Proven live: autopilot resize with 0 LLM calls.
- **Guardrail rules, push ingest and notifications** with escalation.
- **Self-guardrails:** LLM spend caps and an automatic-action cap.
- **Supervised loops, load shedding, durable snapshots** with off-site pull and a tested restore.
- **External watchdog revive,** proven live in about 4 minutes.
- **Token and cost logging plus prompt caching:** −54 % input cost.
- **Daily report** and a **first-run tour.**
- **Horizontal-scaling view and override cards** for the platform's deterministic replica controller.
- **Multiple Liara teams**: production «دوخت» plus a test team «لیارا». A header switcher shows each team with its balance. Every card shows its team in a coloured band, the one-week precheck runs per team, and wiring resources across teams is refused.
- **GitOps**: CI/CD status per app, live commit vs main, what-changed diffs, deploy↔incident correlation, and releases through the repo's own «Deploy to Liara» workflow behind an approval card. A code-level allowlist limits GitHub access to reads plus that one dispatch.
- **Managed databases and object storage**: list, create, connect, back up and restore databases and buckets, all through approval cards with hourly and monthly cost.

See [docs/EVIDENCE.md](docs/EVIDENCE.md) for the criterion → feature → live-proof matrix and [docs/CONTROL_PLANE.md](docs/CONTROL_PLANE.md).

### Documentation
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data model, request lifecycle (with a mermaid diagram).
- [docs/LIARA_API.md](docs/LIARA_API.md): every Liara endpoint used, quirks and how they are handled.
- [docs/USER_GUIDE.md](docs/USER_GUIDE.md): user guide (Persian).
- [docs/SECURITY.md](docs/SECURITY.md): secrets, auth, human-in-the-loop, prompt-injection defences.
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md): what Liara's API can't do, and MVP limits.
- [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md): the diagnostics toolkit.

### Repository layout
```
negahban/        FastAPI app: liara.py (client), tools.py (registry), approvals.py, diagnose.py, orchestrator.py,
                 selffix.py, workspace.py, deployer.py, agent.py, llm.py, templates*.py, jobs.py, obs.py, resilience.py,
                 mcp.py, diag/ (diagnostics toolkit)
static/          dashboard (vanilla JS, CSP script-src 'self'; vendored font, Pele diagrams, own SVG charts)
demo-shop/       demo target app (FastAPI store + tests + token-protected fault switches)
tests/           pytest suite (mocked Liara/LLM/exec; 200+ tests)
deploy/          deploy_negahban.py (Liara API deploy), watchdog.py + systemd units, exec_relay.py
docs/            documentation
evidence/        live E2E evidence (JSON + screenshots)
```

### Run locally
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export LIARA_API_TOKEN=... LIARA_TEAM_ID=... ADMIN_PASSWORD=... BOXAPI_KEYS=... DATA_DIR=./data SECURE_COOKIES=0
.venv/bin/uvicorn negahban.main:app --port 8080
.venv/bin/python -m pytest -q tests
```

### Deploy to Liara
```bash
python deploy/deploy_negahban.py "message"
```
This reads the 600-mode env files on the operator host, makes sure the `data` disk exists, uploads the source and creates the release with its env vars and disk.

<!-- platform:begin -->
## Delivery and operations

See **[docs/PLATFORM.md](docs/PLATFORM.md)** for the platform and **[docs/SCALING.md](docs/SCALING.md)** for horizontal scaling: the architecture, the CI/CD pipeline with automatic rollback, infrastructure as code (`infra/liara.yaml`), health checks, SLOs and the status page, backups and restore, security posture, runbooks, and why the setup is production-grade.
<!-- platform:end -->
