from src.models import ReviewComment, ReviewCommentList, ReviewOutput
from src.pipeline import (
    _check_run_conclusion,
    _extract_context,
    _extract_review_output_from_flow,
    _get_data_as_list,
    _is_line_in_diff,
    _parse_diff_changed_lines,
    _parse_diff_line_ranges,
    _review_event,
    _review_head_is_current,
    _split_review_comments,
)
from src.review_markdown import build_review_summary, normalize_inline_comment


class TestDiffLineParsing:
    def test_added_lines(self):
        diff = """+++ b/src/file.py
@@ -1,3 +1,5 @@
 unchanged
+new line 2
+new line 3
 unchanged"""
        ranges = _parse_diff_line_ranges(diff)
        assert "src/file.py" in ranges
        assert 2 in ranges["src/file.py"]
        assert 3 in ranges["src/file.py"]

    def test_context_lines_excluded(self):
        diff = """+++ b/src/app.py
@@ -1,0 +1,3 @@
+added
 context
+added2"""
        ranges = _parse_diff_line_ranges(diff)
        assert 1 in ranges["src/app.py"]
        assert 2 not in ranges["src/app.py"]
        assert 3 in ranges["src/app.py"]

    def test_multiple_hunks(self):
        diff = """+++ b/src/app.py
@@ -10,5 +10,8 @@
+added 10
+added 11
 unchanged
@@ -30,3 +33,2 @@
+added 33"""
        ranges = _parse_diff_line_ranges(diff)
        assert 10 in ranges["src/app.py"]
        assert 11 in ranges["src/app.py"]
        assert 33 in ranges["src/app.py"]

    def test_deleted_lines_are_tracked_on_left_side(self):
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,2 @@
 context
-removed update
 context"""

        changed = _parse_diff_changed_lines(diff)

        assert changed["src/app.py"]["LEFT"] == {11: "removed update"}
        assert changed["src/app.py"]["RIGHT"] == {}

    def test_exact_match_only(self):
        ranges = {"src/f.py": {10, 11, 12}}
        assert _is_line_in_diff("src/f.py", 10, ranges)
        assert _is_line_in_diff("src/f.py", 11, ranges)
        assert not _is_line_in_diff("src/f.py", 13, ranges)
        assert not _is_line_in_diff("src/f.py", 8, ranges)

    def test_missing_file(self):
        ranges = {"src/a.py": {1, 2}}
        assert not _is_line_in_diff("src/b.py", 1, ranges)

    def test_empty_ranges(self):
        assert not _is_line_in_diff("src/x.py", 1, {})

    def test_out_of_diff_comments_fall_back(self):
        diff = """+++ b/src/app.py
@@ -10,2 +10,3 @@
+added
 unchanged"""
        comments = [
            {"path": "src/app.py", "line": 10, "body": "inline"},
            {"path": "src/app.py", "line": 99, "body": "fallback"},
        ]

        inline, summary, unanchored, dropped = _split_review_comments(comments, diff)

        assert inline == [
            {
                "path": "src/app.py",
                "line": 10,
                "side": "RIGHT",
                "body": (
                    '<img alt="P2 Medium" src="https://img.shields.io/badge/P2-Medium-ca8a04?style=flat-square" '
                    'height="20" align="absmiddle"> '
                    "**inline**"
                ),
            }
        ]
        assert summary == [{"path": "src/app.py", "line": 10, "body": "inline", "side": "RIGHT"}]
        assert unanchored == [{"path": "src/app.py", "line": 99, "body": "fallback"}]
        assert dropped == []

    def test_low_priority_comments_are_dropped(self):
        diff = """+++ b/tests/test_async_client.py
@@ -281,3 +282,7 @@
 context
+    async def test_cached_task_with_ttl_and_stale_reclaim(self):
+        assert await ref.load() == 1
+        assert counter == 1
 context"""
        comments = [
            {
                "path": "tests/test_async_client.py",
                "line": 282,
                "body": "**`P3`** The test name says `with_ttl`, but no TTL is set.",
                "severity": "low",
            }
        ]

        inline, _summary, unanchored, dropped = _split_review_comments(comments, diff)

        assert inline == []
        assert unanchored == []
        assert dropped == comments

    def test_nearby_line_snaps_to_added_line(self):
        diff = """+++ b/tests/test_async_client.py
@@ -281,3 +282,7 @@
 context
+    async def test_cached_task_with_ttl_and_stale_reclaim(self):
+        assert await ref.load() == 1
+        assert counter == 1
 context"""
        comments = [
            {
                "path": "tests/test_async_client.py",
                "line": 282,
                "body": "This assertion can pass while the stale claim is still reused.",
                "severity": "medium",
            }
        ]

        inline, _summary, unanchored, dropped = _split_review_comments(comments, diff)

        assert unanchored == []
        assert dropped == []
        assert inline[0]["line"] == 283
        assert inline[0]["body"].startswith('<img alt="P2 Medium"')

    def test_deleted_line_comment_anchors_to_left_side(self):
        diff = """diff --git a/src/cache.py b/src/cache.py
--- a/src/cache.py
+++ b/src/cache.py
@@ -40,3 +40,2 @@
 row = fetch()
-touch_access(row)
 return row"""
        comments = [
            {
                "path": "src/cache.py",
                "line": 41,
                "side": "LEFT",
                "title": "Cache hits no longer refresh recency",
                "details": "Removing this touch means eviction can delete hot cached entries.",
                "severity": "high",
            }
        ]

        inline, summary, unanchored, dropped = _split_review_comments(comments, diff)

        assert inline == [
            {
                "path": "src/cache.py",
                "line": 41,
                "side": "LEFT",
                "body": (
                    '<img alt="P1 High" src="https://img.shields.io/badge/P1-High-f97316?style=flat-square" '
                    'height="20" align="absmiddle"> '
                    "**Cache hits no longer refresh recency**\n\n"
                    "Removing this touch means eviction can delete hot cached entries."
                ),
            }
        ]
        assert summary == [
            {
                "path": "src/cache.py",
                "line": 41,
                "side": "LEFT",
                "title": "Cache hits no longer refresh recency",
                "details": "Removing this touch means eviction can delete hot cached entries.",
                "severity": "high",
            }
        ]
        assert unanchored == []
        assert dropped == []

    def test_comments_on_unchanged_files_are_dropped(self):
        diff = """+++ b/src/changed.py
@@ -1,1 +1,2 @@
+changed"""
        comments = [
            {"path": "src/related.py", "line": 10, "body": "looks suspicious", "severity": "medium"},
        ]

        inline, summary, unanchored, dropped = _split_review_comments(comments, diff)

        assert inline == []
        assert summary == []
        assert unanchored == []
        assert dropped == comments

    def test_same_line_comments_are_merged(self):
        diff = """+++ b/src/app.py
@@ -10,1 +10,2 @@
+value = parse(raw)
 context"""
        comments = [
            {
                "path": "src/app.py",
                "line": 10,
                "title": "Parsing can crash",
                "details": "parse(raw) raises on empty input.",
                "severity": "high",
            },
            {
                "path": "src/app.py",
                "line": 10,
                "title": "None input is not handled",
                "details": "raw can be None on the fallback path.",
                "severity": "medium",
            },
        ]

        inline, summary, unanchored, dropped = _split_review_comments(comments, diff)

        assert len(inline) == 1
        assert len(summary) == 1
        assert "Also: None input is not handled" in summary[0]["details"]
        assert unanchored == []
        assert dropped == []

    def test_review_event_requests_changes_for_actionable_findings(self):
        assert _review_event([{"severity": "high"}]) == "REQUEST_CHANGES"
        assert _review_event([]) == "COMMENT"

    def test_check_run_conclusion_is_green_only_without_findings(self):
        assert _check_run_conclusion([]) == "success"
        assert _check_run_conclusion([{"severity": "medium"}]) == "neutral"
        assert _check_run_conclusion([{"severity": "critical"}]) == "failure"

    def test_check_run_failure_markers_are_configurable(self):
        assert _check_run_conclusion([{"severity": "high"}], fail_on=["P0"]) == "neutral"
        assert _check_run_conclusion([{"severity": "high"}], fail_on=["P0", "P1"]) == "failure"

    def test_findings_are_capped_by_config(self, monkeypatch):
        diff = """+++ b/src/app.py
@@ -10,1 +10,4 @@
+first = parse(raw)
+second = parse(raw)
+third = parse(raw)
 context"""
        comments = [
            {"path": "src/app.py", "line": 10, "title": "First", "severity": "medium"},
            {"path": "src/app.py", "line": 11, "title": "Second", "severity": "high"},
            {"path": "src/app.py", "line": 12, "title": "Third", "severity": "medium"},
        ]
        monkeypatch.setattr("src.pipeline.settings.max_findings", 2)

        inline, summary, unanchored, dropped = _split_review_comments(comments, diff)

        assert len(inline) == 2
        assert len(summary) == 2
        assert [c["title"] for c in summary] == ["Second", "First"]
        assert unanchored == []
        assert dropped == []


class TestReviewMarkdown:
    def test_insertion_suggestion_preserves_anchor_line(self):
        body = """**`P2`** Add an assertion comparing `expires_at`.

```suggestion
        assert log[0].expires_at is None
```"""

        rendered = normalize_inline_comment(
            body,
            severity="medium",
            path="tests/test_store.py",
            anchor_text='        assert log[0].status.value == "completed"',
            allow_suggestion=True,
        )

        assert '        assert log[0].status.value == "completed"' in rendered
        assert rendered.startswith('<img alt="P2 Medium"')
        assert "```suggestion" in rendered

    def test_summary_uses_badges_and_line_ranges(self):
        summary = build_review_summary(
            generated_summary="old model prose",
            comments=[
                {
                    "path": "src/app.py",
                    "start_line": 10,
                    "line": 14,
                    "title": "Handle missing payload before dereferencing it",
                    "details": "This can crash on None. Validate the input before dereferencing it.",
                    "body": "**P1 High** model markdown should be ignored when structured fields exist",
                    "severity": "high",
                }
            ],
            unanchored_comments=[],
            pr_title="fix app crash",
        )

        assert "## DS-Review" in summary
        assert "### Findings (1)" in summary
        assert '<img alt="P1 High"' in summary
        assert "**Handle missing payload before dereferencing it**" in summary
        assert "`src/app.py:10-14`" in summary
        assert "Validate the input before dereferencing it." in summary
        assert "### Optional Recheck" in summary
        assert "automatically when pull-request sync reviews are enabled" in summary
        assert "model markdown should be ignored" not in summary
        assert "| Pri |" not in summary

    def test_clean_summary_is_fixed_no_findings_text(self):
        summary = build_review_summary(
            generated_summary="",
            comments=[],
            unanchored_comments=[],
            pr_title="tidy up",
        )

        assert summary == (
            "## DS-Review\n\n"
            "No blocking issues found.\n\n"
            "Reviewed the changed diff and related call paths. No actionable bugs, security issues, or meaningful "
            "performance regressions were found."
        )

    def test_summary_strips_rendered_html_badges_from_fallback_body(self):
        rendered_body = (
            '<img alt="P2 Medium" src="https://img.shields.io/badge/P2-Medium-ca8a04?style=flat-square" '
            'height="20" align="absmiddle"> **Possible AttributeError if claimed_at is None**\n\n'
            "This should be plain text in the summary."
        )

        summary = build_review_summary(
            generated_summary="",
            comments=[
                {
                    "path": "src/store.py",
                    "line": 275,
                    "body": rendered_body,
                    "severity": "medium",
                }
            ],
            unanchored_comments=[],
            pr_title="test",
        )

        assert "**Possible AttributeError if claimed_at is None**" in summary
        assert "**<img" not in summary
        assert "**P2 Medium" not in summary


class TestReviewHeadGuard:
    async def test_review_head_guard_rejects_changed_head(self):
        client = FakeHeadClient("new-sha")

        ok = await _review_head_is_current(client, "owner/repo", 7, "token", "old-sha")

        assert ok is False
        assert client.calls == [("owner/repo", 7, "token")]

    async def test_review_head_guard_rejects_superseded_generation(self):
        client = FakeHeadClient("old-sha")

        ok = await _review_head_is_current(client, "owner/repo", 7, "token", "old-sha", is_current=lambda: False)

        assert ok is False
        assert client.calls == []

    async def test_review_head_guard_accepts_current_generation_and_head(self):
        client = FakeHeadClient("old-sha")

        ok = await _review_head_is_current(client, "owner/repo", 7, "token", "old-sha", is_current=lambda: True)

        assert ok is True
        assert client.calls == [("owner/repo", 7, "token")]


class TestDataExtraction:
    def test_extract_context_preserves_repeated_tool_results(self):
        diff_one = "diff for one"
        diff_two = "diff for two"
        report = FakeReport(
            tool_calls=[
                FakeToolCall(
                    "fetch_pr_details",
                    {"repo_full_name": "o/r", "pr_number": 1},
                    {"files": ["src/one.py", "src/two.py"], "diff": "+++ b/src/one.py\n@@ -1,0 +1,1 @@\n+one"},
                ),
                FakeToolCall("fetch_file_diff", {"repo_full_name": "o/r", "path": "src/one.py"}, diff_one),
                FakeToolCall("fetch_file_diff", {"repo_full_name": "o/r", "path": "src/two.py"}, diff_two),
            ]
        )

        context = _extract_context(report)

        assert context["fetch_file_diff"] == [diff_one, diff_two]
        assert context["fetch_file_diff:src/one.py"] == diff_one
        assert context["fetch_file_diff:src/two.py"] == diff_two
        assert len(context["tool_results"]) == 3
        assert context["review_scope"]["changed_files"] == ["src/one.py"]

    def test_from_review_comment_list(self):
        rc = ReviewCommentList(
            comments=[
                ReviewComment(path="a.py", line=1, body="issue", category="bug", severity="critical"),
                ReviewComment(path="b.py", line=2, body="another", category="security", severity="high"),
            ]
        )
        result = _get_data_as_list(FakeReport(data=rc))
        assert len(result) == 2
        assert result[0]["path"] == "a.py"
        assert result[1]["path"] == "b.py"

    def test_from_review_output(self):
        ro = ReviewOutput(summary="test", comments=[ReviewComment(path="x.py", line=1, body="test", category="bug")])
        result = _get_data_as_list(FakeReport(data=ro))
        assert len(result) == 1
        assert result[0]["path"] == "x.py"

    def test_empty(self):
        assert _get_data_as_list(FakeReport(data=None)) == []
        assert _get_data_as_list(FakeReport(data=[])) == []

    def test_flow_wrapper_extraction(self):
        ro = ReviewOutput(summary="test summary", comments=[ReviewComment(path="f.py", line=1, body="ok")])
        flow_data = [
            {"index": 0, "content": "...", "data": None},
            {"index": 1, "content": "...", "data": ro},
        ]
        result = _extract_review_output_from_flow(FakeReport(data=flow_data))
        assert result is not None
        assert "summary" in result
        assert result["summary"] == "test summary"

    def test_flow_wrapper_no_match(self):
        flow_data = [
            {"index": 0, "content": "...", "data": None},
        ]
        result = _extract_review_output_from_flow(FakeReport(data=flow_data))
        assert result is None


class FakeReport:
    def __init__(self, data=None, tool_calls=None):
        self.data = data
        self.tool_calls = tool_calls or []
        self.content = ""


class FakeToolCall:
    def __init__(self, name, arguments, content):
        self.name = name
        self.arguments = arguments
        self.result = FakeToolResult(content)


class FakeToolResult:
    def __init__(self, content):
        self.content = content
        self.data = content


class FakeHeadClient:
    def __init__(self, head_sha):
        self.head_sha = head_sha
        self.calls = []

    async def get_pr_details(self, repo, pr_number, token):
        self.calls.append((repo, pr_number, token))
        return {"head_sha": self.head_sha}


class TestEffectiveReviewEvent:
    def test_downgrades_request_changes_on_own_pr(self):
        from src.pipeline import _effective_review_event

        assert _effective_review_event("REQUEST_CHANGES", "jolovicdev", "jolovicdev") == "COMMENT"

    def test_keeps_request_changes_for_other_authors(self):
        from src.pipeline import _effective_review_event

        assert _effective_review_event("REQUEST_CHANGES", "jolovicdev", "contributor") == "REQUEST_CHANGES"

    def test_keeps_comment_event_unchanged(self):
        from src.pipeline import _effective_review_event

        assert _effective_review_event("COMMENT", "jolovicdev", "jolovicdev") == "COMMENT"


class TestDeskLifecycle:
    async def test_desk_is_closed_when_flow_fails(self, monkeypatch, tmp_path):
        from src import persistent_state as ps
        from src import pipeline

        monkeypatch.setattr(ps, "STATE_PATH", tmp_path / "state.json")
        closed = []

        class StubFlow:
            async def arun(self, job):
                raise RuntimeError("flow boom")

        class StubDesk:
            def __init__(self, **kwargs):
                pass

            def flow(self, steps, name=None):
                return StubFlow()

            def close(self):
                closed.append(True)

        class FakeClient:
            async def get_token(self, installation_id):
                return "t"

            async def get_pr_details(self, repo, pr_number, token):
                return {
                    "title": "t",
                    "body": "",
                    "author_login": "someone",
                    "diff": "",
                    "files": [],
                    "base_ref": "main",
                    "head_sha": "a" * 40,
                }

            async def post_review(self, *args, **kwargs):
                return {"id": 1}

            async def close(self):
                pass

        monkeypatch.setattr(pipeline, "make_github_client", lambda: FakeClient())
        monkeypatch.setattr(pipeline, "Desk", StubDesk)

        result = await pipeline.run_review_pipeline("owner/repo", 7)

        assert result is None
        assert closed == [True]


class TestSummaryVerdictMatchesEvent:
    def _summary(self, event=None):
        comments = [
            {"path": "a.py", "line": 3, "severity": "critical", "title": "t", "details": "d", "body": ""}
        ]
        kwargs = {
            "generated_summary": "s",
            "comments": comments,
            "unanchored_comments": [],
            "pr_title": "P",
        }
        if event is not None:
            kwargs["event"] = event
        return build_review_summary(**kwargs)

    def test_default_event_requests_changes(self):
        assert "Request changes - 1 actionable finding" in self._summary()

    def test_downgraded_event_renders_comment_verdict(self):
        body = self._summary(event="COMMENT")
        assert "Comment - 1 actionable finding, highest severity P0." in body
        assert "Request changes" not in body

    def test_low_priority_findings_still_render_comment(self):
        body = self._summary(event="COMMENT")
        assert "low-priority" not in body
