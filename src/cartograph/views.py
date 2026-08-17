"""Rendering for agent consumption.

Most code-intelligence tools return JSON blobs. That is the wrong output format
when the caller is a language model: a 40-symbol JSON array costs several
thousand tokens of braces and repeated keys, and the model then has to reformat
it anyway.

So every view here is compact Markdown with a hard token budget, and every
truncation is *announced* -- an agent that knows it saw 20 of 87 results will
ask for more, whereas an agent handed a silently truncated list will confidently
conclude the other 67 do not exist.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from cartograph.models import Neighbour, Symbol
from cartograph.service import Architecture, BlastRadius, FileSummary, SymbolDetail

#: Rough but stable: ~4 characters per token for source-like English.
CHARS_PER_TOKEN = 4

DEFAULT_BUDGET = 1200


class TokenBudget:
    """Append-only line buffer that stops before it blows a token budget."""

    def __init__(self, max_tokens: int = DEFAULT_BUDGET) -> None:
        self.max_chars = max_tokens * CHARS_PER_TOKEN
        self.used = 0
        self.lines: list[str] = []
        self.dropped = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_chars

    def add(self, line: str = "") -> bool:
        cost = len(line) + 1
        if self.used + cost > self.max_chars:
            self.dropped += 1
            return False
        self.lines.append(line)
        self.used += cost
        return True

    def add_all(self, lines: Iterable[str]) -> int:
        added = 0
        for line in lines:
            if not self.add(line):
                break
            added += 1
        return added

    def render(self) -> str:
        out = "\n".join(self.lines).rstrip()
        if self.dropped:
            out += (
                f"\n\n_(output truncated at ~{self.max_chars // CHARS_PER_TOKEN} tokens; "
                f"{self.dropped}+ lines omitted -- narrow the query or raise `limit`)_"
            )
        return out


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------


def symbols_table(
    symbols: Sequence[Symbol], *, total: int | None = None, budget: int = DEFAULT_BUDGET
) -> str:
    buf = TokenBudget(budget)
    if not symbols:
        return "No matching symbols. Try `search_code` for a full-text search."
    buf.add(
        f"**{len(symbols)} match{'es' if len(symbols) != 1 else ''}**"
        + (f" of {total}" if total and total > len(symbols) else "")
    )
    buf.add()
    for sym in symbols:
        buf.add(f"- `{sym.qualname}` · {sym.kind} · {sym.location}")
        if sym.signature:
            buf.add(f"  `{_trim(sym.signature, 110)}`")
        if sym.docstring:
            buf.add(f"  {_first_line(sym.docstring, 110)}")
    return buf.render()


def symbol_detail_md(detail: SymbolDetail, *, budget: int = 1600) -> str:
    buf = TokenBudget(budget)
    sym = detail.symbol
    buf.add(f"## `{sym.qualname}`")
    buf.add(f"{sym.kind} · {sym.location}–{sym.end_line} · {sym.lang} · rank {sym.rank:.4f}")
    if sym.signature:
        buf.add()
        buf.add(f"```\n{_trim(sym.signature, 300)}\n```")
    if sym.docstring:
        buf.add()
        buf.add(_trim(sym.docstring, 400))
    if detail.members:
        buf.add()
        buf.add("**Members**")
        buf.add_all(f"- `{m.name}` ({m.kind}) L{m.start_line}" for m in detail.members[:20])
    if detail.callers:
        buf.add()
        buf.add(f"**Called by** ({len(detail.callers)})")
        buf.add_all(_neighbour_lines(detail.callers[:15]))
    if detail.callees:
        buf.add()
        buf.add(f"**Calls** ({len(detail.callees)})")
        buf.add_all(_neighbour_lines(detail.callees[:15]))
    if detail.source:
        buf.add()
        buf.add(f"```{_fence_lang(sym.lang)}")
        buf.add(detail.source)
        buf.add("```")
    return buf.render()


def neighbour_tree(
    root: Symbol,
    neighbours: Sequence[Neighbour],
    *,
    direction: str,
    budget: int = DEFAULT_BUDGET,
) -> str:
    """Depth-indented call tree with confidence and provenance."""
    buf = TokenBudget(budget)
    arrow = "callers of" if direction == "up" else "callees of"
    buf.add(f"**{arrow} `{root.qualname}`** ({root.location})")
    if not neighbours:
        buf.add()
        buf.add(
            "_No edges at this confidence. Lower `min_confidence` (0.3 for recall) "
            "or widen `depth`; a symbol with no callers may also be an entry point._"
        )
        return buf.render()
    buf.add()
    for item in neighbours:
        indent = "  " * (item.depth - 1)
        flag = _confidence_flag(item.confidence)
        buf.add(
            f"{indent}- `{item.symbol.qualname}` · {item.symbol.location} "
            f"· d{item.depth} {flag}{item.confidence:.2f} ({item.reason})"
        )
    low = sum(1 for n in neighbours if n.confidence < 0.5)
    if low:
        buf.add()
        buf.add(f"_{low} edge(s) below 0.5 confidence — name-resolved, verify before acting._")
    return buf.render()


def _neighbour_lines(neighbours: Sequence[Neighbour]) -> list[str]:
    return [
        f"- `{n.symbol.qualname}` · {n.symbol.location} · {n.confidence:.2f} ({n.reason})"
        for n in neighbours
    ]


# ---------------------------------------------------------------------------
# blast radius
# ---------------------------------------------------------------------------


def blast_radius_md(br: BlastRadius, *, budget: int = 1600) -> str:
    buf = TokenBudget(budget)
    buf.add(f"## Blast radius — {br.kind} `{br.target}`")
    buf.add()
    buf.add(
        f"{len(br.files)} dependent file(s), {len(br.symbols)} affected symbol(s), "
        f"{len(br.tests)} test file(s)."
    )
    if br.tests:
        buf.add()
        buf.add("**Tests to run first**")
        buf.add_all(f"- `{path}`" for path in br.tests[:25])
    if br.files:
        buf.add()
        buf.add("**Dependent files** (by import distance)")
        buf.add_all(
            f"- `{path}` · d{depth}{' · test' if is_test else ''}"
            for path, depth, is_test in br.files[:30]
        )
    if br.symbols:
        buf.add()
        buf.add("**Affected symbols**")
        buf.add_all(_neighbour_lines(br.symbols[:25]))
    if not br.files and not br.symbols:
        buf.add()
        buf.add("_Nothing depends on this: it is a leaf, an entry point, or dead code._")
    buf.add()
    buf.add(
        "_Recall-first view (min_confidence 0.3): treat as a review checklist, "
        "not a proof of impact._"
    )
    return buf.render()


# ---------------------------------------------------------------------------
# architecture
# ---------------------------------------------------------------------------


def architecture_md(arch: Architecture, *, budget: int = 2000, mermaid: bool = True) -> str:
    buf = TokenBudget(budget)
    counts = arch.counts
    buf.add("## Architecture overview")
    buf.add()
    langs = ", ".join(f"{lang} {n}" for lang, n in arch.by_lang.items())
    buf.add(
        f"{counts['files']} files · {counts['lines']:,} lines · {counts['symbols']} symbols "
        f"· {counts['edges']} edges ({counts['resolved_edges']} resolved) · {langs}"
    )

    if arch.cycles:
        buf.add()
        buf.add(f"**⚠ {len(arch.cycles)} import cycle(s)**")
        for cycle in arch.cycles[:5]:
            buf.add(f"- {' → '.join(cycle[:6])}{' → …' if len(cycle) > 6 else ''}")
    else:
        buf.add()
        buf.add("**No import cycles.** The module graph is a DAG.")

    if arch.modules:
        buf.add()
        buf.add("**Largest modules**")
        buf.add_all(
            f"- `{name}` · {files} file(s), {syms} symbols, layer {arch.layers.get(name, 0)}"
            for name, files, syms in arch.modules[:12]
        )

    if arch.hotspots:
        buf.add()
        buf.add("**Hotspots** (highest PageRank — change these carefully)")
        buf.add_all(
            f"- `{sym.qualname}` · {sym.location} · fan-in {fan_in} · rank {sym.rank:.4f}"
            for sym, fan_in in arch.hotspots[:10]
        )

    if arch.entry_points:
        buf.add()
        buf.add("**Entry points** (exported, nothing in-repo calls them)")
        buf.add_all(f"- `{sym.qualname}` · {sym.location}" for sym in arch.entry_points[:8])

    if mermaid and arch.edges:
        buf.add()
        buf.add(mermaid_modules(arch.edges, arch.cycles))
    return buf.render()


def mermaid_modules(
    edges: Sequence[tuple[str, str, int]],
    cycles: Sequence[Sequence[str]] = (),
    *,
    limit: int = 25,
) -> str:
    """A Mermaid flowchart of the module graph, cycle members highlighted."""
    in_cycle = {name for cycle in cycles for name in cycle}
    top = sorted(edges, key=lambda e: -e[2])[:limit]
    ids: dict[str, str] = {}

    def node_id(name: str) -> str:
        if name not in ids:
            ids[name] = f"m{len(ids)}"
        return ids[name]

    lines = ["```mermaid", "flowchart LR"]
    for src, dst, weight in top:
        label = "-->" if weight == 1 else f"-- {weight} -->"
        lines.append(f'  {node_id(src)}["{_short(src)}"] {label} {node_id(dst)}["{_short(dst)}"]')
    for name, ident in ids.items():
        if name in in_cycle:
            lines.append(f"  class {ident} cyc;")
    if in_cycle:
        lines.append("  classDef cyc fill:#fde68a,stroke:#b45309,color:#1f2937;")
    if len(edges) > limit:
        lines.append(f"  %% showing {limit} of {len(edges)} module edges by weight")
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# files & stats
# ---------------------------------------------------------------------------


def file_summary_md(fs: FileSummary, *, budget: int = 1400) -> str:
    buf = TokenBudget(budget)
    buf.add(f"## `{fs.path}`")
    buf.add(
        f"{fs.lang} · {fs.lines} lines · {len(fs.symbols)} symbols"
        + (" · test file" if fs.is_test else "")
    )

    if fs.symbols:
        buf.add()
        buf.add("**Defines**")
        buf.add_all(
            f"- L{s.start_line}–{s.end_line} `{s.name}` ({s.kind})"
            + (f" — {_first_line(s.docstring, 80)}" if s.docstring else "")
            for s in fs.symbols[:40]
        )

    internal = [(m, t) for m, t, ext in fs.imports if not ext and t]
    external = sorted({m for m, _, ext in fs.imports if ext})
    if internal:
        buf.add()
        buf.add("**Imports (in-repo)**")
        buf.add_all(f"- `{target}`" for _, target in dict(internal).items())
    if external:
        buf.add()
        buf.add(f"**Imports (external)**: {', '.join(f'`{m}`' for m in external[:20])}")
    if fs.imported_by:
        buf.add()
        buf.add(f"**Imported by** ({len(fs.imported_by)})")
        buf.add_all(f"- `{path}`" for path in fs.imported_by[:20])
    return buf.render()


def stats_md(stats: dict[str, Any]) -> str:
    buf = TokenBudget(900)
    buf.add("## Index stats")
    buf.add()
    buf.add(f"- root: `{stats.get('root')}`")
    buf.add(f"- indexed at: {stats.get('indexed_at')}")
    buf.add(f"- db: `{stats.get('db_path')}` ({_bytes(int(stats.get('db_bytes') or 0))})")
    buf.add(f"- files: {stats.get('files')} · lines: {int(stats.get('lines') or 0):,}")
    buf.add(f"- symbols: {stats.get('symbols')} · references: {stats.get('refs')}")
    buf.add(
        f"- edges: {stats.get('edges')} "
        f"({float(stats.get('resolution_rate') or 0) * 100:.1f}% resolved overall, "
        f"{float(stats.get('internal_resolution_rate') or 0) * 100:.1f}% of in-repo call sites; "
        f"{stats.get('external_edges')} point at third-party code)"
    )
    by_lang = stats.get("by_lang")
    if isinstance(by_lang, dict):
        buf.add(f"- languages: {', '.join(f'{k} {v}' for k, v in by_lang.items())}")
    by_kind = stats.get("by_kind")
    if isinstance(by_kind, dict):
        buf.add(f"- kinds: {', '.join(f'{k} {v}' for k, v in by_kind.items())}")
    reasons = stats.get("edge_reasons")
    if isinstance(reasons, dict):
        buf.add()
        buf.add("**Edge resolution by rule**")
        for reason, count in sorted(reasons.items(), key=lambda kv: -int(kv[1])):
            buf.add(f"- {reason}: {count}")
    return buf.render()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_FENCE = {
    "python": "python",
    "typescript": "typescript",
    "tsx": "tsx",
    "javascript": "javascript",
    "go": "go",
}


def _fence_lang(lang: str) -> str:
    return _FENCE.get(lang, "")


def _trim(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _first_line(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return _trim(text.strip().splitlines()[0], limit)


def _short(name: str, limit: int = 28) -> str:
    if len(name) <= limit:
        return name
    return "…" + name[-(limit - 1) :]


def _confidence_flag(confidence: float) -> str:
    if confidence >= 0.9:
        return ""
    return "~" if confidence >= 0.5 else "?"


def _bytes(size: int) -> str:
    step = 1024.0
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step:
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= step
    return f"{value:.1f}TB"
