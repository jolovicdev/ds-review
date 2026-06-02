import re

from blackgeorge.tools import tool

from src.github_client import GitHubClient


def _extract_file_diff(diff: str, target_path: str) -> str:
    lines = diff.split("\n")
    result = []
    in_target = False
    old_line = 0
    new_line = 0
    for line in lines:
        if line.startswith("+++ b/") and line[6:] == target_path:
            in_target = True
            result.append(f"--- {target_path}")
            continue
        if in_target:
            if line.startswith("diff ") or (line.startswith("--- ") and not line.startswith("--- /dev/null")):
                break
            if line.startswith("@@"):
                match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if match:
                    old_line = int(match.group(1))
                    new_line = int(match.group(2))
                result.append(line)
            elif line.startswith("+") and not line.startswith("+++"):
                result.append(f"R{new_line} + {line[1:]}")
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                result.append(f"L{old_line} - {line[1:]}")
                old_line += 1
            elif line.startswith(" "):
                result.append(f"R{new_line}   {line[1:]}")
                old_line += 1
                new_line += 1
    if not result:
        result.append(f"(no diff hunks for {target_path})")
    result.insert(
        0,
        "Use R line numbers marked with '+' for ReviewComment.line when the issue is in added or replaced code. "
        "Use L line numbers marked with '-' and ReviewComment.side='LEFT' only when the deletion itself caused the "
        "issue. Do not anchor review comments to hunk headers or unchanged context lines.",
    )
    return "\n".join(result)


def make_tools(client: GitHubClient, token: str, pr_details: dict | None = None):
    _state: dict = {}

    def _remember_pr_details(details: dict) -> dict:
        _state["head_sha"] = details.get("head_sha", "")
        _state["base_ref"] = details.get("base_ref", "")
        _state["diff"] = details.get("diff", "")
        return dict(details)

    if pr_details is not None:
        _remember_pr_details(pr_details)

    @tool(
        description=(
            "Fetch PR details: title, body, diff, changed files list, and head SHA. "
            "Stores head_sha and diff for subsequent fetch_changed_file and fetch_file_diff calls."
        )
    )
    async def fetch_pr_details(repo_full_name: str, pr_number: int) -> dict:
        if pr_details is not None:
            return _remember_pr_details(pr_details)
        return _remember_pr_details(await client.get_pr_details(repo_full_name, pr_number, token))

    @tool(
        description=(
            "Fetch a single changed file with line numbers. "
            "Uses head_sha from fetch_pr_details — call fetch_pr_details first."
        )
    )
    async def fetch_changed_file(repo_full_name: str, path: str) -> str:
        ref = _state.get("head_sha")
        if not ref:
            return "Error: fetch_pr_details must be called before fetch_changed_file. No head_sha stored."
        content = await client.get_changed_file(repo_full_name, path, ref, token)
        return content or f"File {path} not found or binary."

    @tool(
        description=(
            "Extract diff hunks for a specific file from the PR diff. "
            "Shows only changed lines with line numbers and +/- markers. "
            "Call this for every changed file to focus the reviewers on what actually changed."
        )
    )
    async def fetch_file_diff(repo_full_name: str, path: str) -> str:
        diff = _state.get("diff", "")
        if not diff:
            return "Error: fetch_pr_details must be called first to load the PR diff."
        hunks = _extract_file_diff(diff, path)
        return hunks if hunks else f"No diff hunks found for {path}."

    @tool(description="Find files that import or reference a target file. Returns up to 5 related files.")
    async def fetch_related_files(repo_full_name: str, target_path: str, changed_files: list[str]) -> str:
        ref = _state.get("head_sha", "HEAD")
        related = await client.get_files_importing(repo_full_name, target_path, changed_files, token, ref=ref)
        if not related:
            return "No related files found."
        parts = []
        for path in related[:3]:
            content = await client.get_changed_file(repo_full_name, path, ref, token)
            if content:
                parts.append(content[:4000])
        return "\n\n".join(parts) if parts else "Related files could not be read."

    @tool(description="Fetch repository review guidelines from CONTEXT.md, CONTRIBUTING.md, or README.md")
    async def fetch_repo_guidelines(repo_full_name: str) -> str:
        content = await client.get_guidelines(repo_full_name, token, ref=_state.get("head_sha", "HEAD"))
        return content or "No review guidelines found."

    @tool(description="Find recent PRs that touched the same files. Returns format: PR #N: title (files: ...)")
    async def fetch_recent_related_prs(repo_full_name: str, changed_files: list[str]) -> str:
        return await client.get_recent_prs(repo_full_name, changed_files, token)

    return [
        fetch_pr_details,
        fetch_changed_file,
        fetch_file_diff,
        fetch_related_files,
        fetch_repo_guidelines,
        fetch_recent_related_prs,
    ]
