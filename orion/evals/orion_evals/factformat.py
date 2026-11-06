"""The contract's one fact format (`format_fact`), reimplemented so the harness reads chips as written.

Contract "Fact values on screen": a string stays as it is; unit `p` prints `0` for zero, one
decimal of e-notation below 0.001 and three significant digits otherwise; unit `log2FC` two
decimals; any other number thousands separators and at most two decimals, trailing zeros dropped.
Rounding is half away from zero on the number's shortest decimal form.
`tests/test_canonical.py` reproduces `fixtures/fact-format-vectors.json`.
"""

import math
from decimal import ROUND_HALF_UP, Decimal


def _decimal(value: int | float) -> Decimal:
    return Decimal(value) if isinstance(value, int) else Decimal(repr(float(value)))


def _plain(number: Decimal) -> str:
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def format_fact(value: int | float | str, unit: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{value!r} is not a finite number")
    number = _decimal(value)
    if unit == "p":
        if number == 0:
            return "0"
        if abs(number) < Decimal("0.001"):
            exponent = number.adjusted()
            mantissa = number.scaleb(-exponent).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            if abs(mantissa) >= 10:
                mantissa = (mantissa / 10).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                exponent += 1
            return f"{mantissa}e{exponent}"
        step = Decimal(1).scaleb(number.adjusted() - 2)
        return _plain(number.quantize(step, rounding=ROUND_HALF_UP))
    if unit == "log2FC":
        text = format(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
        return "0.00" if text == "-0.00" else text
    rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    whole, _, fraction = _plain(abs(rounded)).partition(".")
    return sign + f"{int(whole):,}" + (f".{fraction}" if fraction else "")
