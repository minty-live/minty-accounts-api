"""The two mails this service sends draw in the Minty tokens, main's palette (owner, 2026-11-18): #4fc7c7 for the button, link teal #2e9b9b for the
code and the company name, the neutral greys for text - and never Brand #54D3DA as a fill."""

from __future__ import annotations

import re

from hub.email_signin import _code_email_html
from hub.invitations import _build_invitation_html

PALETTE = {
    "#ffffff", "#fff", "#f5f7fa", "#eef1f4", "#16202e", "#6b7380", "#8b93a0",
    "#2e9b9b", "#4fc7c7",
}


def _colours(html: str) -> set[str]:
    return {c.lower() for c in re.findall(r"#[0-9a-fA-F]{3,6}\b", html)}


def test_sign_in_code_mail_uses_the_palette():
    html = _code_email_html("123456")
    assert _colours(html) <= PALETTE, _colours(html) - PALETTE
    assert "color:#2e9b9b;\">123456<" in html.replace("letter-spacing:8px;", "")


def test_invitation_mail_uses_the_palette():
    html = _build_invitation_html("Acme", "manager", "Jo", "https://x.test/accept?t=1")
    assert _colours(html) <= PALETTE, _colours(html) - PALETTE
    assert "background:#4fc7c7;color:#ffffff;" in html
    assert "#54d3da" not in html.lower()
