"""IBKR Pro commission estimates shared by the simulator, the IBKR broker and risk checks.

Estimates are deliberately a little conservative (they include typical exchange and
clearing fees for the Tiered plan) because the risk manager uses them to refuse trades
whose fees would eat the expected profit. Check IBKR's commission pages for current
rates; these constants are the single place to update them.
"""

from __future__ import annotations

from typing import Literal

from trader.models import AssetClass, Instrument, Side

Plan = Literal["tiered", "fixed"]

# US stocks, IBKR Pro Tiered (<= 300k shares/month).
TIERED_PER_SHARE = 0.0035
TIERED_MIN = 0.35
TIERED_EXCHANGE_CLEARING_PER_SHARE = 0.0032  # typical exchange (remove liquidity) + clearing
# US stocks, IBKR Pro Fixed (exchange and regulatory fees included except on sells).
FIXED_PER_SHARE = 0.005
FIXED_MIN = 1.00
# Both plans cap the commission at 1% of trade value.
MAX_PCT_OF_VALUE = 0.01
# Regulatory fees on sales (SEC Section 31 fee and FINRA TAF), approximate.
SEC_FEE_RATE = 0.0000278
FINRA_TAF_PER_SHARE = 0.000166
FINRA_TAF_MAX = 8.30

# Crypto: 0.12%-0.18% of trade value by monthly volume, min $1.75, capped at 1% of value.
CRYPTO_RATE = 0.0018
CRYPTO_MIN = 1.75


def stock_commission(qty: float, price: float, side: Side, plan: Plan = "tiered") -> float:
    qty = abs(qty)
    value = qty * price
    if qty <= 0 or value <= 0:
        return 0.0
    if plan == "fixed":
        commission = min(max(FIXED_MIN, FIXED_PER_SHARE * qty), MAX_PCT_OF_VALUE * value)
    else:
        commission = min(max(TIERED_MIN, TIERED_PER_SHARE * qty), MAX_PCT_OF_VALUE * value)
        commission += TIERED_EXCHANGE_CLEARING_PER_SHARE * qty
    if side == Side.SELL:
        commission += SEC_FEE_RATE * value + min(FINRA_TAF_PER_SHARE * qty, FINRA_TAF_MAX)
    return round(commission, 4)


def crypto_commission(qty: float, price: float) -> float:
    value = abs(qty) * price
    if value <= 0:
        return 0.0
    return round(min(max(CRYPTO_MIN, CRYPTO_RATE * value), MAX_PCT_OF_VALUE * value), 4)


def estimate_commission(
    instrument: Instrument, qty: float, price: float, side: Side = Side.BUY, plan: Plan = "tiered"
) -> float:
    """One-way commission estimate in USD."""
    if instrument.asset_class == AssetClass.CRYPTO:
        return crypto_commission(qty, price)
    return stock_commission(qty, price, side, plan)
