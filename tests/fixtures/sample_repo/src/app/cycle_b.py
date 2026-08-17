"""Other half of the deliberate import cycle."""

from app import cycle_a


def b_side() -> str:
    return "b"


def calls_back() -> str:
    return cycle_a.a_side()
