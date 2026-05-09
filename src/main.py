import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from gidgethub import sansio

from src.client_factory import make_github_client
from src.config import settings
from src.persistent_state import clear_pending, get_pending_reviews, save_pending_review
from src.pipeline import _is_recheck_request, handle_comment_reply, run_review_pipeline
from src.review_queue import IsCurrent, ReviewCoordinator
from src.trigger_policy import issue_comment_can_trigger_review, review_comment_can_trigger_reply

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ds-review")


async def replay_pending_reviews():
    pending = get_pending_reviews()
    if pending:
        logger.info(f"Replaying {len(pending)} pending reviews from previous run")
        for p in pending:
            await review_coordinator.enqueue(p["repo"], p["pr_number"], p.get("installation_id", 0))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await replay_pending_reviews()
    yield


app = FastAPI(title="DS-Review", version="0.1.0", lifespan=lifespan)


def verify_hmac(body: bytes, signature_header: str | None) -> bool:
    if signature_header is None:
        return False
    parts = signature_header.split("=")
    if len(parts) != 2 or parts[0] != "sha256":
        return False
    expected = parts[1]
    computed = hmac.new(
        settings.github_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, expected)


async def _comment_mentions_bot(comment_body: str, installation_id: int) -> bool:
    if "@ds-review" in comment_body:
        return True

    client = make_github_client()
    try:
        token = await client.get_token(installation_id)
        bot_login = await client.get_bot_username(token)
    except Exception:
        logger.warning("Failed to resolve GitHub App bot username for mention matching")
        return False
    finally:
        await client.close()

    if not bot_login:
        return False
    app_slug = bot_login.removesuffix("[bot]")
    return f"@{app_slug}" in comment_body or f"@{bot_login}" in comment_body


async def _dispatch_review(repo: str, pr_number: int, installation_id: int, is_current: IsCurrent) -> None:
    reaction_client = make_github_client()
    token = ""
    eyes_reaction_id: int | None = None
    try:
        token = await reaction_client.get_token(installation_id)
        eyes_reaction_id = await reaction_client.add_issue_reaction(repo, pr_number, "eyes", token)
        logger.info("Added eyes reaction repo=%s pr=%d reaction_id=%s", repo, pr_number, eyes_reaction_id)
    except Exception:
        logger.warning(f"Could not add eyes reaction to {repo}#{pr_number}", exc_info=True)

    try:
        result = await run_review_pipeline(repo, pr_number, installation_id, is_current=is_current)
        if result and result.get("stale") and result.get("reason") == "head_changed" and is_current():
            logger.info(
                "Requeueing review for latest head repo=%s pr=%d reviewed_head=%s current_head=%s",
                repo,
                pr_number,
                str(result.get("reviewed_head", ""))[:12],
                str(result.get("current_head", ""))[:12],
            )
            save_pending_review(repo, pr_number, installation_id)
            await review_coordinator.enqueue(repo, pr_number, installation_id)
    except Exception:
        logger.exception(f"Pipeline failed for {repo}#{pr_number}")
    finally:
        if token and eyes_reaction_id:
            try:
                await reaction_client.delete_issue_reaction(repo, pr_number, eyes_reaction_id, token)
                logger.info("Removed eyes reaction repo=%s pr=%d reaction_id=%s", repo, pr_number, eyes_reaction_id)
            except Exception:
                logger.warning("Could not remove eyes reaction repo=%s pr=%d", repo, pr_number, exc_info=True)
        await reaction_client.close()


review_coordinator = ReviewCoordinator(_dispatch_review, on_idle=clear_pending)


async def _dispatch_conversation(
    repo: str, pr_number: int, installation_id: int, parent_id: int, reply_id: int, body: str, path: str, line: int
) -> None:
    reaction_client = make_github_client()
    token = ""
    eyes_reaction_id: int | None = None
    is_recheck = _is_recheck_request(body)
    try:
        if is_recheck:
            try:
                token = await reaction_client.get_token(installation_id)
                eyes_reaction_id = await reaction_client.add_review_comment_reaction(repo, reply_id, "eyes", token)
                logger.info(
                    "Added eyes reaction repo=%s pr=%d reply_id=%d reaction_id=%s",
                    repo,
                    pr_number,
                    reply_id,
                    eyes_reaction_id,
                )
            except Exception:
                logger.warning(f"Could not add eyes reaction to recheck reply {reply_id}", exc_info=True)
        result = await handle_comment_reply(repo, pr_number, installation_id, parent_id, reply_id, body, path, line)
        if is_recheck:
            if token and eyes_reaction_id:
                try:
                    await reaction_client.delete_review_comment_reaction(repo, reply_id, eyes_reaction_id, token)
                    logger.info(
                        "Removed eyes reaction repo=%s pr=%d reply_id=%d reaction_id=%s",
                        repo,
                        pr_number,
                        reply_id,
                        eyes_reaction_id,
                    )
                except Exception:
                    logger.warning("Could not remove eyes reaction from recheck reply %d", reply_id, exc_info=True)
            if result.get("resolved"):
                try:
                    if not token:
                        token = await reaction_client.get_token(installation_id)
                    await reaction_client.add_review_comment_reaction(repo, reply_id, "+1", token)
                    await reaction_client.add_issue_reaction(repo, pr_number, "+1", token)
                    resolved = await reaction_client.resolve_review_thread(repo, pr_number, parent_id, token)
                    logger.info(
                        "Recheck resolved repo=%s pr=%d parent_comment_id=%d reply_id=%d thread_resolved=%s",
                        repo,
                        pr_number,
                        parent_id,
                        reply_id,
                        resolved,
                    )
                except Exception:
                    logger.warning("Could not finalize resolved recheck reply %d", reply_id, exc_info=True)
    except Exception:
        logger.exception(f"Conversation reply failed for {repo}#{pr_number}")
        if token and eyes_reaction_id:
            try:
                await reaction_client.delete_review_comment_reaction(repo, reply_id, eyes_reaction_id, token)
            except Exception:
                logger.warning("Could not remove eyes reaction after failed recheck reply %d", reply_id, exc_info=True)
    finally:
        await reaction_client.close()


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256")

    if not verify_hmac(body, sig):
        logger.warning("HMAC validation failed")
        return Response(status_code=401)

    event = sansio.Event.from_http(
        request.headers,
        body,
        secret=settings.github_webhook_secret,
    )

    logger.debug(f"Event: {event.event} {event.data.get('action', '')}")

    payload = json.loads(body)
    repo = payload.get("repository", {}).get("full_name", "")
    inst_id = payload.get("installation", {}).get("id", 0)

    # PR opened or synchronized → auto-review
    if event.event == "pull_request":
        action = payload.get("action", "")
        should_review = (
            (action == "opened" and settings.trigger_on_pull_request_open)
            or (action == "synchronize" and settings.trigger_on_pull_request_sync)
            or (action == "reopened" and settings.trigger_on_pull_request_reopen)
        )
        if should_review:
            pr = payload.get("pull_request", {}).get("number", 0)
            logger.info(f"PR: {repo}#{pr} ({action})")
            save_pending_review(repo, pr, inst_id)
            generation = await review_coordinator.enqueue(repo, pr, inst_id)
            return {"status": "accepted", "repo": repo, "pr": pr, "generation": generation}

    # @ds-review mention on a PR → on-demand review
    if event.event == "issue_comment":
        action = payload.get("action", "")
        comment_body = payload.get("comment", {}).get("body", "")
        if action == "created" and settings.trigger_on_mention:
            if not issue_comment_can_trigger_review(payload, settings.mention_author_associations):
                return {"status": "ignored", "reason": "untrusted or non-PR comment"}
            if not await _comment_mentions_bot(comment_body, inst_id):
                return {"status": "ignored", "reason": "no mention"}
            pr = payload.get("issue", {}).get("number", 0)
            logger.info(f"Mention: {repo}#{pr}")
            save_pending_review(repo, pr, inst_id)
            generation = await review_coordinator.enqueue(repo, pr, inst_id)
            return {"status": "accepted", "repo": repo, "pr": pr, "generation": generation}

    # Reply to bot's review comment → conversation
    if event.event == "pull_request_review_comment":
        action = payload.get("action", "")
        if action == "created":
            sender = payload.get("sender", {})
            if sender.get("type") == "Bot":
                return {"status": "ignored", "reason": "bot reply"}
            if not review_comment_can_trigger_reply(payload, settings.mention_author_associations):
                return {"status": "ignored", "reason": "untrusted comment author"}
            comment = payload.get("comment", {})
            in_reply_to = comment.get("in_reply_to_id")
            if not in_reply_to:
                return {"status": "ignored", "reason": "not a reply"}
            pr = payload.get("pull_request", {}).get("number", 0)
            reply_id = comment.get("id", 0)
            reply_body = comment.get("body", "")
            path = comment.get("path", "")
            line = comment.get("line") or comment.get("original_line") or 1
            logger.info(f"Conversation: {repo}#{pr} reply to {in_reply_to}")
            asyncio.create_task(
                _dispatch_conversation(repo, pr, inst_id, in_reply_to, reply_id, reply_body, path, line)
            )
            return {"status": "accepted", "repo": repo, "pr": pr, "mode": "conversation"}

    return {"status": "ignored", "reason": f"{event.event}"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level.lower(),
    )
