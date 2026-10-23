import os

os.environ.setdefault("PAYMENT_GATEWAY_KEY", "test-key")
os.environ.setdefault("CHAOS_TOKEN", "test-chaos")

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402
from shop.catalog import BY_ID, PRODUCTS  # noqa: E402
from shop.pricing import cart_total, discount_percent, fmt_price  # noqa: E402

client = TestClient(app)


def test_discount_on_sale():
    assert discount_percent({"price": 90, "old_price": 100}) == 10


def test_discount_without_old_price():
    assert discount_percent({"price": 90, "old_price": None}) == 0
    assert discount_percent({"price": 90, "old_price": 0}) == 0
    assert discount_percent({"price": 90}) == 0


def test_fmt_price_persian_digits():
    assert fmt_price(12500000) == "۱۲٬۵۰۰٬۰۰۰ تومان"


def test_cart_total():
    assert cart_total([{"id": 1, "qty": 2}, {"id": 2}], BY_ID) == 2 * BY_ID[1]["price"] + BY_ID[2]["price"]


def test_home_renders_every_product():
    r = client.get("/")
    assert r.status_code == 200
    for p in PRODUCTS:
        assert p["name"] in r.text


def test_products_api():
    r = client.get("/api/products")
    assert r.status_code == 200 and len(r.json()) == len(PRODUCTS)


def test_checkout():
    r = client.post("/api/checkout", json={"items": [{"id": 1}]})
    assert r.status_code == 200 and r.json()["total"] == BY_ID[1]["price"]
    assert client.post("/api/checkout", json={"items": [{"id": 999}]}).status_code == 400


def test_chaos_requires_token():
    assert client.post("/__chaos/slow").status_code == 403
    assert client.post("/__chaos/slow?ms=0", headers={"X-Chaos-Token": "test-chaos"}).status_code == 200
