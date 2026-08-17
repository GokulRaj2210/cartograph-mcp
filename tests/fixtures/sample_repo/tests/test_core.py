"""Test file: exercises the is_test heuristic and the test-detection in blast radius."""

from app.core import build_engine


def test_engine_runs():
    engine = build_engine("t")
    assert engine.run("  hello  world ") == "hello world"
