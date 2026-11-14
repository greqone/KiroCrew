"""Markdown emphasis and remote images in WhatsApp's dialect.

WhatsApp reads ``*x*`` as bold and ``_x_`` as italic, so Markdown italic must be
rewritten, bold italic must leave no ``**`` litter, and an image reference must
not keep its ``!``.
"""

from __future__ import annotations

import pytest

from kiro_crew.whatsapp.renderer import to_whatsapp_text


@pytest.mark.parametrize(
    "src, shown",
    [
        ("an *italic* word", "an _italic_ word"),
        ("***both***", "*_both_*"),
        ("**bold** and *italic*", "*bold* and _italic_"),
        ("See ![diagram](https://x.com/d.png) above", "See diagram (https://x.com/d.png) above"),
    ],
)
def test_markdown_reaches_whatsapp_in_its_own_dialect(src: str, shown: str) -> None:
    assert to_whatsapp_text(src) == shown


@pytest.mark.parametrize(
    "src",
    [
        "a * b * c",  # spaced stars are not emphasis
        "2*3*4",  # inside a word
        "* item",  # a bullet, handled before
        "`*x*`",  # inside inline code
    ],
)
def test_a_star_that_is_not_italic_is_left_alone(src: str) -> None:
    assert "_" not in to_whatsapp_text(src)
