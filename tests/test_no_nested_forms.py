"""No template may nest one form inside another.

HTML does not allow it, and the failure is silent. A browser's parser that
meets a ``<form>`` start tag while another is still open **discards the inner
one** and keeps going; the controls inside are then submitted with the OUTER
form. Nothing errors, nothing is logged, the server receives a well-formed
POST, and every control in the dropped form quietly posts to the wrong URL.

That is exactly what happened twice on the fields page: wrapping the table in
a form for bulk selection destroyed the per-row category form, the confirm
form and the delete form. Clicking "ثبت" on a row's category dropdown posted
to ``/fields/bulk-category``, which moved nothing and redirected to
``?assigned=0``. From the operator's side: "I changed it and nothing
happened." The same bug had quietly killed the models page's clear-key button,
which posted the whole settings form instead -- saving the model and clearing
no key.

The whole test suite missed both. Every test drives the routes directly with
httpx, and httpx does not run an HTML parser, so a dead form looks exactly
like a working one. The only place this can be caught is by reading the markup.
"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path("app/panel/templates")


def _markup_only(source: str) -> str:
    """Drop Jinja comments and tags: they are not markup.

    A comment that explains the rule necessarily contains the word ``form``, and
    a naive scan would count prose about forms as a form.
    """
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    source = re.sub(r"\{%.*?%\}", "", source, flags=re.S)
    return source


def _scan(source: str) -> tuple[list[int], int]:
    """Return (lines where a form opens inside another, leftover depth)."""
    nested: list[int] = []
    depth = 0
    for match in re.finditer(r"<(/?)form\b[^>]*>", source, re.IGNORECASE):
        line = source[: match.start()].count("\n") + 1
        if match.group(1) == "/":
            depth = max(0, depth - 1)
        else:
            if depth > 0:
                nested.append(line)
            depth += 1
    return nested, depth


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_no_template_nests_a_form(path: Path):
    source = _markup_only(path.read_text(encoding="utf-8"))
    nested, leftover = _scan(source)
    assert not nested, (
        f"{path.name} opens a form at {nested} while another is already open; "
        "the browser discards it and its controls post to the outer form. "
        "Use the `form` attribute to bind a control to a form it is not inside."
    )
    assert leftover == 0, f"{path.name} has {leftover} unclosed form tag(s)"


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_every_form_closing_tag_is_accounted_for(path: Path):
    """A stray ``</form>`` closes an outer form the author did not mean to."""
    source = _markup_only(path.read_text(encoding="utf-8"))
    opens = len(re.findall(r"<form\b", source, re.IGNORECASE))
    closes = len(re.findall(r"</form\s*>", source, re.IGNORECASE))
    assert opens == closes, f"{path.name}: {opens} open vs {closes} close"


def test_the_models_clear_key_form_is_a_sibling_of_the_settings_form():
    """It was nested, so "پاک کردن کلید" posted the settings form instead."""
    source = TEMPLATES.joinpath("models.html").read_text(encoding="utf-8")
    assert 'action="/models/{{ selected.id }}/clear-key"' in source
    assert 'form="clear-key-form-{{ selected.id }}"' in source, (
        "the button must bind to its own form by id"
    )


def test_no_template_submits_a_form_by_nesting():
    """A belt-and-braces count: no form may wrap a table that holds forms."""
    for path in TEMPLATES.glob("*.html"):
        source = _markup_only(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"<form[^>]*>", source, re.IGNORECASE):
            body = source[match.end() : match.end() + 4000]
            close = re.search(r"</form\s*>", body, re.IGNORECASE)
            if close and "<table" in body[: close.start()]:
                line = source[: match.start()].count("\n") + 1
                # A form may contain a table only if that table holds no forms
                # of its own; _scan above is what actually enforces it, so here
                # we only report the shape.
                if "<form" in body[: close.start()]:
                    pytest.fail(f"{path.name} line {line}: a form wraps a table containing forms")