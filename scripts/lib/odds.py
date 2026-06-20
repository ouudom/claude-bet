"""odds.py — American-odds math shared by settle.py, calibration.py, edge.py.

Kept dependency-free and pure so every probability in the engine is traceable.
Vig-free (devigged) probabilities are the engine's currency: CLV and edge are both
measured in vig-free prob, never in raw posted prices.
"""


def american_to_decimal(price):
    """American odds -> decimal payout multiplier (e.g. -110 -> 1.909, +120 -> 2.2)."""
    price = int(price)
    return 1.0 + (price / 100.0 if price > 0 else 100.0 / -price)


def american_to_implied(price):
    """American odds -> raw implied probability (includes the book's vig)."""
    price = int(price)
    return (100.0 / (price + 100.0)) if price > 0 else (-price / (-price + 100.0))


def devig_two_way(price_a, price_b):
    """Remove vig from a 2-way market. Returns (p_a, p_b) summing to 1.0.

    Proportional (multiplicative) devig — the standard sharp-line normalization.
    """
    ra = american_to_implied(price_a)
    rb = american_to_implied(price_b)
    total = ra + rb
    return ra / total, rb / total


def payout_units(result, price, stake_units=1.0):
    """Profit/loss in units for a settled bet at American `price`."""
    if result == "win":
        return stake_units * (american_to_decimal(price) - 1.0)
    if result == "loss":
        return -stake_units
    return 0.0  # push
