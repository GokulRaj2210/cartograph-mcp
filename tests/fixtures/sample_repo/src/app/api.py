"""Top layer: imports core, called by nothing in-repo (an entry point)."""

from app.core import Engine, build_engine


def handle(label: str, payload: str) -> str:
    """Two hops from `normalize`: handle -> Engine.run -> truncate/normalize."""
    engine = build_engine(label)
    return engine.run(payload)


def handle_direct(payload: str) -> str:
    """Qualified call on a receiver whose type we *can* name."""
    engine = Engine("direct")
    return engine.run(payload)
