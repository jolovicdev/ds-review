import re

from src.diff_utils import clean_diff_path, parse_diff
from src.pipeline import _parse_diff_changed_lines
from src.tools import _extract_file_diff


def test_clean_diff_path_strips_prefixes_and_tab_metadata():
    assert clean_diff_path("b/src/app.py") == "src/app.py"
    assert clean_diff_path("a/src/app.py\t") == "src/app.py"
    assert clean_diff_path("/dev/null") == ""


def test_rendered_view_and_change_map_agree_on_line_numbers():
    diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,4 @@
 context
+added right
-removed left
 context2"""

    rendered = _extract_file_diff(diff, "src/app.py")
    changed = _parse_diff_changed_lines(diff)

    for number in changed["src/app.py"]["RIGHT"]:
        assert f"R{number} + " in rendered
    for number in changed["src/app.py"]["LEFT"]:
        assert f"L{number} - " in rendered
    # both parsers are driven by the same walk, so the maps come straight from it
    rendered_right = {int(m) for m in re.findall(r"^R(\d+) \+ ", rendered, re.MULTILINE)}
    assert rendered_right == set(changed["src/app.py"]["RIGHT"])


def test_file_diff_handles_tab_suffixed_paths():
    # The previous exact-match parser missed `+++ b/path\t` and returned no hunks.
    diff = (
        "diff --git a/pricing.py b/pricing.py\n"
        "--- a/pricing.py\t\n+++ b/pricing.py\t\n"
        "@@ -1,2 +1,3 @@\n ctx\n+added\n ctx2"
    )

    rendered = _extract_file_diff(diff, "pricing.py")
    changed = _parse_diff_changed_lines(diff)

    assert "R2 + added" in rendered
    assert 2 in changed["pricing.py"]["RIGHT"]


def test_parse_diff_keeps_hunk_headers_for_the_model():
    diff = "+++ b/a.py\n@@ -1,1 +1,2 @@\n+x"
    kinds = [dl.kind for dl in parse_diff(diff)["a.py"]]
    assert kinds == ["hunk", "add"]
