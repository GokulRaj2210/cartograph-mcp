"""Core engine. Exercises: methods, inheritance, same-file and import edges."""

from app.helpers import normalize, truncate


class BaseEngine:
    """Base class, so `inherits` edges have something to point at."""

    def describe(self) -> str:
        return "base"


class Engine(BaseEngine):
    """Inherits from BaseEngine and calls across module boundaries."""

    def __init__(self, label: str) -> None:
        self.label = normalize(label)

    def run(self, payload: str) -> str:
        """Calls a same-file helper and an imported one."""
        cleaned = self._prepare(payload)
        return truncate(cleaned)

    def _prepare(self, payload: str) -> str:
        """Same-file callee of `run`."""
        return normalize(payload)

    def describe(self) -> str:
        return f"engine:{self.label}"


def build_engine(label: str) -> Engine:
    """Instantiates Engine -- an `instantiates` edge."""
    return Engine(label)
