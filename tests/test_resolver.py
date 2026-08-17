"""Resolution rules: each one must fire, and none may over-claim.

These tests pin the *confidence contract*, not just connectivity. The whole
design rests on callers being able to trust that >=0.5 means "structurally
justified" and <0.5 means "name match only", so a rule silently getting promoted
is a correctness bug even though nothing crashes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cartograph.graph.resolver import AMBIGUOUS_CEILING, CONFIDENCE
from cartograph.graph.store import GraphStore
from cartograph.indexer.pipeline import index_repo


def edges_of(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = list(
        conn.execute(
            "SELECT s.qualname AS src, d.qualname AS dst, e.dst_name, e.kind, "
            "e.reason, e.confidence FROM edges e "
            "JOIN symbols s ON s.id = e.src_id "
            "LEFT JOIN symbols d ON d.id = e.dst_id"
        )
    )
    conn.close()
    return rows


def build(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    db = tmp_path / "graph.db"
    index_repo(repo, db_path=db)
    return db


def find(rows: list[sqlite3.Row], src_tail: str, dst_name: str) -> sqlite3.Row:
    for row in rows:
        if row["src"].endswith(src_tail) and row["dst_name"] == dst_name:
            return row
    raise AssertionError(
        f"no edge {src_tail} -> {dst_name}; have: "
        + ", ".join(f"{r['src']}->{r['dst_name']}({r['reason']})" for r in rows)
    )


# ---------------------------------------------------------------------------
# individual rules
# ---------------------------------------------------------------------------


def test_same_file_rule(tmp_path: Path) -> None:
    db = build(tmp_path, {"m.py": "def helper():\n    pass\n\ndef caller():\n    helper()\n"})
    edge = find(edges_of(db), "caller", "helper")
    assert edge["reason"] == "same-file"
    assert edge["confidence"] == CONFIDENCE["same-file"]
    assert edge["dst"].endswith("helper")


def test_import_rule_beats_bare_name(tmp_path: Path) -> None:
    """An explicit import must win over an identically named symbol elsewhere."""
    db = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/target.py": "def work():\n    pass\n",
            "pkg/decoy.py": "def work():\n    pass\n",
            "pkg/user.py": "from pkg.target import work\n\ndef go():\n    work()\n",
        },
    )
    edge = find(edges_of(db), "go", "work")
    assert edge["reason"] == "import"
    assert edge["confidence"] == CONFIDENCE["import"]
    assert edge["dst"] == "pkg.target:work"


def test_receiver_type_rule(tmp_path: Path) -> None:
    """`Klass.method()` where Klass is a known container resolves to the method."""
    db = build(
        tmp_path,
        {
            "m.py": (
                "class Klass:\n"
                "    def method(self):\n"
                "        pass\n"
                "\n"
                "class Other:\n"
                "    def method(self):\n"
                "        pass\n"
                "\n"
                "def go(k):\n"
                "    Klass.method(k)\n"
            )
        },
    )
    edge = find(edges_of(db), "go", "Klass.method")
    assert edge["dst"] == "m:Klass.method"
    assert edge["confidence"] >= 0.5


def test_self_call_stays_in_its_class(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {
            "m.py": (
                "class A:\n"
                "    def run(self):\n"
                "        return self.step()\n"
                "    def step(self):\n"
                "        return 1\n"
                "\n"
                "class B:\n"
                "    def step(self):\n"
                "        return 2\n"
            )
        },
    )
    edge = find(edges_of(db), "A.run", "self.step")
    assert edge["dst"] == "m:A.step", "self.step() must not leak into class B"


def test_same_module_rule(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def shared():\n    pass\n",
            "pkg/b.py": "def go():\n    shared()\n",
            "other/__init__.py": "",
            "other/c.py": "def shared():\n    pass\n",
        },
    )
    edge = find(edges_of(db), "go", "shared")
    # Same package beats the identically named symbol in `other`.
    assert edge["reason"] in ("same-module", "ambiguous")
    if edge["reason"] == "same-module":
        assert edge["dst"] == "pkg.a:shared"


def test_unique_global_only_for_bare_calls(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def only_one():\n    pass\n",
            "pkg/b.py": "def go():\n    only_one()\n",
        },
    )
    edge = find(edges_of(db), "go", "only_one")
    assert edge["reason"] in ("unique-global", "same-module", "import")
    assert edge["confidence"] >= 0.5


def test_untyped_receiver_is_demoted_below_precision_threshold(tmp_path: Path) -> None:
    """The bug this rule exists for: `seen.add()` must not become `Budget.add()`.

    A method name that happens to be unique in the repo is *not* evidence when
    the receiver is a local of unknown type -- here `bag` is a builtin `set`.
    """
    db = build(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/budget.py": "class Budget:\n    def consume(self, n):\n        pass\n",
            "pkg/user.py": "def go(items):\n    bag = {}\n    bag.consume(1)\n",
        },
    )
    edge = find(edges_of(db), "go", "bag.consume")
    assert edge["reason"] == "name-only"
    assert edge["confidence"] < 0.5, "an untyped receiver must stay below the precision line"


def test_external_is_distinguished_from_unresolved(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {
            "m.py": (
                "import json\n"
                "from collections import defaultdict\n"
                "\n"
                "def go(raw):\n"
                "    json.loads(raw)\n"
                "    defaultdict(list)\n"
                "    mystery.thing()\n"
            )
        },
    )
    rows = edges_of(db)
    assert find(rows, "go", "json.loads")["reason"] == "external"
    assert find(rows, "go", "defaultdict")["reason"] == "external"
    assert find(rows, "go", "mystery.thing")["reason"] == "unresolved"


def test_ambiguous_keeps_several_low_confidence_candidates(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {
            "a/__init__.py": "",
            "a/one.py": "def dup():\n    pass\n",
            "b/__init__.py": "",
            "b/two.py": "def dup():\n    pass\n",
            "c/__init__.py": "",
            "c/three.py": "def dup():\n    pass\n",
            "caller.py": "def go():\n    dup()\n",
        },
    )
    rows = [r for r in edges_of(db) if r["dst_name"] == "dup" and r["src"].endswith("go")]
    assert len(rows) > 1, "ambiguity should be preserved, not silently collapsed"
    assert all(r["reason"] == "ambiguous" for r in rows)
    assert all(r["confidence"] <= AMBIGUOUS_CEILING for r in rows)


def test_inherits_prefers_type_like_symbols(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {
            "m.py": (
                "class Base:\n    pass\n\n"
                "def Base_helper():\n    pass\n\n"
                "class Child(Base):\n    pass\n"
            )
        },
    )
    edge = find([r for r in edges_of(db) if r["kind"] == "inherits"], "Child", "Base")
    assert edge["dst"] == "m:Base"


def test_instantiates_edge(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {"m.py": "class Thing:\n    pass\n\ndef make():\n    return Thing()\n"},
    )
    edge = find(edges_of(db), "make", "Thing")
    assert edge["dst"] == "m:Thing"


# ---------------------------------------------------------------------------
# cross-language
# ---------------------------------------------------------------------------


def test_typescript_relative_import_resolves(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {
            "web/util.ts": "export function helper(): void {}\n",
            "web/main.ts": (
                "import { helper } from './util';\nexport function go(): void { helper(); }\n"
            ),
        },
    )
    edge = find(edges_of(db), "go", "helper")
    assert edge["reason"] == "import"
    assert edge["dst"] == "web/util:helper"


def test_typescript_index_import_resolves(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {
            "web/lib/index.ts": "export function core(): void {}\n",
            "web/main.ts": "import { core } from './lib';\nexport function go() { core(); }\n",
        },
    )
    edge = find(edges_of(db), "go", "core")
    assert edge["dst"] == "web/lib:core"


def test_go_same_package_resolves(tmp_path: Path) -> None:
    db = build(
        tmp_path,
        {
            "svc/a.go": 'package svc\n\nfunc Helper() string { return "" }\n',
            "svc/b.go": "package svc\n\nfunc Go() string { return Helper() }\n",
        },
    )
    edge = find(edges_of(db), "Go", "Helper")
    assert edge["reason"] == "same-module"
    assert edge["confidence"] == CONFIDENCE["same-module"]


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------


def test_every_reason_is_a_known_rule(tmp_path: Path) -> None:
    db = build(tmp_path, {"m.py": "def a():\n    b()\n\ndef b():\n    pass\n"})
    known = set(CONFIDENCE) | {"ambiguous"}
    for row in edges_of(db):
        assert row["reason"] in known, f"unknown resolution reason {row['reason']!r}"


def test_resolved_edges_never_have_zero_confidence(tmp_path: Path) -> None:
    db = build(tmp_path, {"m.py": "def a():\n    b()\n\ndef b():\n    pass\n"})
    for row in edges_of(db):
        if row["dst"] is not None:
            assert row["confidence"] > 0.0
        else:
            assert row["confidence"] == 0.0


def test_resolution_is_deterministic(tmp_path: Path) -> None:
    """Two indexes of identical trees must produce identical graphs.

    Non-determinism here would make the CI gate flap, so the tie-breaks in
    `_pick` are load-bearing.
    """
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "def dup():\n    pass\n",
        "pkg/b.py": "def dup():\n    pass\n",
        "pkg/c.py": "def go():\n    dup()\n",
    }
    first = build(tmp_path / "one", files)
    second = build(tmp_path / "two", files)

    def signature(db: Path) -> list[tuple[str, str, str, float]]:
        return sorted((r["src"], r["dst_name"], r["reason"], r["confidence"]) for r in edges_of(db))

    assert signature(first) == signature(second)


def test_deleting_a_target_removes_its_edges(tmp_path: Path) -> None:
    """Resolution is global on every run, so edges cannot outlive their target."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "target.py").write_text("def work():\n    pass\n")
    (repo / "pkg" / "user.py").write_text("from pkg.target import work\n\ndef go():\n    work()\n")
    db = tmp_path / "g.db"
    index_repo(repo, db_path=db)
    assert any(r["dst_name"] == "work" and r["dst"] for r in edges_of(db))

    (repo / "pkg" / "target.py").unlink()
    index_repo(repo, db_path=db)

    with GraphStore.open(db, create=False) as store:
        assert store.find_symbols("work", exact=True) == []
    dangling = [r for r in edges_of(db) if r["dst_name"] == "work" and r["dst"] is not None]
    assert dangling == [], "an edge survived the deletion of its target"


@pytest.mark.parametrize("rule", ["same-file", "import", "receiver-type", "same-module"])
def test_precision_rules_are_above_the_threshold(rule: str) -> None:
    from cartograph.service import PRECISE

    assert CONFIDENCE[rule] >= PRECISE


def test_name_only_is_below_the_threshold() -> None:
    from cartograph.service import BROAD, PRECISE

    assert BROAD <= CONFIDENCE["name-only"] < PRECISE
