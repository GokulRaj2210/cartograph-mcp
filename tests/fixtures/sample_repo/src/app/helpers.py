"""Leaf helpers. Nothing here imports app code, so this is the bottom layer."""

MAX_WIDTH = 80


def normalize(text: str) -> str:
    """Collapse whitespace and trim."""
    return " ".join(text.split())


def truncate(text: str, limit: int = MAX_WIDTH) -> str:
    """Shorten text to ``limit`` characters."""
    cleaned = normalize(text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _internal_only() -> None:
    """Not exported: leading underscore."""
