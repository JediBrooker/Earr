"""Text from outside Earr can't become markup or script.

Book titles, author names and release names come from Audible and from
indexers, where anyone can upload a torrent with any name.
"""

from app.util.templates import catalog

HOSTILE = '"><script>alert(1)</script>'


def test_printed_text_is_escaped():
    html = catalog.render("Auth.Login", title=HOSTILE, backup=False, redirect_uri="/")

    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_a_toast_message_cant_break_out_of_its_script():
    html = catalog.render("ToastBlock", toast_error=f"Failed: {HOSTILE}")

    assert "</script><script>alert(1)" not in html
    assert "alert(1)" in html  # still shown, as text


def test_a_redirect_cant_break_out_of_its_script():
    html = catalog.render("Redirect", redirect_uri=f"/next?a=1&b={HOSTILE}")

    assert "<script>alert(1)" not in html
    assert "window.location.href = " in html
