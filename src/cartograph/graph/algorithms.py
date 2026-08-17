"""Graph algorithms over the call and import graphs.

Deliberately dependency-free (no networkx, no scipy): the graphs here are sparse
and the implementations are short, so vendoring a numeric stack would cost more
than it buys -- and it keeps `pip install cartograph-mcp` cheap.

Why PageRank instead of embeddings? "Which `run` did you mean?" is a *structural*
question, not a semantic one. The `run` that forty call sites depend on is the
one an agent wants, and the call graph already knows that. It is also stable,
explainable and free -- no model, no index build, no vector store.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

Edge = tuple[int, int, float]


def pagerank(
    edges: Sequence[Edge],
    nodes: Iterable[int],
    *,
    damping: float = 0.85,
    iterations: int = 30,
    tolerance: float = 1.0e-7,
) -> dict[int, float]:
    """Weighted PageRank on the call graph (rank flows caller -> callee).

    Dangling nodes (leaves) redistribute their mass uniformly, which is the
    standard fix and keeps the total mass at 1.0 so scores stay comparable
    between runs of different sizes.
    """
    node_list = list(dict.fromkeys(nodes))
    n = len(node_list)
    if n == 0:
        return {}

    out_edges: dict[int, list[tuple[int, float]]] = defaultdict(list)
    out_weight: dict[int, float] = defaultdict(float)
    known = set(node_list)
    for src, dst, weight in edges:
        if src == dst or src not in known or dst not in known:
            continue
        w = max(weight, 1.0e-6)
        out_edges[src].append((dst, w))
        out_weight[src] += w

    base = 1.0 / n
    rank = dict.fromkeys(node_list, base)

    for _ in range(iterations):
        nxt = dict.fromkeys(node_list, 0.0)
        dangling_mass = 0.0
        for node in node_list:
            score = rank[node]
            targets = out_edges.get(node)
            if not targets:
                dangling_mass += score
                continue
            total = out_weight[node]
            for dst, weight in targets:
                nxt[dst] += score * (weight / total)

        leak = (dangling_mass / n) if n else 0.0
        delta = 0.0
        for node in node_list:
            updated = (1.0 - damping) * base + damping * (nxt[node] + leak)
            delta += abs(updated - rank[node])
            nxt[node] = updated
        rank = nxt
        if delta < tolerance:
            break
    return rank


def personalized_pagerank(
    edges: Sequence[Edge],
    seeds: Sequence[int],
    *,
    damping: float = 0.7,
    iterations: int = 20,
    treat_as_undirected: bool = True,
) -> dict[int, float]:
    """Rank nodes by proximity to ``seeds`` -- "what else should I read?".

    Traversal is undirected by default: when you are about to change a function,
    both the things it calls and the things that call it are relevant context.
    """
    adjacency: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for src, dst, weight in edges:
        w = max(weight, 1.0e-6)
        adjacency[src].append((dst, w))
        if treat_as_undirected:
            adjacency[dst].append((src, w))

    if not seeds:
        return {}
    seed_mass = 1.0 / len(seeds)
    rank: dict[int, float] = dict.fromkeys(seeds, seed_mass)

    for _ in range(iterations):
        nxt: dict[int, float] = defaultdict(float)
        for node, score in rank.items():
            targets = adjacency.get(node)
            if not targets:
                continue
            total = sum(w for _, w in targets)
            for dst, weight in targets:
                nxt[dst] += damping * score * (weight / total)
        for seed in seeds:
            nxt[seed] += (1.0 - damping) * seed_mass
        rank = dict(nxt)

    for seed in seeds:
        rank.pop(seed, None)
    return rank


def strongly_connected_components(
    edges: Iterable[tuple[str, str]],
) -> list[list[str]]:
    """Tarjan's SCC, iterative so a deep import chain cannot blow the stack.

    Any component with more than one member is an import cycle -- the single
    most actionable architectural smell you can extract from a repo statically.
    """
    graph: dict[str, list[str]] = defaultdict(list)
    nodes: list[str] = []
    seen: set[str] = set()
    for src, dst in edges:
        graph[src].append(dst)
        for node in (src, dst):
            if node not in seen:
                seen.add(node)
                nodes.append(node)

    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root in nodes:
        if root in index_of:
            continue
        # (node, iterator position) frames, emulating recursion.
        work: list[tuple[str, int]] = [(root, 0)]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, child_idx = work[-1]
            children = graph.get(node, ())
            if child_idx < len(children):
                work[-1] = (node, child_idx + 1)
                child = children[child_idx]
                if child not in index_of:
                    index_of[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, 0))
                elif child in on_stack:
                    low[node] = min(low[node], index_of[child])
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))

    return sorted((c for c in components if len(c) > 1), key=lambda c: (-len(c), c[0]))


def longest_path_layers(edges: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Assign each module a layer via longest-path from any source node.

    Gives an at-a-glance sense of architectural depth; nodes inside a cycle all
    collapse to the same layer, which is honest -- they have no ordering.
    """
    graph: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = defaultdict(int)
    nodes: set[str] = set()
    for src, dst in edges:
        graph[src].append(dst)
        indegree[dst] += 1
        nodes.update((src, dst))

    layer = dict.fromkeys(nodes, 0)
    queue = [n for n in sorted(nodes) if indegree[n] == 0]
    remaining = dict(indegree)
    while queue:
        node = queue.pop(0)
        for child in graph.get(node, ()):
            layer[child] = max(layer[child], layer[node] + 1)
            remaining[child] -= 1
            if remaining[child] == 0:
                queue.append(child)
    return layer
