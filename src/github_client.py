import base64
import logging
import time
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
import jwt

GITHUB_API_BASE = "https://api.github.com"
GITHUB_HTTP_TIMEOUT = 40.0
logger = logging.getLogger("ds-review")

REVIEWABLE_EXTENSIONS = {
    ".astro",
    ".bicep",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".mts",
    ".cts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hh",
    ".hpp",
    ".cs",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".lua",
    ".ex",
    ".exs",
    ".erl",
    ".hrl",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".md",
    ".rst",
    ".sql",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".html",
    ".vue",
    ".svelte",
    ".xml",
    ".proto",
    ".graphql",
    ".gql",
    ".tf",
    ".tfvars",
    ".nix",
    ".patch",
    ".diff",
}

SKIP_DIRECTORIES = {
    "node_modules",
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "dist",
    "build",
    "out",
    "target",
    "coverage",
    "htmlcov",
    ".next",
    ".nuxt",
    ".output",
    ".turbo",
    ".cache",
    ".parcel-cache",
    ".terraform",
    ".gradle",
    "vendor",
    ".tox",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

REVIEWABLE_FILENAMES = {
    ".dockerignore",
    ".env.example",
    ".env.sample",
    ".gitignore",
    "BUILD",
    "Caddyfile",
    "Containerfile",
    "Dockerfile",
    "Gemfile",
    "Justfile",
    "Makefile",
    "Pipfile",
    "Procfile",
    "Rakefile",
    "WORKSPACE",
    "go.mod",
    "pyproject.toml",
    "requirements.txt",
}

SKIP_FILENAMES = {
    "Cargo.lock",
    "Pipfile.lock",
    "go.sum",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}

SKIP_SUFFIXES = (".map", ".min.css", ".min.js", ".snap")

MAX_CODEBASE_CHARS = 200_000


def is_skipped_repo_path(path: str) -> bool:
    posix_path = PurePosixPath(path)
    parts = set(posix_path.parts)
    name = posix_path.name
    return bool(parts & SKIP_DIRECTORIES) or name in SKIP_FILENAMES or name.endswith(SKIP_SUFFIXES)


def is_reviewable_source_path(path: str) -> bool:
    if is_skipped_repo_path(path):
        return False
    posix_path = PurePosixPath(path)
    name = posix_path.name
    if name in REVIEWABLE_FILENAMES or name.startswith(("Dockerfile.", "Containerfile.")):
        return True
    return posix_path.suffix.lower() in REVIEWABLE_EXTENSIONS


class GitHubClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        app_id: int | None = None,
        private_key: str | None = None,
    ) -> None:
        self._token: str | None = None
        self._mode: str
        if token:
            self._token = token
            self._mode = "user"
        elif app_id and private_key:
            self._token = None
            self._mode = "app"
            self.app_id = app_id
            self.private_key = private_key
        else:
            raise ValueError(
                "Provide either token= for user mode (GitHub Actions / PAT) or app_id= + private_key= for app mode."
            )
        self._client = httpx.AsyncClient(timeout=GITHUB_HTTP_TIMEOUT)

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def mode(self) -> str:
        return self._mode

    def _jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 600, "iss": str(self.app_id)}
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    async def get_token(self, installation_id: int = 0) -> str:
        if self._mode == "user":
            if self._token is None:
                raise RuntimeError("GitHub user token is not configured")
            return self._token
        resp = await self._client.post(
            f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self._jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )
        resp.raise_for_status()
        return resp.json()["token"]

    async def get_pr_details(self, repo: str, pr_number: int, token: str) -> dict:
        headers = self._auth(token)
        pr_resp = await self._client.get(f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}", headers=headers)
        pr_resp.raise_for_status()
        pr = pr_resp.json()

        diff_resp = await self._client.get(
            f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}",
            headers={**headers, "Accept": "application/vnd.github.v3.diff"},
        )
        diff_resp.raise_for_status()

        files: list[str] = []
        page = 1
        while True:
            files_resp = await self._client.get(
                f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/files",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            files_resp.raise_for_status()
            files.extend(f["filename"] for f in files_resp.json())
            if "next" not in files_resp.links:
                break
            page += 1

        return {
            "title": pr["title"],
            "body": pr.get("body") or "",
            "author_login": pr.get("user", {}).get("login", ""),
            "diff": diff_resp.text,
            "files": files,
            "base_ref": pr["base"]["ref"],
            "head_sha": pr["head"]["sha"],
        }

    async def get_file_content(self, repo: str, path: str, ref: str, token: str) -> str | None:
        resp = await self._client.get(
            f"{GITHUB_API_BASE}/repos/{repo}/contents/{quote(path, safe='/')}",
            headers=self._auth(token),
            params={"ref": ref},
        )
        if resp.status_code != 200:
            return None
        content = resp.json().get("content", "")
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            return None

    async def get_repo_tree(self, repo: str, ref: str, token: str) -> list[dict]:
        resp = await self._client.get(
            f"{GITHUB_API_BASE}/repos/{repo}/git/trees/{ref}?recursive=1",
            headers=self._auth(token),
        )
        resp.raise_for_status()
        return resp.json().get("tree", [])

    async def get_full_codebase(self, repo: str, ref: str, token: str) -> str:
        tree = await self.get_repo_tree(repo, ref, token)
        parts: list[str] = []
        total = 0

        for entry in tree:
            if entry["type"] != "blob":
                continue
            path = entry["path"]
            if not is_reviewable_source_path(path):
                continue

            content = await self.get_file_content(repo, path, ref, token)
            if content is None:
                continue
            if len(content) > 50_000:
                content = content[:50_000] + "\n# ... [truncated]"

            chunk = f"\n--- {path} ---\n{content}\n"
            if total + len(chunk) > MAX_CODEBASE_CHARS:
                parts.append(chunk[: MAX_CODEBASE_CHARS - total])
                parts.append("\n# ... [remaining files truncated]")
                break
            parts.append(chunk)
            total += len(chunk)

        return "".join(parts)

    async def get_guidelines(self, repo: str, token: str, ref: str = "HEAD") -> str | None:
        for path in ["CONTEXT.md", "CONTRIBUTING.md", "README.md"]:
            content = await self.get_file_content(repo, path, ref, token)
            if content:
                return content
        return None

    async def post_review(
        self,
        repo: str,
        pr_number: int,
        summary: str,
        comments: list[dict],
        token: str,
        event: str = "COMMENT",
    ) -> dict:
        resp = await self._client.post(
            f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/reviews",
            headers=self._auth(token),
            json={"body": summary, "event": event, "comments": comments},
        )
        resp.raise_for_status()
        return resp.json()

    async def post_review_with_fallback(
        self,
        repo: str,
        pr_number: int,
        summary: str,
        comments: list[dict],
        token: str,
        event: str = "COMMENT",
    ) -> dict:
        try:
            return await self.post_review(repo, pr_number, summary, comments, token, event=event)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 422 or not comments:
                raise

        logger.warning("Bulk review comment post failed; retrying comments one by one")
        review = await self.post_review(repo, pr_number, summary, [], token, event=event)
        for comment in comments:
            try:
                await self.post_review(repo, pr_number, "", [comment], token)
            except httpx.HTTPStatusError as e:
                logger.warning(
                    "Dropping inline comment GitHub rejected: %s:%s (%s)",
                    comment.get("path", ""),
                    comment.get("line", ""),
                    e.response.status_code,
                )
        return review

    async def post_issue_comment(self, repo: str, issue_number: int, body: str, token: str) -> dict:
        resp = await self._client.post(
            f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/comments",
            headers=self._auth(token),
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_pr_reviews(self, repo: str, pr_number: int, token: str) -> list[dict]:
        reviews = []
        page = 1
        while True:
            resp = await self._client.get(
                f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/reviews",
                headers=self._auth(token),
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            reviews.extend(resp.json())
            if "next" not in resp.links:
                break
            page += 1
        return reviews

    async def list_review_comments(self, repo: str, pr_number: int, token: str) -> list[dict]:
        comments = []
        page = 1
        while True:
            resp = await self._client.get(
                f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/comments",
                headers=self._auth(token),
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            comments.extend(resp.json())
            if "next" not in resp.links:
                break
            page += 1
        return comments

    async def get_bot_username(self, token: str) -> str:
        if self._mode == "app":
            resp = await self._client.get(
                f"{GITHUB_API_BASE}/app",
                headers={
                    "Authorization": f"Bearer {self._jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            )
            resp.raise_for_status()
            slug = resp.json().get("slug", "")
            return f"{slug}[bot]" if slug else ""

        resp = await self._client.get(f"{GITHUB_API_BASE}/user", headers=self._auth(token))
        resp.raise_for_status()
        return resp.json().get("login", "")

    async def get_review_comment(self, repo: str, comment_id: int, token: str) -> dict | None:
        resp = await self._client.get(
            f"{GITHUB_API_BASE}/repos/{repo}/pulls/comments/{comment_id}",
            headers=self._auth(token),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def reply_to_review_comment(self, repo: str, pr_number: int, comment_id: int, body: str, token: str) -> dict:
        resp = await self._client.post(
            f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
            headers=self._auth(token),
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()

    async def add_issue_reaction(self, repo: str, issue_number: int, content: str, token: str) -> int | None:
        resp = await self._client.post(
            f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/reactions",
            headers=self._auth(token),
            json={"content": content},
        )
        resp.raise_for_status()
        return resp.json().get("id")

    async def delete_issue_reaction(self, repo: str, issue_number: int, reaction_id: int, token: str) -> None:
        resp = await self._client.delete(
            f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/reactions/{reaction_id}",
            headers=self._auth(token),
        )
        resp.raise_for_status()

    async def add_review_comment_reaction(
        self, repo: str, comment_id: int, content: str, token: str
    ) -> int | None:
        resp = await self._client.post(
            f"{GITHUB_API_BASE}/repos/{repo}/pulls/comments/{comment_id}/reactions",
            headers=self._auth(token),
            json={"content": content},
        )
        resp.raise_for_status()
        return resp.json().get("id")

    async def delete_review_comment_reaction(self, repo: str, comment_id: int, reaction_id: int, token: str) -> None:
        resp = await self._client.delete(
            f"{GITHUB_API_BASE}/repos/{repo}/pulls/comments/{comment_id}/reactions/{reaction_id}",
            headers=self._auth(token),
        )
        resp.raise_for_status()

    async def graphql(self, query: str, variables: dict, token: str) -> dict:
        resp = await self._client.post(
            f"{GITHUB_API_BASE}/graphql",
            headers=self._auth(token),
            json={"query": query, "variables": variables},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
        return data.get("data", {})

    async def find_review_thread(
        self, repo: str, pr_number: int, comment_id: int, token: str
    ) -> dict | None:
        owner, name = repo.split("/", 1)
        after = None
        query = """
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              reviewThreads(first: 100, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  isResolved
                  comments(first: 100) {
                    nodes { databaseId }
                  }
                }
              }
            }
          }
        }
        """
        while True:
            data = await self.graphql(
                query,
                {"owner": owner, "name": name, "number": pr_number, "after": after},
                token,
            )
            repo_data = data.get("repository") or {}
            pr_data = repo_data.get("pullRequest") or {}
            threads = pr_data.get("reviewThreads") or {}
            for thread in threads.get("nodes", []):
                comment_ids = [
                    c.get("databaseId")
                    for c in thread.get("comments", {}).get("nodes", [])
                ]
                if comment_id in comment_ids:
                    return thread
            page_info = threads.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                return None
            after = page_info.get("endCursor")

    async def resolve_review_thread(self, repo: str, pr_number: int, comment_id: int, token: str) -> bool:
        thread = await self.find_review_thread(repo, pr_number, comment_id, token)
        if thread is None:
            return False
        if thread.get("isResolved"):
            return True
        mutation = """
        mutation($threadId: ID!) {
          resolveReviewThread(input: {threadId: $threadId}) {
            thread { id isResolved }
          }
        }
        """
        data = await self.graphql(mutation, {"threadId": thread["id"]}, token)
        resolved = (
            data.get("resolveReviewThread", {})
            .get("thread", {})
            .get("isResolved")
        )
        return bool(resolved)

    async def get_changed_file(self, repo: str, path: str, ref: str, token: str) -> str | None:
        content = await self.get_file_content(repo, path, ref, token)
        if content is None:
            return None
        lines = content.split("\n")
        numbered = "\n".join(f"{i + 1:4d} | {line}" for i, line in enumerate(lines))
        return f"--- {path} ({len(lines)} lines) ---\n{numbered}"

    async def get_files_importing(
        self,
        repo: str,
        target: str,
        changed_files: list[str],
        token: str,
        ref: str = "HEAD",
    ) -> list[str]:
        import_name = target.rsplit(".", 1)[0].replace("/", ".").rsplit(".", 1)[-1]
        tree = await self.get_repo_tree(repo, ref, token)
        related = []
        for entry in tree:
            if entry["type"] != "blob":
                continue
            path = entry["path"]
            if path in changed_files:
                continue
            if not is_reviewable_source_path(path):
                continue
            content = await self.get_file_content(repo, path, ref, token)
            if content and (import_name in content or target in content):
                related.append(path)
                if len(related) >= 5:
                    break
        return related

    async def _get_all_pr_files(self, repo: str, pr_number: int, token: str) -> set[str]:
        files: set[str] = set()
        page = 1
        while True:
            resp = await self._client.get(
                f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/files",
                headers=self._auth(token),
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            files.update(f["filename"] for f in resp.json())
            if "next" not in resp.links:
                break
            page += 1
        return files

    async def get_recent_prs(self, repo: str, changed_files: list[str], token: str, limit: int = 5) -> str:
        resp = await self._client.get(
            f"{GITHUB_API_BASE}/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=20",
            headers=self._auth(token),
        )
        resp.raise_for_status()
        prs = resp.json()
        results = []
        seen = set()
        for pr in prs:
            pr_files = await self._get_all_pr_files(repo, pr["number"], token)
            overlap = pr_files & set(changed_files)
            if overlap and pr["number"] not in seen:
                seen.add(pr["number"])
                results.append(
                    {
                        "number": pr["number"],
                        "title": pr["title"],
                        "files": sorted(overlap),
                        "merged_at": pr.get("merged_at", ""),
                    }
                )
                if len(results) >= limit:
                    break
        if not results:
            return "No recent PRs found touching the same files."
        out = []
        for r in results:
            out.append(f"- PR #{r['number']}: {r['title']} (files: {', '.join(r['files'])})")
        return "\n".join(out)

    async def create_check_run(
        self,
        repo: str,
        pr_number: int,
        head_sha: str,
        summary: str,
        conclusion: str,
        token: str,
    ) -> dict:
        resp = await self._client.post(
            f"{GITHUB_API_BASE}/repos/{repo}/check-runs",
            headers=self._auth(token),
            json={
                "name": "DS-Review",
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {
                    "title": (
                        "DS-Review: no findings"
                        if conclusion == "success" and "No blocking issues found." in summary
                        else "DS-Review completed"
                    ),
                    "summary": summary[:3000],
                },
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def approve_pr(self, repo: str, pr_number: int, token: str) -> dict:
        resp = await self._client.post(
            f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/reviews",
            headers=self._auth(token),
            json={
                "event": "APPROVE",
                "body": "Auto-approved by DS-Review — no critical or high-severity issues found.",
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def update_review(self, repo: str, pr_number: int, review_id: int, body: str, token: str) -> dict:
        resp = await self._client.put(
            f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/reviews/{review_id}",
            headers=self._auth(token),
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_commits_between(self, repo: str, base: str, head: str, token: str) -> list[dict]:
        resp = await self._client.get(
            f"{GITHUB_API_BASE}/repos/{repo}/compare/{base}...{head}",
            headers=self._auth(token),
        )
        resp.raise_for_status()
        return resp.json().get("commits", [])

    def _auth(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
