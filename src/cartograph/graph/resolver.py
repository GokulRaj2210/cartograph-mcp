"""Turn raw references into graph edges.

This is the honest core of the project. Without a type checker you cannot know
that ``store.who_calls()`` means ``GraphStore.who_calls``; you can only rank
hypotheses. So instead of pretending, Cartograph runs an ordered rule cascade
and stamps every edge with the rule that produced it plus a confidence:

    rule                confidence  intuition
    ------------------  ----------  --------------------------------------------
    same-file             0.95      the definition is right there in scope
    import                0.90      the file explicitly imported this name
    receiver-type         0.85      `Foo.bar()` where `Foo` is a known container
    same-module           0.75      sibling file in the same package
    unique-global         0.60      one repo symbol has this name, bare call site
    name-only             0.45      one match, but on an untyped receiver
    ambiguous             <=0.40    N candidates, kept as N edges at 1/N each
    external              0.00      rooted at a third-party/stdlib import
    unresolved            0.00      genuinely unknown (dynamic, or a typed method)

Separating `external` from `unresolved` matters for honesty: on a typical repo
most "unresolved" call sites are just `typer.Option` or `sqlite3.execute`, and
lumping them together makes the coverage number look far worse than it is. The
metric that means something is *internal* resolution -- edges that could have
landed on a repo symbol and did.

Callers pick their own operating point: `who_calls` defaults to >=0.5 (precision
first, because an agent will *act* on the answer) while `blast_radius` drops to
0.3 (recall first, because a missed impacted test is the expensive mistake).

Resolution is a pure function of (references x symbols) and is recomputed in
full on every index run -- see the note in schema.sql on why that matters for
incremental correctness.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from cartograph.graph.store import GraphStore, RawRef, SymbolRow

#: (src_id, dst_id, dst_name, kind, line, confidence, reason)
EdgeTuple = tuple[int, int | None, str, str, int, float, str]

CONFIDENCE = {
    "same-file": 0.95,
    "import": 0.90,
    "receiver-type": 0.85,
    "same-module": 0.75,
    "unique-global": 0.60,
    "name-only": 0.45,
    "unresolved": 0.0,
}

#: Ambiguous matches are split across candidates; never above this ceiling.
AMBIGUOUS_CEILING = 0.40
MAX_AMBIGUOUS_CANDIDATES = 3

_CONTAINER_KINDS = frozenset({"class", "interface", "struct", "enum"})
_TYPE_LIKE_KINDS = frozenset({"class", "interface", "struct", "enum", "type"})


@dataclass(slots=True)
class ResolutionReport:
    edges: int = 0
    resolved: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    #: References in module/top-level scope have no owning symbol to hang an
    #: edge off, so they are counted rather than silently discarded.
    module_level_dropped: int = 0

    @property
    def rate(self) -> float:
        return self.resolved / self.edges if self.edges else 0.0


def report_from_store(store: GraphStore) -> ResolutionReport:
    """Rebuild the resolution summary from edges already on disk.

    Used when an index run skips resolution because nothing changed: the reported
    numbers must still describe the graph as stored. Lives here rather than on
    GraphStore so the dependency stays one-way -- the store must not import the
    resolver, or `cartograph cycles` (rightly) fails on our own source.
    """
    counts = store.counts()
    return ResolutionReport(
        edges=counts["edges"],
        resolved=counts["resolved_edges"],
        by_reason=store.edge_reasons(),
    )


def resolve_imports(store: GraphStore) -> int:
    """Point each import row at the file it refers to, or mark it external."""
    from cartograph.indexer.languages import adapter_for_language

    modules = store.module_map()
    file_ids = store.file_ids()
    updates: list[tuple[int | None, int, int]] = []
    internal = 0

    for row in store.all_imports():
        adapter = adapter_for_language(row["importer_lang"])
        target: str | None = None
        if adapter is not None:
            from cartograph.indexer.languages import ImportSpec

            spec = ImportSpec(
                raw=row["module"], module=row["module"], symbol=row["symbol"], alias=row["alias"]
            )
            target = adapter.resolve_import(spec, row["importer_path"], modules)
        dst_id = file_ids.get(target) if target else None
        if dst_id is not None:
            internal += 1
        updates.append((dst_id, 0 if dst_id else 1, row["id"]))

    store.set_import_targets(updates)
    return internal


def resolve_references(store: GraphStore) -> ResolutionReport:
    """Rebuild the whole edge table from the staged references."""
    symbols = store.all_symbol_rows()
    refs = store.all_refs()
    imports = store.resolved_imports()

    # --- indexes ----------------------------------------------------------
    by_name: dict[str, list[SymbolRow]] = defaultdict(list)
    by_file: dict[int, dict[str, list[SymbolRow]]] = defaultdict(lambda: defaultdict(list))
    by_module: dict[str, dict[str, list[SymbolRow]]] = defaultdict(lambda: defaultdict(list))
    by_qualname: dict[str, SymbolRow] = {}
    # container name -> its member symbols, for `Foo.bar()` receiver matching
    by_container: dict[str, dict[str, list[SymbolRow]]] = defaultdict(lambda: defaultdict(list))
    containers: dict[str, list[SymbolRow]] = defaultdict(list)

    for sym in symbols:
        by_name[sym.name].append(sym)
        by_file[sym.file_id][sym.name].append(sym)
        by_module[sym.module][sym.name].append(sym)
        by_qualname[sym.qualname] = sym
        if sym.parent_name:
            by_container[sym.parent_name][sym.name].append(sym)
        if sym.kind in _CONTAINER_KINDS:
            containers[sym.name].append(sym)

    # file_id -> {local alias: (target_file_id, imported symbol name or None)}
    aliases: dict[int, dict[str, tuple[int | None, str | None]]] = defaultdict(dict)
    # file_id -> {names known to come from outside the repo}
    external_names: dict[int, set[str]] = defaultdict(set)
    for imp in imports:
        key = imp.alias or imp.symbol
        if key:
            aliases[imp.file_id][key] = (imp.dst_file_id, imp.symbol)
            if imp.dst_file_id is None:
                external_names[imp.file_id].add(key)
                # `import a.b.c` binds the root name too.
                external_names[imp.file_id].add(imp.module.split(".", 1)[0].split("/", 1)[0])

    src_lookup = _source_symbol_index(symbols)
    module_by_file: dict[int, str] = {}
    for sym in symbols:
        module_by_file.setdefault(sym.file_id, sym.module)

    edges: list[EdgeTuple] = []
    by_reason: dict[str, int] = defaultdict(int)
    resolved = 0
    dropped = 0

    for ref in refs:
        src_id = src_lookup.get((ref.file_id, ref.src_local))
        if src_id is None:
            # Top-level/module scope: there is no owning symbol to hang the edge
            # off. Counted, not silently dropped, so `stats` stays truthful.
            dropped += 1
            continue

        tail = ref.dst_name.rsplit(".", 1)[-1]
        receiver = ref.dst_name.rsplit(".", 1)[0] if "." in ref.dst_name else None
        prefer = _TYPE_LIKE_KINDS if ref.kind in ("inherits", "instantiates") else None

        matches, reason = _rank_candidates(
            ref=ref,
            tail=tail,
            receiver=receiver,
            by_name=by_name,
            by_file=by_file,
            by_module=by_module,
            by_qualname=by_qualname,
            by_container=by_container,
            containers=containers,
            aliases=aliases,
            prefer_kinds=prefer,
            symbol_module=module_by_file.get(ref.file_id),
        )

        if not matches:
            root_name = ref.dst_name.split(".", 1)[0]
            known_external: frozenset[str] | set[str] = external_names.get(ref.file_id, frozenset())
            why = (
                "external"
                if (root_name in known_external or tail in known_external)
                else "unresolved"
            )
            edges.append((src_id, None, ref.dst_name, ref.kind, ref.line, 0.0, why))
            by_reason[why] += 1
            continue

        if reason == "ambiguous":
            share = min(AMBIGUOUS_CEILING, 1.0 / len(matches))
            for cand in matches[:MAX_AMBIGUOUS_CANDIDATES]:
                edges.append(
                    (src_id, cand.id, ref.dst_name, ref.kind, ref.line, share, "ambiguous")
                )
                resolved += 1
                by_reason["ambiguous"] += 1
        else:
            cand = matches[0]
            edges.append(
                (src_id, cand.id, ref.dst_name, ref.kind, ref.line, CONFIDENCE[reason], reason)
            )
            resolved += 1
            by_reason[reason] += 1

    count = store.replace_edges(edges)
    return ResolutionReport(
        edges=count,
        resolved=resolved,
        by_reason=dict(by_reason),
        module_level_dropped=dropped,
    )


# ---------------------------------------------------------------------------
# the cascade
# ---------------------------------------------------------------------------


def _rank_candidates(
    *,
    ref: RawRef,
    tail: str,
    receiver: str | None,
    by_name: dict[str, list[SymbolRow]],
    by_file: dict[int, dict[str, list[SymbolRow]]],
    by_module: dict[str, dict[str, list[SymbolRow]]],
    by_qualname: dict[str, SymbolRow],
    by_container: dict[str, dict[str, list[SymbolRow]]],
    containers: dict[str, list[SymbolRow]],
    aliases: dict[int, dict[str, tuple[int | None, str | None]]],
    prefer_kinds: frozenset[str] | None,
    symbol_module: str | None,
) -> tuple[list[SymbolRow], str]:
    """Return (candidates, rule name). Rules are tried most-specific first."""
    global_matches = by_name.get(tail, [])
    if prefer_kinds:
        preferred = [s for s in global_matches if s.kind in prefer_kinds]
        if preferred:
            global_matches = preferred
    if not global_matches:
        return [], "unresolved"

    # 1. same file
    local = [s for s in by_file.get(ref.file_id, {}).get(tail, []) if s in global_matches]
    if local:
        return [_pick(local)], "same-file"

    # 2. explicit import in this file
    file_aliases = aliases.get(ref.file_id, {})
    for alias_key in filter(None, (receiver, tail)):
        entry = file_aliases.get(alias_key)
        if entry is None:
            continue
        target_file, imported_symbol = entry
        if target_file is not None:
            hit = [s for s in global_matches if s.file_id == target_file]
            if hit:
                return [_pick(hit)], "import"
            # `from mod import Klass` then `Klass.method()`
            if imported_symbol:
                members = by_container.get(imported_symbol, {}).get(tail, [])
                scoped = [s for s in members if s.file_id == target_file]
                if scoped:
                    return [_pick(scoped)], "import"

    # 3. receiver names a container we know about: Foo.bar() / self.bar()
    if receiver:
        receiver_tail = receiver.rsplit(".", 1)[-1]
        if receiver_tail in containers:
            members = by_container.get(receiver_tail, {}).get(tail, [])
            if members:
                return [_pick(members)], "receiver-type"
        if receiver_tail in ("self", "this", "cls"):
            # Stay inside the enclosing container when we can name it.
            owner = (
                (ref.src_local or "").rsplit(".", 1)[0] if "." in (ref.src_local or "") else None
            )
            if owner:
                members = by_container.get(owner, {}).get(tail, [])
                if members:
                    return [_pick(members)], "receiver-type"

    # 4. same module (sibling files in the same package)
    if symbol_module:
        siblings = [
            s for s in by_module.get(symbol_module, {}).get(tail, []) if s in global_matches
        ]
        if siblings:
            return [_pick(siblings)], "same-module"

    # 5. unique repo-wide name.
    #
    # A *qualified* call whose receiver we could not type (`seen.add(...)`,
    # `buf.write(...)`) is a much weaker signal than a bare call: the receiver is
    # usually a local of some stdlib type, and matching on the method name alone
    # invents edges like `set.add -> TokenBudget.add`. Those get their own rule
    # below the 0.5 precision line, so they stay out of who_calls but still
    # contribute to recall-first blast_radius.
    if len(global_matches) == 1:
        return [global_matches[0]], "name-only" if receiver else "unique-global"

    # 6. ambiguous: keep several low-confidence edges rather than guessing one
    ordered = sorted(global_matches, key=lambda s: (s.kind != "function", s.qualname))
    return ordered, "ambiguous"


def _pick(candidates: list[SymbolRow]) -> SymbolRow:
    """Deterministic tie-break so a reindex never reshuffles the graph."""
    return sorted(candidates, key=lambda s: (len(s.qualname), s.qualname))[0]


def _source_symbol_index(symbols: list[SymbolRow]) -> dict[tuple[int, str | None], int]:
    """(file_id, local qualname) -> symbol id, for attributing a reference."""
    index: dict[tuple[int, str | None], int] = {}
    for sym in symbols:
        local = sym.qualname.split(":", 1)[1] if ":" in sym.qualname else sym.qualname
        index[(sym.file_id, local)] = sym.id
    return index
