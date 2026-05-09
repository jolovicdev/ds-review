import asyncio

import pytest

from src import main
from src.review_queue import ReviewCoordinator


class FakeReactionClient:
    def __init__(self):
        self.calls = []

    async def get_token(self, installation_id):
        self.calls.append(("get_token", installation_id))
        return "token"

    async def add_review_comment_reaction(self, repo, comment_id, content, token):
        self.calls.append(("add_review_comment_reaction", repo, comment_id, content, token))
        return 123 if content == "eyes" else 456

    async def delete_review_comment_reaction(self, repo, comment_id, reaction_id, token):
        self.calls.append(("delete_review_comment_reaction", repo, comment_id, reaction_id, token))

    async def add_issue_reaction(self, repo, pr_number, content, token):
        self.calls.append(("add_issue_reaction", repo, pr_number, content, token))
        return 789

    async def delete_issue_reaction(self, repo, pr_number, reaction_id, token):
        self.calls.append(("delete_issue_reaction", repo, pr_number, reaction_id, token))

    async def resolve_review_thread(self, repo, pr_number, comment_id, token):
        self.calls.append(("resolve_review_thread", repo, pr_number, comment_id, token))
        return True

    async def close(self):
        self.calls.append(("close",))


@pytest.mark.asyncio
async def test_recheck_reactions_are_finalized_when_resolved(monkeypatch):
    fake = FakeReactionClient()
    monkeypatch.setattr(main, "make_github_client", lambda: fake)

    async def fake_reply(*args):
        return {"ignored": False, "resolved": True}

    monkeypatch.setattr(main, "handle_comment_reply", fake_reply)

    await main._dispatch_conversation(
        "owner/repo",
        7,
        42,
        parent_id=100,
        reply_id=200,
        body="@ds-review recheck",
        path="src/app.py",
        line=10,
    )

    assert ("add_review_comment_reaction", "owner/repo", 200, "eyes", "token") in fake.calls
    assert ("delete_review_comment_reaction", "owner/repo", 200, 123, "token") in fake.calls
    assert ("add_review_comment_reaction", "owner/repo", 200, "+1", "token") in fake.calls
    assert ("add_issue_reaction", "owner/repo", 7, "+1", "token") in fake.calls
    assert ("resolve_review_thread", "owner/repo", 7, 100, "token") in fake.calls
    assert fake.calls[-1] == ("close",)


@pytest.mark.asyncio
async def test_recheck_removes_eyes_without_like_when_still_open(monkeypatch):
    fake = FakeReactionClient()
    monkeypatch.setattr(main, "make_github_client", lambda: fake)

    async def fake_reply(*args):
        return {"ignored": False, "resolved": False}

    monkeypatch.setattr(main, "handle_comment_reply", fake_reply)

    await main._dispatch_conversation(
        "owner/repo",
        7,
        42,
        parent_id=100,
        reply_id=200,
        body="@ds-review recheck",
        path="src/app.py",
        line=10,
    )

    assert ("add_review_comment_reaction", "owner/repo", 200, "eyes", "token") in fake.calls
    assert ("delete_review_comment_reaction", "owner/repo", 200, 123, "token") in fake.calls
    assert ("add_review_comment_reaction", "owner/repo", 200, "+1", "token") not in fake.calls
    assert ("add_issue_reaction", "owner/repo", 7, "+1", "token") not in fake.calls


@pytest.mark.asyncio
async def test_review_coordinator_runs_one_review_per_pr_and_replays_latest():
    calls = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def dispatch(repo, pr_number, installation_id, is_current):
        calls.append(("start", repo, pr_number, installation_id, is_current()))
        if installation_id == 101:
            first_started.set()
            await release_first.wait()
            calls.append(("finish", installation_id, is_current()))
            return
        calls.append(("finish", installation_id, is_current()))

    coordinator = ReviewCoordinator(dispatch)

    assert await coordinator.enqueue("owner/repo", 7, 101) == 1
    await first_started.wait()
    assert await coordinator.enqueue("owner/repo", 7, 202) == 2
    release_first.set()
    await coordinator.wait_idle()

    assert calls == [
        ("start", "owner/repo", 7, 101, True),
        ("finish", 101, False),
        ("start", "owner/repo", 7, 202, True),
        ("finish", 202, True),
    ]


@pytest.mark.asyncio
async def test_dispatch_requeues_when_reviewed_head_is_stale(monkeypatch):
    fake = FakeReactionClient()
    monkeypatch.setattr(main, "make_github_client", lambda: fake)

    async def fake_pipeline(*args, **kwargs):
        assert kwargs["is_current"]() is True
        return {
            "stale": True,
            "reason": "head_changed",
            "reviewed_head": "old-sha",
            "current_head": "new-sha",
        }

    class FakeCoordinator:
        def __init__(self):
            self.calls = []

        async def enqueue(self, repo, pr_number, installation_id):
            self.calls.append((repo, pr_number, installation_id))
            return 2

    coordinator = FakeCoordinator()
    monkeypatch.setattr(main, "run_review_pipeline", fake_pipeline)
    monkeypatch.setattr(main, "review_coordinator", coordinator)
    monkeypatch.setattr(main, "save_pending_review", lambda repo, pr_number, installation_id: None)

    await main._dispatch_review("owner/repo", 7, 42, lambda: True)

    assert coordinator.calls == [("owner/repo", 7, 42)]
    assert ("delete_issue_reaction", "owner/repo", 7, 789, "token") in fake.calls
