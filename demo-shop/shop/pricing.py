"""Price helpers for the store."""


def discount_percent(p):
    """Percentage off the old price; 0 when the product is not on sale."""
    old = p.get("old_price") or 0
    if not old or old <= p["price"]:
        return 0
    return round(100 * (old - p["price"]) / old)


def fmt_price(v):
    """12500000 -> '۱۲٬۵۰۰٬۰۰۰ تومان'"""
    digits = str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬")
    return f"{int(v):,}".translate(digits) + " تومان"


def cart_total(items, catalogue):
    total = 0
    for it in items:
        p = catalogue[it["id"]]
        total += p["price"] * int(it.get("qty", 1))
    return total
