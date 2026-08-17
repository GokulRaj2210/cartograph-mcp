"""Traversal and impact analysis over the fixture repo's known structure.

The fixture's Python call chain is:

    api.handle -> core.build_engine -> Engine (instantiate)
    api.handle -> Engine.run -> Engine._prepare -> helpers.normalize
                            \\-> helpers.truncate -> helpers.normalize
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cartograph.service import BROAD, PRECISE, Cartograph


def names(items: object) -> set[str]:
    return {n.symbol.qualname for n in items}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------


def test_find_symbol_exact(graph: Cartograph) -> None:
    found = graph.find_symbol("normalize")
    assert any(s.qualname == "app.helpers:normalize" for s in found)


def test_find_symbol_filters_by_kind(graph: Cartograph) -> None:
    found = graph.find_symbol("Engine", kind="class")
    # Partial matching is intentional: `BaseEngine` is a legitimate hit here.
    assert {s.qualname for s in found} == {"app.core:Engine", "app.core:BaseEngine"}
    assert all(s.kind == "class" for s in found)
    # The exact match must still lead.
    assert found[0].qualname == "app.core:Engine"


def test_find_symbol_exact_flag_excludes_partial_matches(graph: Cartograph) -> None:
    found = graph.store.find_symbols("Engine", exact=True)
    assert {s.qualname for s in found} == {"app.core:Engine"}


def test_find_symbol_filters_by_language(graph: Cartograph) -> None:
    found = graph.find_symbol("send", lang="typescript")
    assert found
    assert all(s.lang == "typescript" for s in found)


def test_find_symbol_ranks_by_importance(graph: Cartograph) -> None:
    """`normalize` is called from several places; it should lead its matches."""
    found = graph.find_symbol("normalize")
    assert found[0].rank >= found[-1].rank


def test_selector_accepts_multiple_spellings(graph: Cartograph) -> None:
    target = graph.find_symbol("Engine", kind="class")[0]
    for selector in (
        str(target.id),
        "app.core:Engine",
        "Engine",
        "src/app/core.py:Engine",
    ):
        assert graph.candidates(selector), f"selector {selector!r} resolved to nothing"


def test_search_matches_docstring_text(graph: Cartograph) -> None:
    hits = graph.search("Collapse whitespace")
    assert any(s.name == "normalize" for s in hits)


def test_search_survives_fts_syntax_characters(graph: Cartograph) -> None:
    """Agents paste odd strings; FTS5 must not raise on them."""
    for query in ("normalize()", 'a "quoted" thing', "AND OR NOT", "app.core:Engine", "*"):
        graph.search(query)  # must not raise


def test_missing_symbol_raises_lookup_error(graph: Cartograph) -> None:
    with pytest.raises(LookupError):
        graph.who_calls("definitely_not_a_symbol_xyz")


# ---------------------------------------------------------------------------
# call graph
# ---------------------------------------------------------------------------


def test_direct_callers(graph: Cartograph) -> None:
    _, callers = graph.who_calls("app.helpers:truncate", depth=1)
    assert "app.core:Engine.run" in names(callers)


def test_transitive_callers_report_increasing_depth(graph: Cartograph) -> None:
    _, callers = graph.who_calls("app.helpers:normalize", depth=3)
    by_name = {n.symbol.qualname: n.depth for n in callers}
    assert by_name["app.core:Engine._prepare"] == 1
    assert by_name["app.core:Engine.run"] == 2
    # handle reaches normalize only through run.
    assert by_name.get("app.api:handle", 99) >= 3


def test_depth_limit_is_respected(graph: Cartograph) -> None:
    _, shallow = graph.who_calls("app.helpers:normalize", depth=1)
    assert all(n.depth == 1 for n in shallow)


def test_callees(graph: Cartograph) -> None:
    _, callees = graph.calls("app.core:Engine.run", depth=1)
    reached = names(callees)
    assert "app.core:Engine._prepare" in reached
    assert "app.helpers:truncate" in reached


def test_confidence_filter_excludes_weak_edges(graph: Cartograph) -> None:
    _, precise = graph.who_calls("app.helpers:normalize", depth=3, min_confidence=PRECISE)
    _, broad = graph.who_calls("app.helpers:normalize", depth=3, min_confidence=BROAD)
    assert len(broad) >= len(precise)
    assert all(n.confidence >= PRECISE for n in precise)


def test_traversal_terminates_on_a_cycle(graph: Cartograph) -> None:
    """cycle_a <-> cycle_b would loop forever without the depth cap."""
    _, callers = graph.who_calls("app.cycle_a:a_side", depth=6)
    assert isinstance(callers, list)


def test_limit_caps_results(graph: Cartograph) -> None:
    _, callers = graph.who_calls("app.helpers:normalize", depth=4, limit=1)
    assert len(callers) == 1


def test_related_symbols_excludes_the_anchor(graph: Cartograph) -> None:
    root, related = graph.related("app.helpers:normalize")
    assert root.qualname == "app.helpers:normalize"
    assert root.qualname not in {s.qualname for s in related}


def test_related_symbols_surface_collaborators(graph: Cartograph) -> None:
    _, related = graph.related("app.helpers:normalize", limit=10)
    assert {s.qualname for s in related} & {
        "app.core:Engine.run",
        "app.core:Engine._prepare",
        "app.helpers:truncate",
    }


# ---------------------------------------------------------------------------
# blast radius
# ---------------------------------------------------------------------------


def test_blast_radius_of_a_file_lists_importers(graph: Cartograph) -> None:
    radius = graph.blast_radius("src/app/helpers.py")
    assert radius.kind == "file"
    paths = {p for p, _, _ in radius.files}
    assert "src/app/core.py" in paths


def test_blast_radius_finds_the_test_file(graph: Cartograph) -> None:
    """The whole point: tell me which tests to run."""
    radius = graph.blast_radius("src/app/core.py")
    assert "tests/test_core.py" in radius.tests


def test_blast_radius_excludes_same_file_symbols(graph: Cartograph) -> None:
    radius = graph.blast_radius("src/app/helpers.py")
    assert all(n.symbol.path != "src/app/helpers.py" for n in radius.symbols), (
        "same-file callers are not impact; they move with the change"
    )


def test_blast_radius_of_a_symbol(graph: Cartograph) -> None:
    radius = graph.blast_radius("app.helpers:normalize")
    assert radius.kind == "symbol"
    assert "app.core:Engine._prepare" in names(radius.symbols)


def test_blast_radius_of_a_leaf_is_empty(graph: Cartograph) -> None:
    radius = graph.blast_radius("app.helpers:_internal_only")
    assert radius.symbols == []


def test_blast_radius_partial_path_match(graph: Cartograph) -> None:
    radius = graph.blast_radius("helpers.py")
    assert radius.target == "src/app/helpers.py"


# ---------------------------------------------------------------------------
# overviews
# ---------------------------------------------------------------------------


def test_architecture_detects_the_planted_cycle(graph: Cartograph) -> None:
    arch = graph.architecture()
    assert ["app.cycle_a", "app.cycle_b"] in arch.cycles


def test_architecture_layers_put_leaves_deepest(graph: Cartograph) -> None:
    arch = graph.architecture()
    # helpers is imported by core, which is imported by api.
    assert arch.layers["app.helpers"] > arch.layers["app.core"]


def test_entry_points_include_the_top_layer(graph: Cartograph) -> None:
    arch = graph.architecture()
    assert "app.api:handle" in {s.qualname for s in arch.entry_points}


def test_hotspots_are_rank_ordered(graph: Cartograph) -> None:
    arch = graph.architecture()
    ranks = [sym.rank for sym, _ in arch.hotspots]
    assert ranks == sorted(ranks, reverse=True)


def test_file_summary(graph: Cartograph) -> None:
    summary = graph.file_summary("src/app/core.py")
    assert summary.lang == "python"
    assert {s.name for s in summary.symbols} >= {"Engine", "run", "build_engine"}
    assert any(target == "src/app/helpers.py" for _, target, ext in summary.imports if not ext)
    assert "src/app/api.py" in summary.imported_by


def test_file_summary_marks_external_imports(graph: Cartograph) -> None:
    summary = graph.file_summary("svc/store.go")
    assert any(module == "strings" and ext for module, _, ext in summary.imports)


def test_file_summary_unknown_path_raises(graph: Cartograph) -> None:
    with pytest.raises(LookupError):
        graph.file_summary("no/such/file.py")


def test_stats_are_internally_consistent(graph: Cartograph) -> None:
    stats = graph.stats()
    assert stats["symbols"] > 0
    assert stats["resolved_edges"] <= stats["edges"]
    assert 0.0 <= float(stats["internal_resolution_rate"]) <= 1.0  # type: ignore[arg-type]
    assert set(stats["by_lang"]) == {"python", "typescript", "go"}  # type: ignore[arg-type]


def test_symbol_source_returns_the_definition(graph: Cartograph) -> None:
    detail = graph.symbol_detail("app.helpers:normalize", include_source=True)
    assert detail.source is not None
    assert "def normalize" in detail.source


def test_index_not_found_is_explicit(tmp_path: Path) -> None:
    from cartograph.service import IndexNotFoundError

    with pytest.raises(IndexNotFoundError):
        Cartograph.load(tmp_path / "nope.db")
