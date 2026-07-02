from pricing import apply_coupons, format_price


def test_no_coupons():
    assert apply_coupons(100.0, []) == 100.0


def test_single_coupon():
    assert apply_coupons(100.0, [10]) == 90.0


def test_stacked_coupons():
    # Spec: coupons apply sequentially to the running total.
    # 100 -> 90 (10%) -> 72 (20%)
    assert apply_coupons(100.0, [10, 20]) == 72.0


def test_format_price():
    assert format_price(1234.5) == "$1,234.50"
