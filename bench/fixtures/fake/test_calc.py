"""Tests of the fake task (`python test_calc.py`; exit 0 iff the bug is fixed)."""

from calc import total  # type: ignore[import-not-found]  # run from the fake repo


def test_total() -> None:
    assert total(1) == 1
    assert total(3) == 6


if __name__ == "__main__":
    test_total()
