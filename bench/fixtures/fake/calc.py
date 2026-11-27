"""Fake task repository (bench smoke test): `total` has an off-by-one bug."""


def total(n: int) -> int:
    """Sum of the integers 1..n inclusive."""
    return sum(range(1, n))
