"""فروشگاه نمایشی — the demo target app that Negahban watches, diagnoses and repairs."""
import html, math, os, time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from shop import chaos
from shop.catalog import BY_ID, PRODUCTS
from shop.pricing import cart_total, discount_percent, fmt_price

PAYMENT_GATEWAY_KEY = os.environ["PAYMENT_GATEWAY_KEY"]  # required: the shop cannot take payments without it
VERSION = os.environ.get("APP_VERSION", "1.0.0")
app = FastAPI(title="demo-shop")


@app.middleware("http")
async def timing(request: Request, call_next):
    t0 = time.perf_counter()
    if chaos.STATE["slow_ms"] and not request.url.path.startswith(("/__chaos", "/healthz")):
        import asyncio
        await asyncio.sleep(chaos.STATE["slow_ms"] / 1000)
    resp = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    if ms > 1000:
        print(f"WARNING slow request path={request.url.path} status={resp.status_code} took={ms:.0f}ms", flush=True)
    return resp


def product_view(p):
    return {**p, "discount": discount_percent(p), "price_fa": fmt_price(p["price"])}


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION}


@app.get("/api/products")
def products():
    return [product_view(p) for p in PRODUCTS]


@app.post("/api/checkout")
def checkout(body: dict):
    items = body.get("items") or []
    if not items or any(it.get("id") not in BY_ID for it in items):
        raise HTTPException(400, "سبد خرید نامعتبر است")
    return {"total": cart_total(items, BY_ID), "currency": "IRT", "paid": bool(PAYMENT_GATEWAY_KEY)}


@app.get("/api/report")
def report(n: int = 40000):
    """Sales report (CPU-heavy on purpose: it is what a traffic spike hits)."""
    n = max(1000, min(n, 200000))
    sieve = [True] * n
    for i in range(2, int(math.isqrt(n)) + 1):
        if sieve[i]:
            sieve[i * i::i] = [False] * len(range(i * i, n, i))
    s = sum(i for i in range(2, n) if sieve[i])
    return {"orders": len(PRODUCTS) * 17, "checksum": s}


@app.get("/", response_class=HTMLResponse)
def home():
    cards = []
    for p in map(product_view, PRODUCTS):
        badge = f'<span class="off">{p["discount"]}٪ تخفیف</span>' if p["discount"] else ""
        cards.append(f'<div class="card"><div class="em">{p["emoji"]}</div><h3>{html.escape(p["name"])}</h3>{badge}<b>{p["price_fa"]}</b></div>')
    return f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>فروشگاه نمونه</title><style>body{{font-family:Tahoma,sans-serif;background:#f6f7fb;margin:0;color:#1d2433}}header{{background:#0f766e;color:#fff;padding:18px 20px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px;padding:18px}}.card{{background:#fff;border-radius:14px;padding:16px;box-shadow:0 1px 3px #0001}}
.em{{font-size:34px}}h3{{font-size:15px;margin:8px 0}}.off{{background:#fee2e2;color:#b91c1c;border-radius:8px;padding:2px 8px;font-size:12px}}b{{display:block;margin-top:8px}}
footer{{color:#64748b;font-size:12px;padding:0 20px 20px}}</style></head><body><header><h1>🛍️ فروشگاه نمونه</h1>اپلیکیشن نمایشی که «نگهبان» از آن مراقبت می‌کند</header>
<main>{''.join(cards)}</main><footer>نسخه {html.escape(VERSION)}</footer></body></html>"""


# ------------------------------------------------------------------ chaos switches (token protected)
def _guard(token):
    if not chaos.authorised(token or ""):
        raise HTTPException(403, "forbidden")


@app.get("/__chaos/state")
def chaos_state(x_chaos_token: str = Header(default="")):
    _guard(x_chaos_token)
    return chaos.STATE


@app.post("/__chaos/slow")
def chaos_slow(ms: int = 3500, x_chaos_token: str = Header(default="")):
    _guard(x_chaos_token)
    chaos.STATE["slow_ms"] = max(0, min(ms, 20000))
    print(f"chaos: slow mode {chaos.STATE['slow_ms']}ms", flush=True)
    return chaos.STATE


@app.post("/__chaos/leak")
def chaos_leak(target_mb: int = 700, x_chaos_token: str = Header(default="")):
    _guard(x_chaos_token)
    chaos.start_leak(max(50, min(target_mb, 4000)))
    return chaos.STATE


@app.post("/__chaos/reset")
def chaos_reset(x_chaos_token: str = Header(default="")):
    _guard(x_chaos_token)
    chaos.reset()
    return chaos.STATE

