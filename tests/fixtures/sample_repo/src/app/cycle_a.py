"""Half of a deliberate import cycle, for the Tarjan SCC test."""

from app import cycle_b


def a_side() -> str:
    return cycle_b.b_side()
