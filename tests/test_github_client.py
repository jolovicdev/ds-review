from src.github_client import is_reviewable_source_path, is_skipped_repo_path


def test_reviewable_source_path_includes_common_code_and_diff_files():
    assert is_reviewable_source_path("src/plugin.lua")
    assert is_reviewable_source_path("patches/fix-cache.patch")
    assert is_reviewable_source_path("Dockerfile.prod")
    assert is_reviewable_source_path("infra/main.tf")


def test_reviewable_source_path_skips_generated_dependency_noise():
    assert is_skipped_repo_path("node_modules/lib/index.js")
    assert is_skipped_repo_path("dist/app.js")
    assert is_skipped_repo_path("src/app.min.js")
    assert is_skipped_repo_path("uv.lock")
    assert not is_reviewable_source_path("node_modules/lib/index.js")
    assert not is_reviewable_source_path("package-lock.json")


def test_get_file_content_encodes_special_characters():
    import httpx

    from src.github_client import GitHubClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"content": ""})

    client = GitHubClient(token="t")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    import asyncio

    asyncio.run(client.get_file_content("owner/repo", "src/a#b?c.py", "refs/heads/x", "t"))

    assert "/contents/src/a%23b%3Fc.py?ref=refs%2Fheads%2Fx" in seen["url"]


def test_get_incremental_diff_reassembles_parseable_unified_diff():
    import asyncio

    import httpx

    from src.github_client import GitHubClient
    from src.pipeline import _parse_diff_changed_lines

    payload = {
            "status": "ahead",
            "ahead_by": 2,
            "files": [
                {"filename": "src/app.py", "patch": "@@ -10,3 +10,4 @@\n context\n-old line\n+new line\n+added line"},
                {"filename": "src/bin.png", "patch": None},
                {"filename": "src/util.py", "patch": "@@ -1,2 +1,2 @@\n-a\n+b"},
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = GitHubClient(token="t")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    diff, ahead_by = asyncio.run(client.get_incremental_diff("owner/repo", "base", "head", "t"))

    assert ahead_by == 2
    changed = _parse_diff_changed_lines(diff)
    assert set(changed) == {"src/app.py", "src/util.py"}
    assert changed["src/app.py"]["RIGHT"] == {11: "new line", 12: "added line"}
    assert changed["src/app.py"]["LEFT"] == {11: "old line"}


def test_get_incremental_diff_raises_when_compare_diff_truncated():
    import asyncio

    import httpx

    from src.github_client import GitHubClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ahead_by": 5, "diff_truncated": True, "files": []})

    client = GitHubClient(token="t")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        asyncio.run(client.get_incremental_diff("owner/repo", "base", "head", "t"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_get_incremental_diff_rejects_non_ahead_comparison():
    import asyncio

    import httpx

    from src.github_client import GitHubClient

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "status": "diverged",
            "ahead_by": 2,
            "behind_by": 4,
            "files": [{"filename": "a.py", "patch": "@@ -1 +1 @@\n-a\n+b"}],
        }
        return httpx.Response(200, json=payload)

    client = GitHubClient(token="t")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        asyncio.run(client.get_incremental_diff("owner/repo", "base", "head", "t"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "diverged" in str(exc)


def test_get_incremental_diff_rejects_capped_file_list():
    import asyncio

    import httpx

    from src.github_client import GitHubClient

    def handler(request: httpx.Request) -> httpx.Response:
        files = [{"filename": f"f{i}.py", "patch": "@@ -1 +1 @@\n-a\n+b"} for i in range(300)]
        return httpx.Response(200, json={"status": "ahead", "ahead_by": 1, "files": files})

    client = GitHubClient(token="t")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        asyncio.run(client.get_incremental_diff("owner/repo", "base", "head", "t"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "300" in str(exc)
