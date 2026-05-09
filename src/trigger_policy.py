from collections.abc import Iterable, Mapping
from typing import Any


def comment_author_is_allowed(author_association: str | None, allowed: Iterable[str]) -> bool:
    allowed_set = {item.upper() for item in allowed}
    if not allowed_set:
        return False
    return (author_association or "").upper() in allowed_set


def issue_comment_targets_pr(payload: Mapping[str, Any]) -> bool:
    return "pull_request" in (payload.get("issue") or {})


def comment_author_association(payload: Mapping[str, Any]) -> str:
    comment = payload.get("comment") or {}
    return str(comment.get("author_association") or payload.get("author_association") or "")


def issue_comment_can_trigger_review(payload: Mapping[str, Any], allowed: Iterable[str]) -> bool:
    return issue_comment_targets_pr(payload) and comment_author_is_allowed(comment_author_association(payload), allowed)


def review_comment_can_trigger_reply(payload: Mapping[str, Any], allowed: Iterable[str]) -> bool:
    return comment_author_is_allowed(comment_author_association(payload), allowed)
