"""GitHub Action entrypoint.

Reads the event payload from GITHUB_EVENT_PATH, extracts repo + PR number,
and runs the review pipeline using GITHUB_TOKEN (user mode).

Triggered by:
  - pull_request: opened, synchronize → auto-review
  - issue_comment: created with @ds-review → on-demand review
"""

import asyncio
import json
import logging
import os
import sys
from typing import NoReturn

from src.config import settings
from src.logging_config import configure_logging
from src.pipeline import run_review_pipeline
from src.trigger_policy import issue_comment_can_trigger_review

configure_logging(action_mode=True)
logger = logging.getLogger("ds-review.action")


def fail(msg: str) -> NoReturn:
    logger.error(msg)
    sys.exit(1)


async def main() -> None:
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    github_token = os.environ.get("GITHUB_TOKEN")

    if not github_token:
        fail("GITHUB_TOKEN not set — is this running inside GitHub Actions?")
    if not settings.deepseek_api_key:
        fail("DEEPSEEK_API_KEY not set in repository secrets.")

    settings.github_token = github_token
    settings.deployment_type = "user"

    if not event_path:
        fail("GITHUB_EVENT_PATH not set.")
    try:
        with open(event_path) as f:
            payload = json.load(f)
    except Exception as e:
        fail(f"Failed to read event payload: {e}")

    repo = payload.get("repository", {}).get("full_name", "")
    if not repo:
        fail("No repository in event payload.")
    pr_number = payload.get("pull_request", {}).get("number") or payload.get("issue", {}).get("number")
    if not pr_number:
        fail("No pull_request/issue number in event payload.")

    action = payload.get("action", "")

    if event_name == "pull_request":
        if action == "opened" and settings.trigger_on_pull_request_open:
            logger.info(f"Auto-review: {repo}#{pr_number} ({action})")
            await run_review_pipeline(repo, pr_number)
        elif action == "synchronize" and settings.trigger_on_pull_request_sync:
            logger.info(f"Auto-review: {repo}#{pr_number} ({action})")
            await run_review_pipeline(repo, pr_number)
        elif action == "reopened" and settings.trigger_on_pull_request_reopen:
            logger.info(f"Auto-review: {repo}#{pr_number} ({action})")
            await run_review_pipeline(repo, pr_number)
        else:
            logger.info(f"Ignoring event: {event_name}/{action}")

    elif event_name == "issue_comment" and action == "created":
        comment_body = payload.get("comment", {}).get("body", "")
        if not settings.trigger_on_mention:
            logger.info(f"Ignoring comment trigger on {repo}#{pr_number}: mentions disabled")
        elif not issue_comment_can_trigger_review(payload, settings.mention_author_associations):
            logger.info(f"Ignoring comment trigger on {repo}#{pr_number}: untrusted or non-PR comment")
        elif "@ds-review" in comment_body:
            logger.info(f"On-demand review: {repo}#{pr_number}")
            await run_review_pipeline(repo, pr_number)
        else:
            logger.info(f"Ignoring comment without @ds-review on {repo}#{pr_number}")

    else:
        logger.info(f"Ignoring event: {event_name}/{action}")


if __name__ == "__main__":
    asyncio.run(main())
