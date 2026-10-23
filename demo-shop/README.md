# demo-shop

فروشگاه نمایشی کوچکی که «نگهبان» روی لیارا از آن مراقبت می‌کند. FastAPI، بدون دیتابیس.

- `GET /` صفحهٔ فروشگاه، `GET /api/products`، `POST /api/checkout`، `GET /api/report` (پرمصرف از نظر CPU)، `GET /healthz`
- متغیر محیطی اجباری: `PAYMENT_GATEWAY_KEY` (بدون آن برنامه بالا نمی‌آید)
- کلیدهای خطا (فقط با هدر `X-Chaos-Token` برابر `CHAOS_TOKEN`): `POST /__chaos/slow?ms=`، `POST /__chaos/leak?target_mb=`، `POST /__chaos/reset`
- تست‌ها: `python -m pytest -q`
