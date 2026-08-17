"""The Pages demo is generated from a live index, so it is testable like code."""

from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_docs


class TagBalance(HTMLParser):
    """Detects unclosed and crossed tags -- the failure the renderer once had."""

    VOID = frozenset({"meta", "link", "br", "img", "hr", "input"})

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            self.problems.append(f"crossed </{tag}>, open={self.stack[-3:]}")
            while self.stack and self.stack.pop() != tag:
                pass
        else:
            self.problems.append(f"stray </{tag}>")


def check(markup: str) -> TagBalance:
    parser = TagBalance()
    parser.feed(markup)
    return parser


# ---------------------------------------------------------------------------
# the inline renderer
# ---------------------------------------------------------------------------


def test_code_span() -> None:
    assert build_docs._inline("use `foo()` now") == "use <code>foo()</code> now"


def test_bold() -> None:
    assert build_docs._inline("**Tests** first") == "<strong>Tests</strong> first"


def test_wrapping_italics() -> None:
    assert build_docs._inline("_a note_") == "<em>a note</em>"


def test_underscores_in_identifiers_are_left_alone() -> None:
    """Regression: `min_confidence` was becoming `min<em>confidence`."""
    out = build_docs._inline("_Recall-first view (min_confidence 0.3): treat as a list._")
    assert check(out).problems == []
    assert "min_confidence" in out
    assert out.startswith("<em>") and out.endswith("</em>")


def test_emphasis_is_not_applied_inside_code_spans() -> None:
    """Regression: emphasis inside `code` produced crossed tags."""
    out = build_docs._inline("`app.helpers:_internal_only` and `a**b`")
    assert check(out).problems == []
    assert "<em>" not in out
    assert "<strong>" not in out


def test_html_is_escaped() -> None:
    out = build_docs._inline("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_lists_and_headings() -> None:
    markup = build_docs.md_to_html("## Title\n\n- one\n- `two`\n\ntrailing text")
    balance = check(markup)
    assert balance.problems == []
    assert balance.stack == []
    assert "<h2>Title</h2>" in markup
    assert markup.count("<li") == 2
    assert "<ul>" in markup and "</ul>" in markup


def test_unterminated_list_still_closes() -> None:
    markup = build_docs.md_to_html("- only item")
    assert check(markup).stack == []


# ---------------------------------------------------------------------------
# the whole page
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def page(tmp_path_factory: pytest.TempPathFactory) -> str:
    out = tmp_path_factory.mktemp("site")
    root = Path(__file__).resolve().parent.parent
    index = build_docs.build(root, out)
    assert (out / ".nojekyll").exists()
    assert (out / "stats.json").exists()
    return index.read_text(encoding="utf-8")


def test_page_is_well_formed(page: str) -> None:
    balance = check(page)
    assert balance.problems == []
    assert balance.stack == []


def test_no_unfilled_template_placeholders(page: str) -> None:
    import re

    assert re.findall(r"\{[a-z_]+\}", page) == []


def test_page_carries_live_data(page: str) -> None:
    """If extraction breaks, the page must break with it."""
    assert "Blast radius" in page
    assert "Tests to run first" in page
    assert "flowchart LR" in page
    assert "cartograph.graph.store" in page


def test_javascript_braces_survived_formatting(page: str) -> None:
    """`.format()` on a template containing JS is a classic way to emit `{{`."""
    script = page.split('<script type="module">')[1].split("</script>")[0]
    assert "{{" not in script
    assert "}}" not in script


def test_mermaid_block_holds_raw_diagram_source(page: str) -> None:
    import html as html_mod

    block = page.split('<pre class="mermaid">')[1].split("</pre>")[0]
    source = html_mod.unescape(block)
    assert source.strip().startswith("flowchart LR")
    assert "```" not in source, "markdown fence leaked into the diagram"


def test_page_states_its_limitations(page: str) -> None:
    assert "No type inference" in page
    assert "Dynamic dispatch is invisible" in page
