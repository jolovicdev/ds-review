from src.trigger_policy import (
    comment_author_is_allowed,
    issue_comment_can_trigger_review,
    review_comment_can_trigger_reply,
)

ALLOWED = ["OWNER", "MEMBER", "COLLABORATOR"]


def test_issue_comment_requires_pr_and_trusted_author():
    payload = {
        "issue": {"number": 7, "pull_request": {"url": "https://api.github.com/prs/7"}},
        "comment": {"author_association": "COLLABORATOR"},
    }

    assert issue_comment_can_trigger_review(payload, ALLOWED)


def test_issue_comment_blocks_regular_issues_and_untrusted_authors():
    assert not issue_comment_can_trigger_review(
        {"issue": {"number": 7}, "comment": {"author_association": "OWNER"}},
        ALLOWED,
    )
    assert not issue_comment_can_trigger_review(
        {
            "issue": {"number": 7, "pull_request": {"url": "https://api.github.com/prs/7"}},
            "comment": {"author_association": "NONE"},
        },
        ALLOWED,
    )


def test_review_comment_replies_require_trusted_author():
    assert review_comment_can_trigger_reply({"comment": {"author_association": "MEMBER"}}, ALLOWED)
    assert not review_comment_can_trigger_reply({"comment": {"author_association": "FIRST_TIMER"}}, ALLOWED)


def test_empty_allowed_association_list_blocks_comment_triggers():
    assert not comment_author_is_allowed("OWNER", [])
