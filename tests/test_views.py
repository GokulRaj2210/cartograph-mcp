"""Rendering: compact, and honest about what it left out.

A silently truncated list is the failure mode that matters here. An agent handed
20 of 87 callers with no marker will confidently conclude the other 67 do not
exist, and then delete something.
"""

from __future__ import annotations

from cartograph import views
from cartograph.models import Neighbour, Symbol
from cartograph.service import Cartograph
from cartograph.views import CHARS_PER_TOKEN, TokenBudget


def make_symbol(index: int, name: str = "sym") -> Symbol:
    return Symbol(
        id=index,
        name=f"{name}{index}",
        qualname=f"mod.sub:{name}{index}",
        kind="function",
        lang="python",
        path=f"src/mod/sub{index}.py",
        start_line=index,
        end_line=index + 5,
        signature=f"def {name}{index}(a, b) -> int",
        docstring=f"Docstring for {name}{index}.",
        rank=1.0 / (index + 1),
    )


# ---------------------------------------------------------------------------
# TokenBudget
# ---------------------------------------------------------------------------


def test_budget_accepts_lines_until_full() -> None:
    budget = TokenBudget(max_tokens=10)  # 40 chars
    assert budget.add("a" * 20) is True
    assert budget.add("b" * 20) is False
    assert budget.dropped == 1


def test_budget_announces_truncation() -> None:
    budget = TokenBudget(max_tokens=5)
    budget.add("x" * 30)
    budget.add("y" * 30)
    out = budget.render()
    assert "truncated" in out
    assert "omitted" in out


def test_budget_is_silent_when_nothing_dropped() -> None:
    budget = TokenBudget(max_tokens=100)
    budget.add("short")
    assert "truncated" not in budget.render()


def test_budget_add_all_stops_at_the_limit() -> None:
    # 30 tokens = 120 chars; each line is ~28, so a handful fit and the rest don't.
    budget = TokenBudget(max_tokens=30)
    added = budget.add_all(f"line {i} " + "z" * 20 for i in range(50))
    assert 0 < added < 50
    assert budget.dropped >= 1


def test_budget_respects_its_char_limit() -> None:
    budget = TokenBudget(max_tokens=25)
    budget.add_all("x" * 40 for _ in range(100))
    assert len(budget.render()) <= 25 * CHARS_PER_TOKEN + 200  # + the notice


# ---------------------------------------------------------------------------
# symbol views
# ---------------------------------------------------------------------------


def test_symbols_table_is_compact() -> None:
    out = views.symbols_table([make_symbol(i) for i in range(5)])
    assert "5 matches" in out
    assert "mod.sub:sym0" in out
    # Markdown, not JSON: no structural punctuation tax.
    assert '{"' not in out


def test_symbols_table_empty_suggests_a_next_step() -> None:
    out = views.symbols_table([])
    assert "search_code" in out


def test_symbols_table_reports_the_total_when_capped() -> None:
    out = views.symbols_table([make_symbol(i) for i in range(3)], total=90)
    assert "of 90" in out


def test_large_result_set_is_truncated_with_notice() -> None:
    out = views.symbols_table([make_symbol(i) for i in range(400)], budget=300)
    assert "truncated" in out


def test_neighbour_tree_indents_by_depth() -> None:
    root = make_symbol(0, "root")
    neighbours = [
        Neighbour(symbol=make_symbol(1), depth=1, confidence=0.95, via_line=3, reason="same-file"),
        Neighbour(symbol=make_symbol(2), depth=2, confidence=0.9, via_line=4, reason="import"),
    ]
    out = views.neighbour_tree(root, neighbours, direction="up")
    lines = [line for line in out.splitlines() if line.strip().startswith("- `mod.sub")]
    assert lines[0].startswith("- ")
    assert lines[1].startswith("  - ")


def test_neighbour_tree_flags_low_confidence() -> None:
    root = make_symbol(0, "root")
    weak = [
        Neighbour(symbol=make_symbol(9), depth=1, confidence=0.45, via_line=1, reason="name-only")
    ]
    out = views.neighbour_tree(root, weak, direction="up")
    assert "name-only" in out
    assert "0.45" in out
    assert "verify" in out.lower(), "weak edges must carry a warning"


def test_neighbour_tree_empty_explains_why() -> None:
    out = views.neighbour_tree(make_symbol(0), [], direction="up")
    assert "min_confidence" in out
    assert "entry point" in out


def test_confidence_flag_marks_uncertainty() -> None:
    root = make_symbol(0)
    items = [
        Neighbour(symbol=make_symbol(1), depth=1, confidence=0.95, via_line=1, reason="same-file"),
        Neighbour(
            symbol=make_symbol(2), depth=1, confidence=0.6, via_line=1, reason="unique-global"
        ),
        Neighbour(symbol=make_symbol(3), depth=1, confidence=0.3, via_line=1, reason="ambiguous"),
    ]
    out = views.neighbour_tree(root, items, direction="up")
    assert "~0.60" in out
    assert "?0.30" in out


# ---------------------------------------------------------------------------
# rendered against the real fixture graph
# ---------------------------------------------------------------------------


def test_architecture_view_includes_a_mermaid_diagram(graph: Cartograph) -> None:
    out = views.architecture_md(graph.architecture(), budget=4000)
    assert "```mermaid" in out
    assert "flowchart LR" in out
    assert "import cycle" in out  # the fixture has a planted cycle


def test_architecture_view_can_omit_the_diagram(graph: Cartograph) -> None:
    out = views.architecture_md(graph.architecture(), mermaid=False, budget=4000)
    assert "```mermaid" not in out


def test_mermaid_highlights_cycle_members() -> None:
    edges = [("a", "b", 3), ("b", "a", 1)]
    out = views.mermaid_modules(edges, cycles=[["a", "b"]])
    assert "classDef cyc" in out
    assert "class m0 cyc;" in out or "class m1 cyc;" in out


def test_mermaid_caps_the_node_count() -> None:
    edges = [(f"m{i}", f"m{i + 1}", 1) for i in range(80)]
    out = views.mermaid_modules(edges, limit=10)
    assert out.count("-->") <= 10
    assert "showing 10 of 80" in out


def test_blast_radius_view_leads_with_tests(graph: Cartograph) -> None:
    out = views.blast_radius_md(graph.blast_radius("src/app/core.py"))
    assert "Tests to run first" in out
    assert "tests/test_core.py" in out
    assert out.index("Tests to run first") < out.index("Dependent files")


def test_blast_radius_view_states_its_bias(graph: Cartograph) -> None:
    out = views.blast_radius_md(graph.blast_radius("src/app/core.py"))
    assert "Recall-first" in out


def test_file_summary_view(graph: Cartograph) -> None:
    out = views.file_summary_md(graph.file_summary("src/app/core.py"))
    assert "`src/app/core.py`" in out
    assert "Engine" in out
    assert "Imported by" in out


def test_stats_view_separates_internal_from_external(graph: Cartograph) -> None:
    out = views.stats_md(graph.stats())
    assert "in-repo call sites" in out
    assert "third-party" in out
    assert "Edge resolution by rule" in out


def test_symbol_detail_view_with_source(graph: Cartograph) -> None:
    detail = graph.symbol_detail("app.core:Engine.run", include_source=True)
    out = views.symbol_detail_md(detail, budget=4000)
    assert "```python" in out
    assert "def run" in out
    assert "Called by" in out or "Calls" in out


def test_every_view_returns_non_empty_markdown(graph: Cartograph) -> None:
    """Cheap guard: a view that silently renders nothing is worse than an error."""
    assert views.symbols_table(graph.find_symbol("normalize")).strip()
    assert views.stats_md(graph.stats()).strip()
    assert views.architecture_md(graph.architecture()).strip()
    assert views.file_summary_md(graph.file_summary("src/app/core.py")).strip()
    assert views.blast_radius_md(graph.blast_radius("src/app/core.py")).strip()
