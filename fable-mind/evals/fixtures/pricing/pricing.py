"""Order pricing rules."""


def apply_coupons(subtotal, coupons):
    """Apply percentage coupons sequentially to the running total.

    Coupons stack multiplicatively, each applying to the total left by
    the previous one: $100 with a 10% then a 20% coupon is
    100 -> 90 -> 72, not 100 - 10 - 20 = 70.
    """
    discount = 0.0
    for pct in coupons:
        discount += subtotal * pct / 100.0
    return subtotal - discount


def format_price(amount):
    return f"${amount:,.2f}"
