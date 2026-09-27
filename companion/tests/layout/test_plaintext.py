"""Plain-text extraction: markup removed, whitespace collapsed, NFC."""

from __future__ import annotations

import pytest

from cremind_tag.layout.plaintext import plain_text


@pytest.mark.parametrize(("source", "expected"), [
    ("# Heading\nText", "Heading Text"),
    ("**bold** and __strong__, *em* and _em_", "bold and strong, em and em"),
    ("~~gone~~ stays", "gone stays"),
    ("see [the docs](https://x.example/a_(b)) now", "see the docs now"),
    ("![logo](https://x/y.png) done", "logo done"),
    ("use `pip install` then", "use pip install then"),
    ("```python\nprint('hi')\n```", "print('hi')"),
    ("> quoted\n> text", "quoted text"),
    ("- one\n- two\n* three", "one two three"),
    ("1. first\n2. second", "1. first 2. second"),
    ("| a | b |\n|---|---|\n| 1 | 2 |", "a b 1 2"),
    ("snake_case_name and 2*3*4", "snake_case_name and 2*3*4"),
    (r"a \*literal\* star", "a *literal* star"),
    ("[ref][1]\n\n[1]: https://example.com", "ref"),
    ("---\ntext\n***", "text"),
])
def test_markdown_is_removed(source: str, expected: str) -> None:
    assert plain_text(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("<b>bold</b> &amp; <i>it</i>", "bold & it"),
    ("line<br>next<br/>last", "line\nnext\nlast"),
    ("<p>one</p><p>two</p>", "one\ntwo"),
    ("<script>alert(1)</script>safe<style>p{}</style>", "safe"),
    ("<!-- note -->kept", "kept"),
    ("go to <https://example.com/x>", "go to https://example.com/x"),
    ("5 &lt; 6 &gt; 4 &#x1F600;", "5 < 6 > 4 😀"),
])
def test_html_is_removed(source: str, expected: str) -> None:
    assert plain_text(source, keep_newlines=True) == expected


def test_whitespace_controls_and_nfc() -> None:
    assert plain_text("  a \t b\r\n\n\n  c  ", keep_newlines=True) == "a b\nc"
    assert plain_text("  a \t b\r\n\n\n  c  ") == "a b c"
    assert plain_text("é x\x00y\x07z") == "é xyz"
    assert plain_text("Tiếng Việt") == "Tiếng Việt"
    assert plain_text("no break　ideographic") == "no break　ideographic"
    assert plain_text(None) == "" and plain_text("") == ""
    assert plain_text("**raw**", markup=False) == "**raw**"
