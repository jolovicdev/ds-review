import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from blackgeorge import Desk, Job
from blackgeorge.workflow import Step
from pydantic import TypeAdapter

from src.client_factory import make_github_client
from src.config import settings
from src.github_client import GitHubClient
from src.models import (
    ConversationReply,
    HypothesesOutput,
    ReviewCommentList,
    ReviewOutput,
    ReviewRecheckOutput,
)
from src.persistent_state import get_last_commit, get_review_id, save_review
from src.review_markdown import build_review_summary, normalize_inline_comment, severity_marker
from src.tools import make_tools
from src.workers import (
    build_context_collector,
    build_conversation_responder,
    build_evaluator,
    build_hypothesis_generator,
    build_recheck_responder,
    build_reflector,
    build_specialist_workforce,
    build_summarizer,
)

logger = logging.getLogger("ds-review")

DS_REVIEW_MARKER = "\n\n<!-- ds-review -->"
INLINE_MARKER = "<!-- ds-review-inline -->"
ACTIONABLE_MARKERS = {"P0", "P1", "P2"}
SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
IsCurrent = Callable[[], bool]


@dataclass(frozen=True)
class ReviewHeadStatus:
    current: bool
    reason: str = ""
    latest_head_sha: str = ""


def _schema(model: type) -> TypeAdapter:
    return TypeAdapter(model)


def _format_flow_errors(errors) -> str:
    if not errors:
        return "(none)"
    if isinstance(errors, str):
        return errors
    return "; ".join(str(error) for error in errors)


async def _discover_existing_reviews(client: GitHubClient, repo: str, pr_number: int, token: str) -> list[dict]:
    """Return DS-Review reviews on this PR by inspecting author + body markers."""
    try:
        reviews = await client.list_pr_reviews(repo, pr_number, token)
    except Exception:
        logger.warning("Failed to list PR reviews")
        return []

    bot_login = None
    try:
        bot_login = await client.get_bot_username(token)
    except Exception:
        pass

    our_reviews = []
    for r in reviews:
        body = r.get("body", "")
        user = r.get("user", {})
        login = user.get("login", "")
        if bot_login and login == bot_login:
            our_reviews.append(r)
        elif DS_REVIEW_MARKER.strip() in body or INLINE_MARKER in body or "DS-Review" in body:
            our_reviews.append(r)
    return our_reviews


async def _get_our_review_comments(
    client: GitHubClient, repo: str, pr_number: int, our_review_ids: set[int], token: str
) -> list[dict]:
    """Return review comments that belong to our reviews."""
    try:
        all_comments = await client.list_review_comments(repo, pr_number, token)
    except Exception:
        logger.warning("Failed to list review comments")
        return []
    return [c for c in all_comments if c.get("pull_request_review_id") in our_review_ids]


def _write_github_output(review_output: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a") as f:
            summary = review_output.get("summary", "")
            f.write(f"review_summary<<EOF\n{summary}\nEOF\n")
            comments = review_output.get("comments", [])
            f.write(f"review_comments_count={len(comments)}\n")
            f.write(f"review_json={json.dumps(review_output)}\n")
    except Exception as e:
        logger.warning(f"Failed to write GITHUB_OUTPUT: {e}")


def _extract_review_output_from_flow(report) -> dict | None:
    data = report.data
    if data is None:
        return None
    if isinstance(data, list):
        for item in reversed(data):
            inner = item.get("data", item) if isinstance(item, dict) else item
            if hasattr(inner, "model_dump"):
                dumped = inner.model_dump()
                if isinstance(dumped, dict) and "summary" in dumped:
                    return dumped
            elif isinstance(inner, dict) and "summary" in inner:
                return inner
        return None
    if hasattr(data, "model_dump"):
        return data.model_dump()
    if isinstance(data, dict) and "summary" in data:
        return data
    return None


async def _review_head_status(
    client: GitHubClient,
    repo_full_name: str,
    pr_number: int,
    token: str,
    reviewed_head_sha: str,
    is_current: IsCurrent | None = None,
) -> ReviewHeadStatus:
    if is_current is not None and not is_current():
        logger.info(
            "Review superseded before publish repo=%s pr=%d head=%s",
            repo_full_name,
            pr_number,
            reviewed_head_sha[:12],
        )
        return ReviewHeadStatus(False, reason="superseded", latest_head_sha=reviewed_head_sha)

    latest = await client.get_pr_details(repo_full_name, pr_number, token)
    latest_head_sha = latest.get("head_sha", "")
    if latest_head_sha != reviewed_head_sha:
        logger.info(
            "Review head changed before publish repo=%s pr=%d reviewed_head=%s current_head=%s",
            repo_full_name,
            pr_number,
            reviewed_head_sha[:12],
            latest_head_sha[:12],
        )
        return ReviewHeadStatus(False, reason="head_changed", latest_head_sha=latest_head_sha)

    if is_current is not None and not is_current():
        logger.info(
            "Review superseded during publish guard repo=%s pr=%d head=%s",
            repo_full_name,
            pr_number,
            reviewed_head_sha[:12],
        )
        return ReviewHeadStatus(False, reason="superseded", latest_head_sha=latest_head_sha)

    return ReviewHeadStatus(True, latest_head_sha=latest_head_sha)


async def _review_head_is_current(
    client: GitHubClient,
    repo_full_name: str,
    pr_number: int,
    token: str,
    reviewed_head_sha: str,
    is_current: IsCurrent | None = None,
) -> bool:
    return (
        await _review_head_status(client, repo_full_name, pr_number, token, reviewed_head_sha, is_current)
    ).current


async def run_review_pipeline(
    repo_full_name: str,
    pr_number: int,
    installation_id: int = 0,
    *,
    is_current: IsCurrent | None = None,
) -> dict | None:
    review_started = time.perf_counter()
    client = make_github_client()
    token = await client.get_token(installation_id)
    logger.info(
        "Review started repo=%s pr=%d deployment=%s",
        repo_full_name,
        pr_number,
        settings.deployment_type,
    )

    # Incremental check BEFORE running expensive LLM flow
    pr_data = await client.get_pr_details(repo_full_name, pr_number, token)
    head_sha = pr_data.get("head_sha", "")
    logger.info(
        "Review head repo=%s pr=%d head=%s files=%d diff_chars=%d",
        repo_full_name,
        pr_number,
        head_sha[:12],
        len(pr_data.get("files", [])),
        len(pr_data.get("diff", "")),
    )

    is_stateless = settings.deployment_type == "user"

    if not is_stateless:
        last_sha = get_last_commit(repo_full_name, pr_number)
        if last_sha and last_sha == head_sha:
            logger.info(f"Skipping {repo_full_name}#{pr_number} — no new commits")
            await client.close()
            return None
        if last_sha and head_sha:
            new_commits = await client.get_commits_between(repo_full_name, last_sha, head_sha, token)
            logger.info(f"Incremental: {len(new_commits)} new commits for {repo_full_name}#{pr_number}")

    tools = make_tools(client, token, pr_details=pr_data)
    context_tools = tools[:6]

    context_collector = build_context_collector(context_tools)
    hypothesis_generator = build_hypothesis_generator()
    evaluator = build_evaluator()
    specialist_workforce = build_specialist_workforce()
    summarizer = build_summarizer()
    reflector = build_reflector()

    desk = Desk(
        model=settings.fast_model,
        temperature=settings.temperature,
        max_iterations=25,
        max_tool_calls=30,
        respect_context_window=True,
        max_context_messages=40,
    )

    pro_thinking = {"type": "enabled"}
    pro_extra = {"reasoning_effort": "high"}

    def context_job(ctx):
        return Job(input={"repo_full_name": repo_full_name, "pr_number": pr_number})

    def hypotheses_job(ctx):
        context_report = ctx.outputs[0]
        context_data = _extract_context(context_report)
        return Job(
            input=context_data,
            response_schema=_schema(HypothesesOutput),
            thinking=pro_thinking,
            extra_body=pro_extra,
        )

    def evaluator_job(ctx):
        context_data = _extract_context(ctx.outputs[0])
        hypotheses = ctx.outputs[1].data
        if hasattr(hypotheses, "hypotheses"):
            hypotheses = [h.model_dump() for h in hypotheses.hypotheses]
        return Job(
            input={"full_context": context_data, "hypotheses": hypotheses},
            response_schema=_schema(ReviewCommentList),
            thinking=pro_thinking,
            extra_body=pro_extra,
        )

    def specialist_job(ctx):
        context_data = _extract_context(ctx.outputs[0])
        existing = _get_data_as_list(ctx.outputs[2])
        return Job(
            input={"full_context": context_data, "existing_comments": existing},
            response_schema=_schema(ReviewCommentList),
            thinking=pro_thinking,
            extra_body=pro_extra,
        )

    def summarizer_job(ctx):
        context_data = _extract_context(ctx.outputs[0])
        evaluator_comments = _get_data_as_list(ctx.outputs[2])
        specialist_comments = _get_data_as_list(ctx.outputs[3])
        all_comments = evaluator_comments + specialist_comments
        return Job(
            input={"all_comments": all_comments, "full_context": context_data},
            response_schema=_schema(ReviewOutput),
        )

    def reflector_job(ctx):
        context_data = _extract_context(ctx.outputs[0])
        review = _extract_review_output_from_flow(ctx.outputs[4])
        if review is None:
            return Job(
                input={"review_summary": "", "review_comments": [], "pr_context": context_data},
                response_schema=_schema(ReviewOutput),
            )
        return Job(
            input={
                "review_summary": review.get("summary", ""),
                "review_comments": review.get("comments", []),
                "pr_context": context_data,
            },
            response_schema=_schema(ReviewOutput),
        )

    flow = desk.flow(
        [
            Step(context_collector, name="collect_context", job_builder=context_job),
            Step(hypothesis_generator, name="generate_hypotheses", job_builder=hypotheses_job),
            Step(evaluator, name="evaluate_hypotheses", job_builder=evaluator_job),
            Step(specialist_workforce, name="specialist_review", job_builder=specialist_job),
            Step(summarizer, name="summarize_review", job_builder=summarizer_job),
            Step(reflector, name="self_reflection", job_builder=reflector_job),
        ]
    )

    initial_job = Job(
        input={
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
        }
    )

    try:
        flow_started = time.perf_counter()
        logger.info("Flow started repo=%s pr=%d head=%s", repo_full_name, pr_number, head_sha[:12])
        try:
            report = await flow.arun(initial_job)
        finally:
            desk.close()
        logger.info(
            "Flow completed repo=%s pr=%d status=%s duration_ms=%d",
            repo_full_name,
            pr_number,
            report.status,
            int((time.perf_counter() - flow_started) * 1000),
        )
        if report.status == "failed":
            logger.error(
                "Flow failed repo=%s pr=%d errors=%s",
                repo_full_name,
                pr_number,
                _format_flow_errors(getattr(report, "errors", [])),
            )
            head_status = await _review_head_status(client, repo_full_name, pr_number, token, head_sha, is_current)
            if not head_status.current:
                await client.close()
                return {
                    "stale": True,
                    "reason": head_status.reason,
                    "reviewed_head": head_sha,
                    "current_head": head_status.latest_head_sha,
                    "summary": "",
                    "comments": [],
                }
            await _post_failure_comment(client, token, repo_full_name, pr_number)
            await client.close()
            return None

        review_output = _extract_review_output_from_flow(report)
        if review_output is None:
            logger.error("No ReviewOutput in flow report")
            await client.close()
            return None

        summary = review_output.get("summary", "")
        comments = review_output.get("comments", [])
        clean = [
            {
                "path": c["path"],
                "line": c.get("line", 1),
                "start_line": c.get("start_line"),
                "side": c.get("side", "RIGHT"),
                "title": c.get("title", ""),
                "details": c.get("details", ""),
                "suggestion": c.get("suggestion"),
                "body": c.get("body", ""),
                "severity": c.get("severity", "medium"),
                "category": c.get("category", "general"),
            }
            for c in comments
        ]

        diff = pr_data.get("diff", "")

        review_comments, summary_comments, unanchored_comments, dropped_comments = _split_review_comments(clean, diff)
        if not settings.inline_comments_enabled:
            review_comments = []
        for c in unanchored_comments:
            logger.warning(f"Keeping unanchored finding in summary for {c['path']}:{c['line']} - not in diff")
        for c in dropped_comments:
            logger.warning(f"Dropping finding for {c['path']}:{c['line']} - not publishable")

        published_comments = summary_comments + unanchored_comments
        review_event = _review_event(published_comments)

        own_pr = False
        try:
            bot_login = await client.get_bot_username(token)
        except Exception:
            bot_login = ""
            logger.warning("Failed to resolve bot username for own-PR check")
        author_login = pr_data.get("author_login", "")
        own_pr = bool(bot_login) and bot_login == author_login
        if own_pr and review_event == "REQUEST_CHANGES":
            logger.info("Downgrading review event to COMMENT: token user authored %s#%d", repo_full_name, pr_number)
        review_event = _effective_review_event(review_event, bot_login, author_login)

        review_body = build_review_summary(
            generated_summary=summary,
            comments=summary_comments,
            unanchored_comments=unanchored_comments,
            pr_title=pr_data.get("title", ""),
            event=review_event,
        )
        if not settings.summary_comment_enabled:
            review_body = "DS-Review completed."
        review_body += DS_REVIEW_MARKER

        head_status = await _review_head_status(client, repo_full_name, pr_number, token, head_sha, is_current)
        if not head_status.current:
            await client.close()
            return {
                "stale": True,
                "reason": head_status.reason,
                "reviewed_head": head_sha,
                "current_head": head_status.latest_head_sha,
                "summary": summary,
                "comments": clean,
            }

        if is_stateless:
            # Stateless: discover previous reviews from GitHub
            our_reviews = await _discover_existing_reviews(client, repo_full_name, pr_number, token)
            our_review_ids = {r["id"] for r in our_reviews}
            existing_summary_review = None
            for r in our_reviews:
                body = r.get("body", "")
                if DS_REVIEW_MARKER.strip() in body or ("DS-Review" in body and INLINE_MARKER not in body):
                    existing_summary_review = r
                    break

            existing_state = existing_summary_review.get("state", "") if existing_summary_review else ""
            should_replace_review_state = (
                (review_event == "REQUEST_CHANGES" and existing_state != "CHANGES_REQUESTED")
                or (review_event == "COMMENT" and existing_state == "CHANGES_REQUESTED")
            )

            if existing_summary_review and not should_replace_review_state:
                existing_review_id = existing_summary_review["id"]
                await client.update_review(repo_full_name, pr_number, existing_review_id, review_body, token)
                logger.info(f"Updated review {existing_review_id}")
            else:
                resp = await client.post_review_with_fallback(
                    repo_full_name, pr_number, review_body, review_comments, token, event=review_event
                )
                existing_review_id = resp.get("id", 0)
                logger.info(
                    "Posted new review repo=%s pr=%d review_id=%s event=%s inline_comments=%d",
                    repo_full_name,
                    pr_number,
                    existing_review_id,
                    review_event,
                    len(review_comments),
                )

            # For inline comments: only post genuinely new ones to avoid duplicates
            if review_comments and existing_summary_review and not should_replace_review_state:
                existing_comments = await _get_our_review_comments(
                    client, repo_full_name, pr_number, our_review_ids, token
                )
                existing_keys = {(c["path"], c["body"]) for c in existing_comments}
                new_comments = [c for c in review_comments if (c["path"], c["body"]) not in existing_keys]
                if new_comments:
                    await client.post_review_with_fallback(
                        repo_full_name, pr_number, INLINE_MARKER, new_comments, token
                    )
                    logger.info(f"Posted {len(new_comments)} new inline comments")

        else:
            # Stateful: use .blackgeorge
            existing_review_id = get_review_id(repo_full_name, pr_number)
            existing_state = ""
            if existing_review_id:
                try:
                    reviews = await client.list_pr_reviews(repo_full_name, pr_number, token)
                    existing_state = next(
                        (r.get("state", "") for r in reviews if r.get("id") == existing_review_id),
                        "",
                    )
                except Exception:
                    logger.warning("Failed to resolve existing review state")
            should_replace_review_state = (
                (review_event == "REQUEST_CHANGES" and existing_state != "CHANGES_REQUESTED")
                or (review_event == "COMMENT" and existing_state == "CHANGES_REQUESTED")
            )
            should_update_existing = existing_review_id and not should_replace_review_state
            if should_update_existing:
                await client.update_review(repo_full_name, pr_number, existing_review_id, review_body, token)
                logger.info(f"Updated review {existing_review_id}")
            else:
                resp = await client.post_review_with_fallback(
                    repo_full_name, pr_number, review_body, review_comments, token, event=review_event
                )
                existing_review_id = resp.get("id", 0)
                logger.info(
                    "Posted new review repo=%s pr=%d review_id=%s event=%s inline_comments=%d",
                    repo_full_name,
                    pr_number,
                    existing_review_id,
                    review_event,
                    len(review_comments),
                )

            # Avoid duplicate inline comments: only post if no existing review
            if review_comments and not existing_review_id:
                await client.post_review_with_fallback(repo_full_name, pr_number, INLINE_MARKER, review_comments, token)
                logger.info(f"Posted {len(review_comments)} inline comments")

            if existing_review_id and head_sha:
                save_review(repo_full_name, pr_number, existing_review_id, head_sha)

        # Check run — best-effort
        if head_sha:
            markers = [_comment_marker(c) for c in published_comments]
            has_critical = "P0" in markers
            has_high = "P1" in markers
            if settings.check_runs_enabled:
                try:
                    conclusion = _check_run_conclusion(published_comments, settings.fail_check_on)
                    await client.create_check_run(repo_full_name, pr_number, head_sha, review_body, conclusion, token)
                    logger.info(f"Check run: {conclusion}")
                except Exception as e:
                    logger.warning(f"Check run failed (non-fatal): {e}")

            if not published_comments:
                try:
                    await client.add_issue_reaction(repo_full_name, pr_number, "+1", token)
                    logger.info("Added clean-review +1 reaction")
                except Exception as e:
                    logger.warning(f"Clean-review reaction failed (non-fatal): {e}")

            should_approve = settings.auto_approve_enabled and not own_pr
            if should_approve and settings.auto_approve_no_critical and has_critical:
                should_approve = False
            if should_approve and settings.auto_approve_no_high and has_high:
                should_approve = False

            if should_approve:
                try:
                    await client.approve_pr(repo_full_name, pr_number, token)
                    logger.info("Auto-approved")
                except Exception as e:
                    logger.warning(f"Auto-approve failed (non-fatal): {e}")

        _write_github_output(review_output)
        logger.info(
            "Review finished repo=%s pr=%d findings=%d inline_comments=%d duration_ms=%d",
            repo_full_name,
            pr_number,
            len(published_comments),
            len(review_comments),
            int((time.perf_counter() - review_started) * 1000),
        )
        await client.close()
        return review_output

    except Exception as e:
        logger.exception(f"Pipeline crashed for {repo_full_name}#{pr_number}: {e}")
        await _post_failure_comment(client, token, repo_full_name, pr_number)
        await client.close()
        return None


def _extract_context(report) -> dict:
    data: dict = {"tool_results": []}
    for tc in report.tool_calls:
        if tc.result is None:
            continue
        if isinstance(tc.result, dict):
            result_data = tc.result.get("data")
            result_content = tc.result.get("content")
        else:
            result_data = getattr(tc.result, "data", None)
            result_content = getattr(tc.result, "content", None)
        if result_data is not None:
            content = result_data
        elif result_content is not None:
            content = result_content
        else:
            content = str(tc.result)
        item = {"tool": tc.name, "arguments": tc.arguments, "content": content}
        data["tool_results"].append(item)
        _append_tool_result(data, tc.name, content)
        context_key = _tool_context_key(tc.name, tc.arguments)
        if context_key != tc.name:
            data[context_key] = content

    pr_details = _first_tool_result(data.get("fetch_pr_details"))
    if isinstance(pr_details, str):
        try:
            parsed = json.loads(pr_details)
        except json.JSONDecodeError:
            parsed = None
        pr_details = parsed
    if isinstance(pr_details, dict):
        diff = pr_details.get("diff")
        if isinstance(diff, str):
            changed_lines = _parse_diff_changed_lines(diff)
            data["review_scope"] = {
                "changed_files": sorted(changed_lines),
                "right_lines": {path: sorted(sides["RIGHT"]) for path, sides in changed_lines.items()},
                "left_lines": {path: sorted(sides["LEFT"]) for path, sides in changed_lines.items()},
            }

    if not data["tool_results"]:
        data["raw_output"] = report.content
    return data


def _append_tool_result(data: dict, name: str, content) -> None:
    existing = data.get(name)
    if existing is None:
        data[name] = content
    elif isinstance(existing, list):
        existing.append(content)
    else:
        data[name] = [existing, content]


def _first_tool_result(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _tool_context_key(name: str, arguments: dict) -> str:
    path = arguments.get("path") or arguments.get("target_path")
    if isinstance(path, str) and path:
        return f"{name}:{path}"
    return name


def _get_data_as_list(report) -> list:
    data = report.data
    if data is None:
        return []
    if isinstance(data, list):
        result: list = []
        for d in data:
            if hasattr(d, "model_dump"):
                dumped = d.model_dump()
                result.extend(dumped.get("comments", [dumped]) if isinstance(dumped, dict) else [dumped])
            elif isinstance(d, dict):
                result.extend(d.get("comments", [d]))
            else:
                result.append(d)
        return result
    if hasattr(data, "model_dump"):
        dumped = data.model_dump()
        return dumped.get("comments", [dumped]) if isinstance(dumped, dict) else [dumped]
    if isinstance(data, dict):
        return data.get("comments", [data])
    return []


def _comment_marker(comment: dict) -> str:
    return severity_marker(comment.get("severity"), comment.get("body", ""))


def _allowed_markers(min_severity: str | None = None) -> set[str]:
    marker = (min_severity or settings.min_severity).upper()
    max_rank = SEVERITY_RANK.get(marker, SEVERITY_RANK["P2"])
    return {name for name, rank in SEVERITY_RANK.items() if rank <= max_rank}


def _is_actionable_comment(comment: dict) -> bool:
    marker = _comment_marker(comment)
    if marker not in ACTIONABLE_MARKERS or marker not in _allowed_markers():
        return False
    return not (settings.require_suggestion_for_p2 and marker == "P2" and not comment.get("suggestion"))


def _review_event(comments: list[dict]) -> str:
    return "REQUEST_CHANGES" if any(_is_actionable_comment(c) for c in comments) else "COMMENT"


def _effective_review_event(review_event: str, bot_login: str, author_login: str) -> str:
    """GitHub rejects REQUEST_CHANGES when the reviewing token user authored the PR."""
    if review_event == "REQUEST_CHANGES" and bot_login and bot_login == author_login:
        return "COMMENT"
    return review_event


def _check_run_conclusion(comments: list[dict], fail_on: list[str] | None = None) -> str:
    markers = {_comment_marker(c) for c in comments}
    fail_markers = {marker.upper() for marker in (fail_on or ["P0"])}
    if markers & fail_markers:
        return "failure"
    if markers & {"P1", "P2"}:
        return "neutral"
    return "success"


def _severity_rank(comment: dict) -> int:
    return SEVERITY_RANK.get(_comment_marker(comment), 2)


def _merge_comment_details(primary: dict, secondary: dict) -> str:
    primary_details = (primary.get("details") or primary.get("body") or "").strip()
    secondary_title = (secondary.get("title") or "").strip()
    secondary_details = (secondary.get("details") or secondary.get("body") or "").strip()
    if not secondary_details or secondary_details in primary_details:
        return primary_details
    if secondary_title and secondary_title not in secondary_details:
        secondary_details = f"{secondary_title}: {secondary_details}"
    if not primary_details:
        return secondary_details
    return f"{primary_details}\n\nAlso: {secondary_details}"


def _merge_summary_comments(existing: dict, incoming: dict) -> dict:
    primary, secondary = (
        (incoming, existing) if _severity_rank(incoming) < _severity_rank(existing) else (existing, incoming)
    )
    merged = {**primary}
    merged["details"] = _merge_comment_details(primary, secondary)
    if not merged.get("suggestion") and secondary.get("suggestion"):
        merged["suggestion"] = secondary["suggestion"]
    start_lines: list[int] = []
    for comment in (existing, incoming):
        start_line = comment.get("start_line")
        if isinstance(start_line, int):
            start_lines.append(start_line)
    if start_lines:
        merged["start_line"] = min(start_lines)
    return merged


def _clean_diff_path(path: str) -> str:
    path = path.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return ""
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _parse_hunk_start(header: str) -> tuple[int, int] | None:
    match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", header)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _parse_diff_changed_lines(diff: str) -> dict[str, dict[str, dict[int, str]]]:
    """Return changed line numbers grouped by GitHub review side."""
    lines_by_file: dict[str, dict[str, dict[int, str]]] = {}
    current_file = ""
    old_path = ""
    old_line = 0
    new_line = 0
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            current_file = ""
            old_path = ""
            old_line = 0
            new_line = 0
        elif line.startswith("--- "):
            old_path = _clean_diff_path(line[4:])
        elif line.startswith("+++ "):
            new_path = _clean_diff_path(line[4:])
            current_file = new_path or old_path
            if current_file:
                lines_by_file.setdefault(current_file, {"RIGHT": {}, "LEFT": {}})
        elif line.startswith("@@") and current_file:
            hunk_start = _parse_hunk_start(line)
            if hunk_start is not None:
                old_line, new_line = hunk_start
        elif current_file and (old_line > 0 or new_line > 0):
            if line.startswith("+") and not line.startswith("+++"):
                lines_by_file[current_file]["RIGHT"][new_line] = line[1:]
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                lines_by_file[current_file]["LEFT"][old_line] = line[1:]
                old_line += 1
            elif line.startswith(" "):
                old_line += 1
                new_line += 1
    return lines_by_file


def _parse_diff_added_lines(diff: str) -> dict[str, dict[int, str]]:
    """Return right-side added line numbers with their source text."""
    return {path: sides["RIGHT"] for path, sides in _parse_diff_changed_lines(diff).items()}


def _parse_diff_line_ranges(diff: str) -> dict[str, set[int]]:
    return {path: set(lines) for path, lines in _parse_diff_added_lines(diff).items()}


def _is_line_in_diff(path: str, line: int, ranges: dict[str, set[int]]) -> bool:
    return line in ranges.get(path, set())


def _nearest_added_line(
    path: str, line: int, added_lines: dict[str, dict[int, str]], max_distance: int = 6
) -> int | None:
    candidates = added_lines.get(path, {})
    if not candidates:
        return None
    nearest = min(candidates, key=lambda candidate: (abs(candidate - line), candidate))
    if abs(nearest - line) <= max_distance:
        return nearest
    return None


def _line_in_diff(path: str, line: int | None, added_lines: dict[str, dict[int, str]]) -> bool:
    return line is not None and line in added_lines.get(path, {})


def _normalize_review_side(side: str | None) -> str:
    return "LEFT" if isinstance(side, str) and side.upper() == "LEFT" else "RIGHT"


def _line_in_changed_diff(
    path: str, line: int | None, side: str, changed_lines: dict[str, dict[str, dict[int, str]]]
) -> bool:
    return line is not None and line in changed_lines.get(path, {}).get(side, {})


def _nearest_changed_line(
    path: str,
    line: int,
    side: str,
    changed_lines: dict[str, dict[str, dict[int, str]]],
    max_distance: int = 6,
) -> tuple[int, str] | None:
    sides = changed_lines.get(path)
    if not sides:
        return None
    preferred_sides = [side, "LEFT" if side == "RIGHT" else "RIGHT"]
    candidates: list[tuple[int, str]] = []
    for candidate_side in preferred_sides:
        candidates.extend((candidate, candidate_side) for candidate in sides.get(candidate_side, {}))
    if not candidates:
        return None
    nearest = min(
        candidates,
        key=lambda candidate: (
            abs(candidate[0] - line),
            0 if candidate[1] == side else 1,
            candidate[0],
            candidate[1],
        ),
    )
    if abs(nearest[0] - line) <= max_distance:
        return nearest
    return None


def _summary_comment(comment: dict, line: int, side: str) -> dict:
    summary = {**comment, "line": line, "side": side}
    start_line = summary.get("start_line")
    if not isinstance(start_line, int) or start_line >= line:
        summary.pop("start_line", None)
    return summary


def _split_review_comments(comments: list[dict], diff: str) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    changed_lines = _parse_diff_changed_lines(diff)
    anchored_comments: dict[tuple[str, int, str], dict] = {}
    anchor_texts: dict[tuple[str, int, str], str] = {}
    unanchored_comments: list[dict] = []
    dropped_comments: list[dict] = []
    for c in comments:
        path = c["path"]
        line = int(c.get("line", 1))
        side = _normalize_review_side(c.get("side"))
        if not _is_actionable_comment(c):
            dropped_comments.append(c)
            continue
        if path not in changed_lines:
            dropped_comments.append(c)
            continue
        anchor: tuple[int, str] | None
        if _line_in_changed_diff(path, line, side, changed_lines):
            anchor = (line, side)
        else:
            anchor = _nearest_changed_line(path, line, side, changed_lines)
        if anchor is not None:
            anchor_line, anchor_side = anchor
            anchor_text = changed_lines[path][anchor_side][anchor_line]
            key = (path, anchor_line, anchor_side)
            summary = _summary_comment(c, anchor_line, anchor_side)
            existing = anchored_comments.get(key)
            anchored_comments[key] = _merge_summary_comments(existing, summary) if existing else summary
            anchor_texts[key] = anchor_text
        else:
            unanchored_comments.append(c)
    summary_comments = sorted(anchored_comments.values(), key=_severity_rank)[: settings.max_findings]
    review_comments: list[dict] = []
    for c in summary_comments:
        path = c["path"]
        anchor_line = c["line"]
        anchor_side = c["side"]
        key = (path, anchor_line, anchor_side)
        body = normalize_inline_comment(
            c.get("body", ""),
            severity=c.get("severity"),
            path=path,
            anchor_text=anchor_texts[key],
            allow_suggestion=anchor_side == "RIGHT",
            title=c.get("title", ""),
            details=c.get("details", ""),
            suggestion=c.get("suggestion"),
        )
        comment = {"path": path, "line": anchor_line, "side": anchor_side, "body": body}
        start_line = c.get("start_line")
        if (
            isinstance(start_line, int)
            and start_line < anchor_line
            and _line_in_changed_diff(path, start_line, anchor_side, changed_lines)
        ):
            comment["start_line"] = start_line
            comment["start_side"] = anchor_side
        review_comments.append(comment)
    return review_comments, summary_comments, unanchored_comments, dropped_comments


async def _post_failure_comment(client, token, repo, pr_number):
    try:
        await client.post_review(
            repo,
            pr_number,
            "Automated PR review encountered an error and could not complete.",
            [],
            token,
        )
    except Exception:
        pass


def _is_recheck_request(reply_body: str) -> bool:
    return bool(re.search(r"(^|\s)(?:/)?re[- ]?check\b", reply_body.lower()))


def _reply_body_from_report(report) -> str:
    data = getattr(report, "data", None)
    body = getattr(data, "body", None)
    if isinstance(body, str) and body:
        return body
    if isinstance(data, dict) and data.get("body"):
        return str(data["body"])
    return report.content or "I've reviewed your reply."


def _recheck_resolved_from_report(report) -> bool:
    data = getattr(report, "data", None)
    resolved = getattr(data, "resolved", None)
    if isinstance(resolved, bool):
        return resolved
    if isinstance(data, dict):
        return bool(data.get("resolved"))
    return False


async def handle_comment_reply(
    repo: str,
    pr_number: int,
    installation_id: int,
    parent_comment_id: int,
    reply_comment_id: int,
    reply_body: str,
    file_path: str,
    line_number: int,
) -> dict[str, bool | str]:
    client = make_github_client()
    try:
        token = await client.get_token(installation_id)

        parent = await client.get_review_comment(repo, parent_comment_id, token)
        parent_body = parent.get("body", "") if parent else "(original comment not found)"
        if parent:
            try:
                bot_login = await client.get_bot_username(token)
                parent_login = parent.get("user", {}).get("login", "")
                if bot_login and parent_login != bot_login:
                    logger.info(
                        "Ignoring reply to non-DS-Review comment repo=%s pr=%d parent_comment_id=%d",
                        repo,
                        pr_number,
                        parent_comment_id,
                    )
                    return {"ignored": True, "resolved": False}
            except Exception:
                logger.warning("Failed to verify parent review comment author")
            file_path = parent.get("path") or file_path
            line_number = parent.get("line") or parent.get("original_line") or line_number

        try:
            pr_details = await client.get_pr_details(repo, pr_number, token)
            diff_context = pr_details.get("diff", "")[:5000]
            ref = pr_details.get("head_sha", "HEAD")
        except Exception:
            diff_context = "(unavailable)"
            ref = "HEAD"

        code_context = await client.get_file_content(repo, file_path, ref, token)
        if code_context:
            lines = code_context.split("\n")
            header = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[:30], start=0))
            ctx_start = max(0, line_number - 31)
            ctx_end = min(len(lines), line_number + 30)
            region = "\n".join(
                f"{i + 1}: {line}" for i, line in enumerate(lines[ctx_start:ctx_end], start=ctx_start)
            )
            code_context = f"--- file top ---\n{header}\n--- around line {line_number} ---\n{region}"

        is_recheck = _is_recheck_request(reply_body)
        responder = build_recheck_responder() if is_recheck else build_conversation_responder()
        response_schema = _schema(ReviewRecheckOutput if is_recheck else ConversationReply)
        desk = Desk(
            model=settings.fast_model,
            temperature=settings.temperature,
            max_iterations=5,
            max_tool_calls=0,
        )

        job = Job(
            input={
                "parent_comment": parent_body,
                "developer_reply": reply_body,
                "code_context": code_context or "(unavailable)",
                "diff_context": diff_context,
            },
            response_schema=response_schema,
        )

        try:
            try:
                report = await desk.arun(responder, job)
            finally:
                desk.close()
            response_body = _reply_body_from_report(report)
            if len(response_body) > 2000:
                response_body = response_body[:1997] + "..."
            await client.reply_to_review_comment(repo, pr_number, parent_comment_id, response_body, token)
            resolved = is_recheck and _recheck_resolved_from_report(report)
            logger.info(
                "Conversation replied repo=%s pr=%d parent_comment_id=%d recheck=%s resolved=%s",
                repo,
                pr_number,
                parent_comment_id,
                is_recheck,
                resolved,
            )
            return {"ignored": False, "resolved": resolved}
        except Exception as e:
            logger.exception(f"Failed to reply to comment: {e}")
            return {"ignored": False, "resolved": False}
    finally:
        await client.close()
