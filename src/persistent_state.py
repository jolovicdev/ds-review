import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

STATE_PATH = Path(".blackgeorge/review_state.json")

logger = logging.getLogger("ds-review")


@contextmanager
def _state_lock():
    lock_path = STATE_PATH.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Review state file unreadable, starting empty", exc_info=True)
        return {}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, STATE_PATH)


def get_pr_state(repo: str, pr_number: int) -> dict:
    key = f"{repo}#{pr_number}"
    return _load().get(key, {})


def save_review(repo: str, pr_number: int, review_id: int, commit_sha: str, summary_comment_id: int = 0) -> None:
    with _state_lock():
        state = _load()
        key = f"{repo}#{pr_number}"
        entry = state.get(key, {})
        entry["review_id"] = review_id
        entry["last_commit_sha"] = commit_sha
        if summary_comment_id:
            entry["summary_comment_id"] = summary_comment_id
        entry["updated_at"] = datetime.now(UTC).isoformat()
        state[key] = entry
        _save(state)


def get_last_commit(repo: str, pr_number: int) -> str:
    return get_pr_state(repo, pr_number).get("last_commit_sha", "")


def get_review_id(repo: str, pr_number: int) -> int:
    return get_pr_state(repo, pr_number).get("review_id", 0)


def save_pending_review(repo: str, pr_number: int, installation_id: int = 0) -> None:
    with _state_lock():
        state = _load()
        key = f"{repo}#{pr_number}"
        state[key] = {
            **state.get(key, {}),
            "pending": True,
            "installation_id": installation_id,
            "queued_at": datetime.now(UTC).isoformat(),
        }
        _save(state)


def note_requeue(repo: str, pr_number: int) -> int:
    with _state_lock():
        state = _load()
        key = f"{repo}#{pr_number}"
        entry = state.get(key, {})
        entry["requeues"] = int(entry.get("requeues", 0)) + 1
        state[key] = entry
        _save(state)
        return entry["requeues"]


def clear_pending(repo: str, pr_number: int) -> None:
    with _state_lock():
        state = _load()
        key = f"{repo}#{pr_number}"
        if key in state:
            state[key].pop("pending", None)
            state[key].pop("queued_at", None)
            state[key].pop("requeues", None)
            _save(state)


def get_pending_reviews() -> list[dict]:
    state = _load()
    pending = []
    for key, entry in state.items():
        if entry.get("pending"):
            parts = key.split("#")
            if len(parts) == 2:
                pending.append(
                    {
                        "repo": parts[0],
                        "pr_number": int(parts[1]),
                        "installation_id": entry.get("installation_id", 0),
                    }
                )
    return pending
