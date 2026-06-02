"""Single source of truth for parsing unified diffs.

Both the LLM-facing per-file diff view (src/tools.py) and the deterministic
review-anchoring logic (src/pipeline.py) are built from `parse_diff`, so the
line numbers the model sees always match the line numbers anchoring trusts.
"""

import re
from dataclasses import dataclass

_HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class DiffLine:
    kind: str  # "add", "del", "context", or "hunk"
    text: str
    side: str = ""  # "RIGHT" for add/context, "LEFT" for del, "" for hunk
    number: int = 0  # new-side line for add/context, old-side line for del


def clean_diff_path(path: str) -> str:
    path = path.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return ""
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def parse_hunk_start(header: str) -> tuple[int, int] | None:
    match = _HUNK_RE.match(header)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_diff(diff: str) -> dict[str, list[DiffLine]]:
    """Walk a unified diff once and return ordered DiffLines per file path."""
    files: dict[str, list[DiffLine]] = {}
    current = ""
    old_path = ""
    old_line = 0
    new_line = 0
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            current = ""
            old_path = ""
            old_line = 0
            new_line = 0
        elif line.startswith("--- "):
            old_path = clean_diff_path(line[4:])
        elif line.startswith("+++ "):
            new_path = clean_diff_path(line[4:])
            current = new_path or old_path
            if current:
                files.setdefault(current, [])
        elif line.startswith("@@") and current:
            hunk_start = parse_hunk_start(line)
            if hunk_start is not None:
                old_line, new_line = hunk_start
            files[current].append(DiffLine(kind="hunk", text=line))
        elif current and (old_line > 0 or new_line > 0):
            if line.startswith("+") and not line.startswith("+++"):
                files[current].append(DiffLine("add", line[1:], "RIGHT", new_line))
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                files[current].append(DiffLine("del", line[1:], "LEFT", old_line))
                old_line += 1
            elif line.startswith(" "):
                files[current].append(DiffLine("context", line[1:], "RIGHT", new_line))
                old_line += 1
                new_line += 1
    return files
