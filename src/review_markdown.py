import re
from pathlib import Path

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SEVERITY_LABELS = {
    "P0": "Critical",
    "P1": "High",
    "P2": "Medium",
    "P3": "Low",
}
SEVERITY_BADGE_COLORS = {
    "P0": "b91c1c",
    "P1": "f97316",
    "P2": "ca8a04",
    "P3": "64748b",
}
SEVERITY_FROM_TEXT = {
    "critical": "P0",
    "p0": "P0",
    "high": "P1",
    "p1": "P1",
    "medium": "P2",
    "p2": "P2",
    "low": "P3",
    "info": "P3",
    "informational": "P3",
    "p3": "P3",
}

_SEVERITY_PREFIX_RE = re.compile(
    r"^\s*(?:\*\*)?`?(P[0-3])`?(?:\s+(?:Critical|High|Medium|Low))?(?:\*\*)?\s*[:\-]?\s*",
    re.IGNORECASE,
)
_BADGE_PREFIX_RE = re.compile(r"^\s*!\[(P[0-3])(?:\s+(?:Critical|High|Medium|Low))?\]\([^)]+\)\s*", re.IGNORECASE)
_HTML_BADGE_PREFIX_RE = re.compile(
    r'^\s*<img\b[^>]*\balt=["\'](P[0-3])(?:\s+(?:Critical|High|Medium|Low))?["\'][^>]*>\s*',
    re.IGNORECASE,
)
_SUGGESTION_RE = re.compile(r"```suggestion\n(.*?)\n```", re.DOTALL)
_ANY_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)\n```", re.DOTALL)


def severity_marker(severity: str | None, body: str = "") -> str:
    if severity:
        return SEVERITY_FROM_TEXT.get(severity.lower(), "P2")
    html_badge = _HTML_BADGE_PREFIX_RE.match(body)
    if html_badge:
        return html_badge.group(1).upper()
    badge = _BADGE_PREFIX_RE.match(body)
    if badge:
        return badge.group(1).upper()
    prefix = _SEVERITY_PREFIX_RE.match(body)
    if prefix:
        return prefix.group(1).upper()
    return "P2"


def normalize_inline_comment(
    body: str,
    *,
    severity: str | None,
    path: str,
    anchor_text: str | None,
    allow_suggestion: bool,
    title: str = "",
    details: str = "",
    suggestion: str | None = None,
) -> str:
    marker = severity_marker(severity, body)
    title, details, suggestion = comment_parts(
        title=title,
        details=details,
        suggestion=suggestion,
        body=body,
        path=path,
        anchor_text=anchor_text,
        allow_suggestion=allow_suggestion,
    )
    badge = severity_badge(marker)
    rendered = f"{badge} **{title}**"
    if details:
        rendered += f"\n\n{details}"
    if suggestion:
        rendered += f"\n\n{suggestion}"
    return rendered.strip()


def build_review_summary(
    *,
    generated_summary: str,
    comments: list[dict],
    unanchored_comments: list[dict],
    pr_title: str,
    event: str = "REQUEST_CHANGES",
) -> str:
    all_comments = comments + unanchored_comments
    if not all_comments:
        return "\n".join(
            [
                "## DS-Review",
                "",
                "No blocking issues found.",
                "",
                "Reviewed the changed diff and related call paths. No actionable bugs, security issues, or meaningful "
                "performance regressions were found.",
            ]
        )

    sorted_comments = sorted(
        all_comments,
        key=lambda c: (
            SEVERITY_ORDER.get(severity_marker(c.get("severity"), c.get("body", "")), 2),
            c["path"],
            c["line"],
        ),
    )
    highest = severity_marker(sorted_comments[0].get("severity"), sorted_comments[0].get("body", ""))
    alert = "[!IMPORTANT]" if highest in {"P0", "P1", "P2"} else "[!WARNING]"
    verdict = verdict_line(highest, len(sorted_comments), event)

    lines = [
        "## DS-Review",
        "",
        f"**PR:** {pr_title or summary_pr_line(generated_summary)}",
        "",
        f"> {alert}",
        f"> **Verdict:** {verdict}",
        "",
        f"### Findings ({len(sorted_comments)})",
        "",
    ]

    for comment in sorted_comments:
        marker = severity_marker(comment.get("severity"), comment.get("body", ""))
        location = format_location(comment)
        title, details, _suggestion = comment_parts(
            title=comment.get("title", ""),
            details=comment.get("details", ""),
            suggestion=comment.get("suggestion"),
            body=comment.get("body", ""),
            path=comment["path"],
            anchor_text=None,
            allow_suggestion=False,
        )
        lines.extend(
            [
                f"{severity_badge(marker)} **{title}**",
                "",
                f"`{location}`",
                "",
                details,
                "",
            ]
        )

    if unanchored_comments:
        lines.extend(
            [
                "> [!WARNING]",
                "> Some findings could not be anchored to changed diff lines, so they were kept here instead of",
                "> being posted as plain timeline comments.",
                "",
            ]
        )

    lines.extend(
        [
            "### Optional Recheck",
            "",
            "After pushing a fix, DS-Review will review the new commit automatically when pull-request sync reviews "
            "are enabled.",
            "Reply `@ds-review recheck` under the relevant inline finding for targeted verification of that thread.",
        ]
    )
    return "\n".join(lines).strip()


def severity_badge(marker: str) -> str:
    label = SEVERITY_LABELS[marker]
    color = SEVERITY_BADGE_COLORS[marker]
    url = f"https://img.shields.io/badge/{marker}-{label}-{color}?style=flat-square"
    return f'<img alt="{marker} {label}" src="{url}" height="20" align="absmiddle">'


def format_location(comment: dict) -> str:
    line = int(comment["line"])
    start_line = comment.get("start_line")
    if isinstance(start_line, int) and start_line < line:
        return f"{comment['path']}:{start_line}-{line}"
    return f"{comment['path']}:{line}"


def comment_parts(
    *,
    title: str,
    details: str,
    suggestion: str | None,
    body: str,
    path: str,
    anchor_text: str | None,
    allow_suggestion: bool,
) -> tuple[str, str, str | None]:
    title = plain_text(title)
    details = plain_text(details)
    suggestion_block = render_suggestion(
        suggestion=suggestion,
        body=body,
        path=path,
        anchor_text=anchor_text,
        allow_suggestion=allow_suggestion,
    )
    if title and details:
        return title, details, suggestion_block

    fallback = strip_suggestion_blocks(strip_severity_prefix(body))
    fallback_title, fallback_details = split_title_and_details(fallback)
    return title or fallback_title, details or fallback_details, suggestion_block


def render_suggestion(
    *,
    suggestion: str | None,
    body: str,
    path: str,
    anchor_text: str | None,
    allow_suggestion: bool,
) -> str | None:
    suggestion = suggestion or extract_suggestion(body)
    if not suggestion:
        return None
    language = language_for_path(path)
    code = suggestion.strip("\n")
    if allow_suggestion:
        code = preserve_anchor_for_insertions(body, code, anchor_text)
        return f"```suggestion\n{code}\n```"
    return f"```{language}\n{code}\n```"


def extract_suggestion(body: str) -> str | None:
    suggestion = _SUGGESTION_RE.search(body)
    if suggestion:
        return suggestion.group(1).strip("\n")
    fence = _ANY_FENCE_RE.search(body)
    if fence:
        return fence.group(1).strip("\n")
    return None


def strip_suggestion_blocks(body: str) -> str:
    body = _SUGGESTION_RE.sub("", body)
    return _ANY_FENCE_RE.sub("", body).strip()


def plain_text(value: str) -> str:
    value = strip_suggestion_blocks(strip_severity_prefix(value))
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def strip_severity_prefix(body: str) -> str:
    body = _HTML_BADGE_PREFIX_RE.sub("", body).strip()
    body = _BADGE_PREFIX_RE.sub("", body).strip()
    return _SEVERITY_PREFIX_RE.sub("", body).strip()


def split_title_and_details(body: str) -> tuple[str, str]:
    body = body.strip()
    if not body:
        return "Review finding", ""

    paragraphs = body.split("\n\n", 1)
    first_paragraph = paragraphs[0]
    rest = paragraphs[1] if len(paragraphs) > 1 else ""
    sentence, remainder = split_first_sentence(first_paragraph)
    title_source = sentence or first_paragraph
    title = compact_title(title_source)
    detail_parts = [part.strip() for part in [remainder, rest] if part.strip()]
    details = "\n\n".join(detail_parts)
    return title, details


def split_first_sentence(text: str) -> tuple[str, str]:
    match = re.search(r"(?<=[.!?])\s+", text)
    if match and match.start() <= 140:
        return text[: match.start()].strip(), text[match.end() :].strip()
    if len(text) <= 120:
        return text.strip(), ""
    return text[:120].strip(), text.strip()


def compact_title(text: str) -> str:
    title = re.sub(r"\s+", " ", text).strip()
    if title.startswith("**") and title.endswith("**"):
        title = title[2:-2].strip()
    if len(title) <= 120:
        return title
    return title[:117].rstrip() + "..."


def preserve_anchor_for_insertions(body: str, code: str, anchor_text: str | None) -> str:
    if not anchor_text:
        return code
    if not looks_like_insertion(body):
        return code
    anchor = anchor_text.rstrip()
    code_lines = code.splitlines()
    if any(line.strip() == anchor.strip() for line in code_lines):
        return code
    return "\n".join([anchor, *code_lines])


def looks_like_insertion(body: str) -> bool:
    lower = body.lower()
    if re.search(r"\b(rename|replace|change .* to|use .* instead)\b", lower):
        return False
    return bool(re.search(r"\b(add|insert|include|append|missing|does not assert|also)\b", lower))


def language_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "jsx",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".rb": "ruby",
        ".php": "php",
        ".sh": "bash",
        ".bash": "bash",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".json": "json",
        ".toml": "toml",
        ".md": "markdown",
        ".sql": "sql",
        ".css": "css",
        ".html": "html",
    }.get(suffix, "text")


def summary_pr_line(summary: str) -> str:
    for line in summary.splitlines():
        stripped = line.strip()
        if stripped.startswith("**PR:**"):
            return stripped.removeprefix("**PR:**").strip()
    return "Review completed."


def verdict_line(highest: str, count: int, event: str = "REQUEST_CHANGES") -> str:
    noun = "finding" if count == 1 else "findings"
    if highest in {"P0", "P1", "P2"} and event == "REQUEST_CHANGES":
        return f"Request changes - {count} actionable {noun}, highest severity {highest}."
    if highest in {"P0", "P1", "P2"}:
        return f"Comment - {count} actionable {noun}, highest severity {highest}."
    return f"Comment - {count} low-priority {noun}."


def _finding_parts(body: str) -> tuple[str, str]:
    cleaned = strip_severity_prefix(body)
    cleaned = _SUGGESTION_RE.sub("", cleaned).strip()
    cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    title, details = split_title_and_details(cleaned)
    return title, details or cleaned
