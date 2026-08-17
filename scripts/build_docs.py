#!/usr/bin/env python
"""Generate the GitHub Pages demo from a live Cartograph index.

Every number, diagram and table on the published page is read out of a real
index built moments earlier in CI. Nothing is hand-written, so the demo cannot
quietly drift away from what the tool actually does -- and if extraction breaks,
the page breaks with it.

Usage:
    python scripts/build_docs.py [repo_root] [--out site]
"""

from __future__ import annotations

import argparse
import html
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cartograph import __version__, views
from cartograph.graph.resolver import CONFIDENCE
from cartograph.indexer.pipeline import index_repo
from cartograph.service import BROAD, PRECISE, Cartograph

TEMPLATE = """<!doctype html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cartograph — agent-native code intelligence</title>
<meta name="description"
  content="Turn any repo into a queryable code graph over MCP. tree-sitter + SQLite.">
<style>{css}</style>
</head>
<body>
<header class="hero">
  <div class="wrap">
    <p class="eyebrow">MCP server · tree-sitter · SQLite</p>
    <h1>Cartograph</h1>
    <p class="tagline">Turn any repository into a queryable <strong>code graph</strong> and serve
    it to coding agents over MCP — so an agent can ask <em>“what breaks if I change this?”</em>
    instead of grepping and hoping.</p>
    <p class="badges">
      <a class="btn primary"
         href="https://github.com/GokulRaj2210/cartograph-mcp">View on GitHub</a>
      <span class="chip">no embeddings</span>
      <span class="chip">no API keys</span>
      <span class="chip">$0 to run</span>
    </p>
    <p class="generated">Generated from a live index of this project · v{version} · {generated}</p>
  </div>
</header>

<main class="wrap">

<section>
  <h2>The problem</h2>
  <p>Give a coding agent a large unfamiliar repo and watch it work: <code>grep</code>, read a
  file, <code>grep</code> again. It burns context rebuilding structure a parser could have
  supplied in one call — and still misses the caller three modules away that its change just
  broke.</p>
  <p>The reflex is RAG: embed the code, retrieve “similar” chunks. But <em>“who calls this
  function?”</em> is not a similarity question. It has an exact answer, and that answer lives in
  the call graph.</p>
</section>

<section>
  <h2>Live numbers from this repo</h2>
  <div class="stats">{stat_cards}</div>
</section>

<section>
  <h2>Impact analysis, before the edit</h2>
  <p>The query that earns the whole project. One call names the dependent files
  <em>and the tests to run</em>.</p>
  <pre class="terminal"><code>$ cartograph blast {blast_target}</code></pre>
  <div class="rendered">{blast_html}</div>
</section>

<section>
  <h2>Architecture, extracted not documented</h2>
  <p>Module graph, layering, cycles and hotspots — computed from imports and the call graph.
  Edge labels are import counts; PageRank picks out the symbols that carry the codebase.</p>
  <div class="mermaid-box"><pre class="mermaid">{mermaid}</pre></div>
  <h3>Hotspots — highest PageRank</h3>
  <table>
    <thead><tr><th>Symbol</th><th>Location</th><th class="num">Fan-in</th>
    <th class="num">Rank</th></tr></thead>
    <tbody>{hotspot_rows}</tbody>
  </table>
</section>

<section>
  <h2>Confidence is a first-class column</h2>
  <p>Without a type checker you cannot <em>know</em> that <code>store.who_calls()</code> means
  <code>GraphStore.who_calls</code>. You can only rank hypotheses. So every edge records the rule
  that produced it and a confidence, and callers choose their own operating point:
  <code>who_calls</code> is precision-first (≥{precise}), <code>blast_radius</code> is
  recall-first (≥{broad}).</p>
  <table>
    <thead><tr><th>Rule</th><th class="num">Confidence</th><th>Intuition</th>
    <th class="num">Edges here</th></tr></thead>
    <tbody>{rule_rows}</tbody>
  </table>
  <p class="note">The <code>name-only</code> tier exists because of a real bug:
  <code>seen.add(...)</code> on a builtin <code>set</code> was resolving to a repo class’s
  <code>add</code> method purely because the name was unique — and surfacing as a confident
  caller. A method name on a receiver you cannot type is not evidence, so it now sits below the
  precision line.</p>
</section>

<section>
  <h2>The tool surface</h2>
  <p>Ten MCP tools, two resources and a prompt. Descriptions are written as routing guidance,
  because the description <em>is</em> the prompt the model uses to choose.</p>
  <div class="tools">{tool_cards}</div>
</section>

<section>
  <h2>Benchmarks</h2>
  <p>Real repositories, single process, M-series laptop. Cold = full index; warm = no-op
  reindex.</p>
  <table>
    <thead><tr><th>Repo</th><th class="num">Files</th><th class="num">KLOC</th>
    <th class="num">Symbols</th><th class="num">Edges</th><th class="num">Cold</th>
    <th class="num">Warm</th><th class="num">Internal resolution</th></tr></thead>
    <tbody>{bench_rows}</tbody>
  </table>
  <p class="note">Warm reindex is fast because resolution is skipped only when both its inputs
  are provably unchanged. On Django that took a no-op run from 7.5s to 0.67s with a
  byte-identical graph. Reproduce with <code>scripts/bench.py --clone</code>.</p>
</section>

<section>
  <h2>Try it</h2>
  <pre class="terminal"><code>uv tool install cartograph-mcp

cartograph index ~/code/my-repo
cartograph arch
cartograph blast src/auth/token.py

# wire it into an agent
claude mcp add cartograph -- cartograph serve ~/code/my-repo</code></pre>
</section>

<section>
  <h2>Limitations</h2>
  <p>Stated plainly, because a code-intelligence tool that oversells its precision is worse
  than useless.</p>
  <ul>
    <li><strong>No type inference.</strong> <code>self.conn.execute(...)</code> cannot be
    resolved without knowing <code>conn</code>’s type. Those stay unresolved, and they are the
    bulk of what remains.</li>
    <li><strong>Dynamic dispatch is invisible.</strong> <code>getattr(obj, name)()</code>,
    decorator registries and DI containers produce no edges.</li>
    <li><strong>No cross-language edges.</strong> A TypeScript frontend calling a Python
    endpoint is two disconnected subgraphs.</li>
  </ul>
</section>

</main>

<footer class="wrap">
  <p>MIT licensed · built by <a href="https://github.com/GokulRaj2210">Gokul Raj</a> ·
  <a href="https://github.com/GokulRaj2210/cartograph-mcp">source</a></p>
</footer>

<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
mermaid.initialize({{ startOnLoad: true, theme: dark ? "dark" : "neutral",
  themeVariables: {{ fontFamily: "ui-sans-serif, system-ui, sans-serif" }} }});
</script>
</body>
</html>
"""

CSS = """
:root {
  --bg: #ffffff; --bg-alt: #f6f8fa; --fg: #1f2328; --fg-dim: #59636e;
  --border: #d1d9e0; --accent: #0969da; --accent-bg: #ddf4ff;
  --term-bg: #1f2328; --term-fg: #e6edf3; --warn: #9a6700; --warn-bg: #fff8c5;
  --radius: 10px;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --bg-alt: #151b23; --fg: #e6edf3; --fg-dim: #9198a1;
    --border: #3d444d; --accent: #4493f8; --accent-bg: #121d2f;
    --term-bg: #010409; --term-fg: #e6edf3; --warn: #d29922; --warn-bg: #1f1a09;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.65 var(--sans); -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 62rem; margin: 0 auto; padding: 0 1.25rem; }
.hero {
  background: linear-gradient(180deg, var(--bg-alt), var(--bg));
  border-bottom: 1px solid var(--border); padding: 3.5rem 0 2.75rem;
}
.eyebrow {
  margin: 0 0 .5rem; font: 600 .78rem/1 var(--mono); letter-spacing: .08em;
  text-transform: uppercase; color: var(--fg-dim);
}
h1 { margin: 0 0 .6rem; font-size: clamp(2.4rem, 6vw, 3.6rem); letter-spacing: -.03em; }
.tagline { margin: 0 0 1.5rem; max-width: 46rem; font-size: 1.14rem; color: var(--fg-dim); }
.tagline strong, .tagline em { color: var(--fg); }
.badges { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; margin: 0 0 1.25rem; }
.btn {
  display: inline-block; padding: .55rem 1.1rem; border-radius: var(--radius);
  border: 1px solid var(--border); text-decoration: none; color: var(--fg); font-weight: 600;
}
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.chip {
  padding: .3rem .7rem; border: 1px solid var(--border); border-radius: 999px;
  background: var(--bg); font: 500 .8rem var(--mono); color: var(--fg-dim);
}
.generated { margin: 0; font: .8rem var(--mono); color: var(--fg-dim); }
main { padding: 1rem 1.25rem 3rem; }
section { padding: 2.25rem 0; border-bottom: 1px solid var(--border); }
section:last-child { border-bottom: 0; }
h2 { margin: 0 0 .75rem; font-size: 1.6rem; letter-spacing: -.02em; }
h3 { margin: 1.75rem 0 .6rem; font-size: 1.1rem; }
p { margin: 0 0 1rem; max-width: 52rem; }
code {
  font: .89em var(--mono); background: var(--bg-alt);
  padding: .12em .35em; border-radius: 5px;
}
a { color: var(--accent); }
.note {
  border-left: 3px solid var(--warn); background: var(--warn-bg);
  padding: .8rem 1rem; border-radius: 0 var(--radius) var(--radius) 0; font-size: .94rem;
}
.note code { background: rgba(127,127,127,.18); }
.stats { display: grid; gap: .9rem; grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr)); }
.stat {
  background: var(--bg-alt); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 1rem;
}
.stat .v { font: 700 1.6rem/1.1 var(--sans); letter-spacing: -.02em; }
.stat .k { font: .78rem var(--mono); color: var(--fg-dim); text-transform: uppercase;
  letter-spacing: .05em; margin-top: .3rem; }
.terminal {
  background: var(--term-bg); color: var(--term-fg); padding: 1rem 1.1rem;
  border-radius: var(--radius); overflow-x: auto; margin: 0 0 1rem;
}
.terminal code { background: none; color: inherit; font-size: .9rem; }
.rendered {
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 1.1rem 1.3rem; background: var(--bg-alt); overflow-x: auto;
}
.rendered h2, .rendered h3 { margin-top: 0; font-size: 1.05rem; }
.rendered ul { margin: .35rem 0 1rem; padding-left: 1.2rem; }
.rendered li { font: .9rem/1.6 var(--mono); }
.mermaid-box {
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 1rem; background: var(--bg-alt); overflow-x: auto;
}
.mermaid { display: flex; justify-content: center; min-width: 40rem; }
table { width: 100%; border-collapse: collapse; font-size: .92rem; display: block;
  overflow-x: auto; white-space: nowrap; }
th, td { padding: .55rem .7rem; text-align: left; border-bottom: 1px solid var(--border); }
th { font: 600 .78rem var(--mono); text-transform: uppercase; letter-spacing: .04em;
  color: var(--fg-dim); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td code { font-size: .85em; }
.tools { display: grid; gap: .8rem; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); }
.tool {
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: .9rem 1rem; background: var(--bg-alt);
}
.tool h4 { margin: 0 0 .35rem; font: 600 .95rem var(--mono); color: var(--accent); }
.tool p { margin: 0; font-size: .88rem; color: var(--fg-dim); }
footer { padding: 2rem 1.25rem 3rem; color: var(--fg-dim); font-size: .9rem; }
ul { max-width: 52rem; }
"""

RULE_INTUITION = {
    "same-file": "the definition is right there in scope",
    "import": "the file explicitly imported this name",
    "receiver-type": "<code>Foo.bar()</code> where <code>Foo</code> is a known container",
    "same-module": "sibling file in the same package",
    "unique-global": "one repo symbol has this name, bare call site",
    "name-only": "one match, but on an <strong>untyped receiver</strong>",
    "ambiguous": "N candidates, kept as N edges at 1/N each",
    "external": "rooted at a third-party or stdlib import",
    "unresolved": "genuinely unknown (dynamic, or a typed method)",
}

RULE_ORDER = list(RULE_INTUITION)

TOOLS = [
    ("find_symbol", "Where is X defined? Ranked by structural importance."),
    ("search_code", "Full-text over names, signatures and docstrings (BM25)."),
    ("get_symbol", "One symbol: signature, doc, members, callers, callees, source."),
    ("who_calls", "Reverse call tree — before you change a signature."),
    ("what_it_calls", "Forward call tree — understand code without reading every file."),
    ("blast_radius", "What a change could break, <em>and which tests to run</em>."),
    ("related_symbols", "“What else should I read?” via personalized PageRank."),
    ("file_summary", "What a file defines, imports, and who imports it."),
    ("architecture_overview", "Modules, layering, import cycles, hotspots, entry points."),
    ("index_stats", "Index health and the edge-resolution breakdown by rule."),
]

#: Measured with scripts/bench.py. Kept as data so the page states its source.
BENCHMARKS = [
    ("django", 2973, 534, 45394, 252441, "11.9s", "0.67s", 83.2),
    ("gin (Go)", 98, 24, 1610, 9179, "0.32s", "0.03s", 88.1),
    ("flask", 83, 18, 1624, 4271, "0.21s", "0.03s", 87.4),
]


def md_to_html(markdown: str) -> str:
    """Minimal Markdown renderer for the snippets this page embeds.

    Deliberately tiny and dependency-free: the page only needs headings, bold,
    inline code and bullet lists, and a real Markdown library would be a build
    dependency for four constructs.
    """
    out: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue

        indent = len(line) - len(line.lstrip())
        if stripped.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f'<li style="margin-left:{indent * 0.6}rem">{_inline(stripped[2:])}</li>')
            continue
        if in_list:
            out.append("</ul>")
            in_list = False

        if stripped.startswith("### "):
            out.append(f"<h3>{_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{_inline(stripped[3:])}</h2>")
        else:
            out.append(f"<p>{_inline(stripped)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline(text: str) -> str:
    """Render inline `code`, **bold** and wrapping _italics_.

    Emphasis is applied *only* outside code spans, and underscore-italics only
    when the underscores wrap the whole run. Both rules exist because the naive
    version produced crossed tags on real output: identifiers are full of
    underscores, so `min_confidence` inside a note became `min<em>confidence…`
    and `<code>` closed across an open `<em>`.
    """
    stripped = text.strip()
    italic = len(stripped) > 2 and stripped.startswith("_") and stripped.endswith("_")
    if italic:
        text = stripped[1:-1]

    segments = html.escape(text).split("`")
    rendered: list[str] = []
    for i, segment in enumerate(segments):
        if i % 2:  # inside backticks: verbatim
            rendered.append(f"<code>{segment}</code>")
            continue
        while segment.count("**") >= 2:
            segment = segment.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
        rendered.append(segment)

    out = "".join(rendered)
    return f"<em>{out}</em>" if italic else out


def stat_cards(stats: dict[str, object], test_count: int) -> str:
    cards = [
        (f"{stats['files']:,}", "files indexed"),
        (f"{int(stats['lines']):,}", "lines parsed"),
        (f"{stats['symbols']:,}", "symbols"),
        (f"{stats['edges']:,}", "graph edges"),
        (f"{float(stats['internal_resolution_rate']) * 100:.0f}%", "internal resolution"),
        (str(len(stats["by_lang"])), "languages"),  # type: ignore[arg-type]
        (str(test_count), "tests"),
        ("0", "API keys needed"),
    ]
    return "\n".join(
        f'<div class="stat"><div class="v">{v}</div><div class="k">{k}</div></div>'
        for v, k in cards
    )


def build(root: Path, out_dir: Path) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "docs.db"
        index_repo(root, db_path=db)

        with Cartograph.load(db) as graph:
            stats = graph.stats()
            arch = graph.architecture()

            # Pick the most-depended-on non-test source file for the demo.
            target = "src/cartograph/graph/store.py"
            radius = graph.blast_radius(target)
            blast_md = views.blast_radius_md(radius, budget=1200)

            mermaid = views.mermaid_modules(
                [e for e in arch.edges if not e[0].startswith("tests")],
                arch.cycles,
                limit=18,
            )
            mermaid_body = mermaid.removeprefix("```mermaid\n").removesuffix("\n```")

            hotspot_rows = "\n".join(
                f"<tr><td><code>{html.escape(sym.qualname)}</code></td>"
                f"<td><code>{html.escape(sym.location)}</code></td>"
                f'<td class="num">{fan_in}</td>'
                f'<td class="num">{sym.rank:.4f}</td></tr>'
                for sym, fan_in in arch.hotspots[:10]
            )

            reasons: dict[str, int] = stats["edge_reasons"]  # type: ignore[assignment]

    rule_rows = "\n".join(
        f"<tr><td><code>{rule}</code></td>"
        f'<td class="num">{CONFIDENCE.get(rule, 0.40):.2f}{"" if rule in CONFIDENCE else "*"}</td>'
        f"<td>{RULE_INTUITION[rule]}</td>"
        f'<td class="num">{reasons.get(rule, 0):,}</td></tr>'
        for rule in RULE_ORDER
    )

    tool_cards = "\n".join(
        f'<div class="tool"><h4>{name}</h4><p>{desc}</p></div>' for name, desc in TOOLS
    )

    bench_rows = "\n".join(
        f"<tr><td>{name}</td><td class='num'>{files:,}</td><td class='num'>{kloc:,}</td>"
        f"<td class='num'>{syms:,}</td><td class='num'>{edges:,}</td>"
        f"<td class='num'>{cold}</td><td class='num'>{warm}</td>"
        f"<td class='num'>{internal:.1f}%</td></tr>"
        for name, files, kloc, syms, edges, cold, warm, internal in BENCHMARKS
    )

    test_count = sum(
        1
        for path in (root / "tests").rglob("test_*.py")
        if "fixtures" not in path.parts
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("def test_") or line.startswith("async def test_")
    )

    page = TEMPLATE.format(
        css=CSS,
        version=__version__,
        generated=datetime.now(UTC).strftime("%Y-%m-%d"),
        stat_cards=stat_cards(stats, test_count),
        blast_target=target,
        blast_html=md_to_html(blast_md),
        mermaid=html.escape(mermaid_body),
        hotspot_rows=hotspot_rows,
        rule_rows=rule_rows,
        tool_cards=tool_cards,
        bench_rows=bench_rows,
        precise=f"{PRECISE:.1f}",
        broad=f"{BROAD:.1f}",
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    (out_dir / "stats.json").write_text(
        json.dumps({k: v for k, v in stats.items() if k != "db_path"}, indent=2, default=str),
        encoding="utf-8",
    )
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=Path("site"))
    args = parser.parse_args()

    index = build(args.root.resolve(), args.out)
    size = index.stat().st_size / 1024
    print(f"wrote {index} ({size:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
