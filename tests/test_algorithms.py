"""Graph algorithms, tested against hand-computable graphs."""

from __future__ import annotations

import pytest

from cartograph.graph.algorithms import (
    longest_path_layers,
    pagerank,
    personalized_pagerank,
    strongly_connected_components,
)

# ---------------------------------------------------------------------------
# PageRank
# ---------------------------------------------------------------------------


def test_pagerank_mass_is_conserved() -> None:
    edges = [(1, 2, 1.0), (2, 3, 1.0), (3, 1, 1.0)]
    ranks = pagerank(edges, [1, 2, 3])
    assert pytest.approx(sum(ranks.values()), abs=1e-6) == 1.0


def test_pagerank_symmetric_cycle_is_uniform() -> None:
    edges = [(1, 2, 1.0), (2, 3, 1.0), (3, 1, 1.0)]
    ranks = pagerank(edges, [1, 2, 3])
    assert pytest.approx(ranks[1], abs=1e-6) == ranks[2] == pytest.approx(ranks[3], abs=1e-6)


def test_pagerank_favours_the_most_called_symbol() -> None:
    """Everything calls 99; it must outrank its callers."""
    edges = [(i, 99, 1.0) for i in range(1, 10)]
    ranks = pagerank(edges, [*range(1, 10), 99])
    assert max(ranks, key=lambda k: ranks[k]) == 99


def test_pagerank_dangling_nodes_do_not_leak_mass() -> None:
    # 2 is a leaf: without dangling redistribution, total mass would decay.
    edges = [(1, 2, 1.0)]
    ranks = pagerank(edges, [1, 2, 3])
    assert pytest.approx(sum(ranks.values()), abs=1e-6) == 1.0
    assert ranks[2] > ranks[1]


def test_pagerank_weights_matter() -> None:
    """A high-confidence edge should transfer more rank than a weak one."""
    ranks = pagerank([(1, 2, 0.95), (1, 3, 0.05)], [1, 2, 3])
    assert ranks[2] > ranks[3]


def test_pagerank_ignores_self_loops_and_unknown_nodes() -> None:
    ranks = pagerank([(1, 1, 1.0), (1, 42, 1.0), (1, 2, 1.0)], [1, 2])
    assert set(ranks) == {1, 2}
    assert pytest.approx(sum(ranks.values()), abs=1e-6) == 1.0


def test_pagerank_empty_graph() -> None:
    assert pagerank([], []) == {}
    ranks = pagerank([], [7])
    assert pytest.approx(ranks[7]) == 1.0


# ---------------------------------------------------------------------------
# personalized PageRank
# ---------------------------------------------------------------------------


def test_personalized_pagerank_prefers_near_neighbours() -> None:
    # 1 -> 2 -> 3 -> 4, seeded at 1.
    edges = [(1, 2, 1.0), (2, 3, 1.0), (3, 4, 1.0)]
    scores = personalized_pagerank(edges, [1])
    assert scores[2] > scores[3] > scores[4]


def test_personalized_pagerank_excludes_the_seed() -> None:
    scores = personalized_pagerank([(1, 2, 1.0)], [1])
    assert 1 not in scores


def test_personalized_pagerank_is_undirected_by_default() -> None:
    """Callers are as relevant as callees when deciding what to read."""
    scores = personalized_pagerank([(2, 1, 1.0)], [1])
    assert scores.get(2, 0.0) > 0.0


def test_personalized_pagerank_no_seeds() -> None:
    assert personalized_pagerank([(1, 2, 1.0)], []) == {}


# ---------------------------------------------------------------------------
# SCC / cycles
# ---------------------------------------------------------------------------


def test_scc_finds_a_two_node_cycle() -> None:
    assert strongly_connected_components([("a", "b"), ("b", "a")]) == [["a", "b"]]


def test_scc_ignores_a_dag() -> None:
    assert strongly_connected_components([("a", "b"), ("b", "c"), ("a", "c")]) == []


def test_scc_finds_multiple_components_largest_first() -> None:
    edges = [
        ("a", "b"),
        ("b", "c"),
        ("c", "a"),  # 3-cycle
        ("x", "y"),
        ("y", "x"),  # 2-cycle
        ("p", "q"),  # acyclic tail
    ]
    assert strongly_connected_components(edges) == [["a", "b", "c"], ["x", "y"]]


def test_scc_ignores_self_loops() -> None:
    """A module importing itself is not an architectural cycle worth reporting."""
    assert strongly_connected_components([("a", "a")]) == []


def test_scc_handles_deep_chains_without_recursion_error() -> None:
    """Tarjan is iterative precisely so a 5k-deep import chain cannot blow up."""
    depth = 5000
    edges = [(f"n{i}", f"n{i + 1}") for i in range(depth)]
    edges.append((f"n{depth}", "n0"))  # close it into one giant SCC
    components = strongly_connected_components(edges)
    assert len(components) == 1
    assert len(components[0]) == depth + 1


def test_scc_is_deterministic() -> None:
    edges = [("b", "a"), ("a", "b"), ("c", "d"), ("d", "c")]
    assert strongly_connected_components(edges) == strongly_connected_components(edges)


# ---------------------------------------------------------------------------
# layering
# ---------------------------------------------------------------------------


def test_layers_follow_the_longest_path() -> None:
    layers = longest_path_layers([("a", "b"), ("b", "c"), ("a", "c")])
    assert layers["a"] == 0
    assert layers["b"] == 1
    assert layers["c"] == 2  # longest path wins over the direct a->c edge


def test_layers_terminate_on_a_cycle() -> None:
    layers = longest_path_layers([("a", "b"), ("b", "a")])
    # Nodes inside a cycle have no valid ordering; they simply stay at 0.
    assert layers == {"a": 0, "b": 0}
