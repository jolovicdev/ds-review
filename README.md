# DS-Review

Self-hosted AI code reviewer for GitHub pull requests, powered by [Blackgeorge](https://jolovicdev.github.io/blackgeorge/) and DeepSeek-V4.

DS-Review reviews what changed and why it matters. It starts from the PR diff, pulls in related code and repo guidance for context, and publishes only findings it can tie back to changed lines. Run it as a composite GitHub Action or as your own self-hosted GitHub App webhook server.

Posts reviews as `@ds-review[bot]` with severity badges (P0–P2), inline comments anchored to changed lines, and applyable `suggestion` code blocks.

DS-Review is BYOK: bring your own DeepSeek API key. This repository does not provide a hosted review service.

## What You Get

**Review quality**

- **6-stage review pipeline**: context collection, hypothesis generation, evaluation, parallel specialists, summarization, self-reflection
- **Diff-scoped but context-aware review**: changed hunks first, then related files, repo guidance, and recent PRs
- **Strict publishing threshold**: P0/P1/P2 findings by default, with P3/style noise suppressed
- **Deterministic Markdown**: model output is structured data; DS-Review renders predictable GitHub comments

**GitHub workflow**

- **Auto-review** on `pull_request.opened`, `synchronize`, and `reopened`
- **On-demand review** by commenting `@ds-review` on a PR
- **Threaded follow-up** under inline comments, with optional `@ds-review recheck`
- **Persistent comments** that update existing summaries instead of spraying duplicates
- **Check runs + auto-approve** as opt-in controls

**Runtime behavior**

- **One active webhook review per PR**: rapid commit pushes are coalesced into the latest head
- **Stale-head guard**: a review that finished against an old commit is discarded instead of posted
- **Crash recovery** for pending webhook reviews and last-posted review state

## Quick start

Pick the path that fits how you work.

### Path A: GitHub Action (composite `uv`, no server)

Drop a workflow file into your repo. You provide your own DeepSeek key as a secret. GitHub runs the review.
Use this path instead of the VPS/GitHub App deployment, not alongside it.

```yaml
# .github/workflows/ds-review.yml
name: DS-Review Action
on:
  pull_request:
    types: [opened, synchronize, reopened]
  issue_comment:
    types: [created]

permissions:
  pull-requests: write
  contents: read
  issues: write

jobs:
  review:
    runs-on: ubuntu-latest
    if: >
      github.event.sender.type != 'Bot' &&
      (
        github.event_name == 'pull_request' ||
        (
          github.event.issue.pull_request &&
          contains(github.event.comment.body, '@ds-review') &&
          contains('OWNER,MEMBER,COLLABORATOR', github.event.comment.author_association)
        )
      )
    steps:
      - name: DS-Review
        uses: jolovicdev/ds-review@v0.1.3
        with:
          deepseek_api_key: ${{ secrets.DEEPSEEK_API_KEY }}
          github_token: ${{ secrets.GITHUB_TOKEN }}
```

Add `DEEPSEEK_API_KEY` to your repo secrets: **Settings → Secrets and variables → Actions → New repository secret**. `GITHUB_TOKEN` is auto-provided by GitHub.

Open a PR. Review appears within minutes.

The GitHub Action path is intentionally a composite `uv` action. No Docker image is built or published for this path.
That makes the action easy to inspect and publish from this repo. The tradeoff is a little more cold-start setup than a prebuilt Docker or JavaScript action.
A prebuilt Docker/GHCR image may be published later for self-hosted deployments; the Action path stays composite for now.

If you enable `check_runs_enabled = true`, add `checks: write` to the workflow permissions.

For public repositories, the normal `pull_request` event does not expose repository secrets to untrusted fork PRs. That is the safer default. Avoid switching this workflow to `pull_request_target` unless you understand the security tradeoff.
Comment-triggered reviews are gated to `OWNER`, `MEMBER`, and `COLLABORATOR` by default so random issue commenters cannot burn your model budget.

Privacy: DS-Review sends selected PR context to DeepSeek for review, including private repository code when enabled on private repositories. It does not intentionally log secrets, tokens, prompts, full diffs, or model responses. See [PRIVACY.md](./PRIVACY.md).

### Path B: Self-hosted GitHub App (Docker)

Run your own GitHub App server. Requires a domain pointing to your VPS. Your server uses your DeepSeek key, so do not offer a public hosted App unless you also add billing, quotas, or an allowlist.

```bash
git clone https://github.com/jolovicdev/ds-review && cd ds-review
cp .secrets_template.toml .secrets.toml
# edit .secrets.toml — fill in [deepseek] and [github] sections
docker compose up -d
```

Set up TLS with Caddy (auto-LetsEncrypt, zero config):

```bash
sudo apt install -y caddy
sudoedit /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Use this Caddyfile:

```caddyfile
ds-review.your-domain.com {
    reverse_proxy localhost:8765
}
```

Set your [GitHub App](#creating-a-github-app) webhook URL to `https://ds-review.your-domain.com/webhook`.

The current Docker path builds the image locally from this repo. A future `ghcr.io` image would only save that build step: users would pull a pinned image, mount their own `.secrets.toml`, and still pay for their own DeepSeek usage.

### Path C: Local development

```bash
git clone https://github.com/jolovicdev/ds-review && cd ds-review
uv sync
cp .secrets_template.toml .secrets.toml
# edit .secrets.toml

# Terminal 1: webhook server
uv run python -m src.main

# Terminal 2: smee proxy (forwards GitHub webhooks to localhost)
npx smee-client --url https://smee.io/YOUR_CHANNEL --target http://localhost:8765/webhook
```

Test without running a server:

```bash
uv run python test_review.py owner/repo 42
```

### Path D: CLI test run

```bash
# User mode (PAT from .secrets.toml)
uv run python test_review.py owner/repo 42

# App mode (GitHub App JWT)
uv run python test_review.py owner/repo 42 --app 12345678
```

## Creating a GitHub App

Needed for Paths B and C (webhook server). Not needed for Path A (GitHub Action).

1. Go to [GitHub App settings](https://github.com/settings/apps/new)
2. Set **Webhook URL** to your server (or smee.io URL for local dev)
3. Set **Webhook secret** to a random string — copy it to `github.webhook_secret` in `.secrets.toml`
4. **Repository permissions:**
   - Contents: Read-only, for diffs and related files
   - Pull requests: Read & Write, for PR reviews and inline comments
   - Issues: Read & Write, for PR timeline comments and reactions
   - Checks: Read & Write, only if `check_runs_enabled = true`
   - Metadata: Read-only, auto-selected by GitHub
5. **Subscribe to events:** Pull request, Issue comment, Pull request review comment
6. Create the App, then **Generate a private key** — paste the full PEM block into `github.private_key` in `.secrets.toml`
7. Note the **App ID** (numeric, top of settings page) — put it in `github.app_id` in `.secrets.toml`
8. **Install the App** on your repositories from the Install App tab

## Configuration

Two files. One committed, one secret.

### `ds_review.toml` — committed, non-sensitive defaults

```toml
[models]
fast = "deepseek/deepseek-v4-flash"   # context, summarizer, reflector
pro = "deepseek/deepseek-v4-pro"      # hypotheses, evaluator, specialists
temperature = 0.0                     # DeepSeek recommends 0.0 for coding/math

[server]
host = "0.0.0.0"
port = 8765

[pipeline]
log_level = "INFO"

[triggers]
on_pull_request_open = true
on_pull_request_sync = true
on_pull_request_reopen = true
on_mention = true
mention_author_associations = ["OWNER", "MEMBER", "COLLABORATOR"]

[review]
auto_approve_enabled = false     # opt-in: approve PRs with no P0/P1 findings
inline_comments_enabled = true   # publish anchored review comments
summary_comment_enabled = true   # include a review summary body
check_runs_enabled = false       # opt-in: publish a DS-Review GitHub check run
fail_check_on = ["P0"]           # check conclusion is failure for these severities
min_severity = "P2"              # publish P0/P1/P2, suppress P3 by default
max_findings = 12
require_suggestion_for_p2 = false
```

### `.secrets.toml` — gitignored, your actual secrets

```toml
[deepseek]
api_key = "sk-..."              # https://platform.deepseek.com/api_keys

[github]
deployment_type = "app"         # "user" for PAT/Actions, "app" for GitHub App
token = ""                      # user mode: PAT or GITHUB_TOKEN from Actions
app_id = 123456                 # app mode: numeric App ID
webhook_secret = "random"       # app mode: webhook secret
private_key = """               # app mode: paste full PEM block
-----BEGIN RSA PRIVATE KEY-----
...
-----END RSA PRIVATE KEY-----"""
```

Environment variables override TOML values. In GitHub Actions, `DEEPSEEK_API_KEY` and `GITHUB_TOKEN` are read from `secrets` and the environment directly.

## Cost Model

DS-Review does not include a hosted backend.

| Deployment | Who runs it | Who pays DeepSeek |
|------------|-------------|-------------------|
| GitHub Action | The repository installing the action | That repository owner |
| Self-hosted GitHub App | The operator running the server | That operator |
| Future GHCR image | The operator running the container | That operator |

GHCR is only image hosting. It makes Docker deployment faster and easier to pin, but it does not change API-key ownership. Public GitHub Packages/GHCR packages are free under current GitHub billing docs; private packages use plan quotas unless GitHub's current Container Registry policy applies.

## What Blackgeorge Does Here

Blackgeorge is the orchestration layer. DS-Review uses it to run the staged worker flow, route tool calls to GitHub, keep structured Pydantic outputs between workers, run the specialist pair in parallel, and persist run state under `.blackgeorge/` for webhook deployments.

DeepSeek provides the reasoning model. Blackgeorge provides the agent runtime around it: tools, workers, flow steps, structured outputs, retries, and the local state store.

## Context Collection

DS-Review uses DeepSeek-V4's long context aggressively, but the context is ordered. The collector fetches changed hunks first, then full changed files when useful, related import/caller files, repo guidance, and recent PRs touching the same files. That keeps the review anchored to the diff while still giving the model enough surrounding code to understand contracts.

## Review Lifecycle

Webhook mode treats each `repo#pr` as a single review lane.

1. A PR event saves a pending review item, then enqueues that PR.
2. If another commit arrives while review is running, DS-Review records that a newer generation exists instead of starting a second active review.
3. The in-flight review finishes its analysis, then re-checks the PR head before publishing anything.
4. If the head changed, the stale result is dropped and the queue immediately runs the latest generation.
5. `last_commit_sha` is saved only after DS-Review posts against the same head it reviewed.

This avoids stale reviews overwriting newer results when developers push several commits in quick succession.

## Crash Recovery

Webhook mode stores review state in `.blackgeorge/review_state.json`. Today that means:

- pending PR reviews queued from webhooks
- the GitHub App installation id needed to replay them
- the last reviewed commit SHA per PR
- the last DS-Review summary review id
- optional summary comment id for older state entries

On process restart, pending PR reviews are replayed through the same per-PR review lane. In-flight LLM calls themselves are not resumed mid-token; the PR review is restarted from the saved pending item.

## On-demand review

Comment `@ds-review` on any PR to trigger a review.

Use the GitHub App slug, not the display name, if you changed it during app creation. For an app URL like `https://github.com/apps/ds-review`, the trigger is `@ds-review`. The `[bot]` suffix is how GitHub displays the app's bot account as an author; you usually do not need to type it.

By default, comment-triggered reviews and threaded replies only run for `OWNER`, `MEMBER`, or `COLLABORATOR` author associations. This protects BYOK deployments from public comment spam. To widen that, set `mention_author_associations` in `ds_review.toml` or `DS_REVIEW_MENTION_AUTHOR_ASSOCIATIONS`.

Additional commands (append to `@ds-review`):
- `review` — full review (default if no command given)
- (extensible via worker prompts in `src/workers.py`)

## Threaded follow-up

For GitHub App / webhook deployments, DS-Review also listens for replies under its inline review comments.

Reply naturally to keep the conversation going:

```text
Are you sure this can happen?
```

Use `@ds-review recheck` when you pushed a fix and want the bot to re-evaluate that specific finding against the current PR head:

```text
@ds-review recheck
```

The bot replies in the same review thread. It ignores bot-authored replies to avoid loops, and it only answers threads whose parent comment belongs to DS-Review.

Recheck is optional. If `on_pull_request_sync = true`, pushing a fix already triggers a full review on the new commit. Use `@ds-review recheck` when you want targeted verification and thread cleanup for one specific finding.

## Review formatting

The model decides what is wrong and returns plain structured fields; deterministic code renders all GitHub Markdown.

- Summary comments use shields.io severity badges (`P0` through `P2`) with short finding titles and full explanatory text.
- Inline comments start with the same shields.io severity badge, then a punchy finding title and supporting paragraph.
- Concrete fixes use GitHub `suggestion` fences when they can be applied cleanly.
- If the model lands a comment a few lines away from the real added line, DS-Review snaps it to the nearest changed line in the same file.
- If a finding cannot be anchored to the diff, it stays in the summary instead of being posted as a plain timeline comment.

### Real output examples

Clean PR ([example](https://github.com/jolovicdev/cashet/pull/21)):

![Clean DS-Review summary](assets/clean-review-summary.png)

```md
## DS-Review

No blocking issues found.

Reviewed the changed diff and related call paths. No actionable bugs, security issues, or meaningful performance regressions were found.
```

Blocking review summary ([example](https://github.com/jolovicdev/cashet/pull/20)):

![DS-Review blocking summary](assets/review-summary-finding.png)

Inline diff comment with suggestion ([example](https://github.com/jolovicdev/cashet/pull/20)):

![DS-Review inline finding and suggestion](assets/inline-finding-suggestion.png)

````md
P1 High cache option overrides explicit false to true

The shared helper uses data.get("cache") or True, which treats a client-supplied false as falsy and replaces it with True. The original inline code used data.get("cache", True), which correctly preserved an explicit False.

```suggestion
    cache = data.get("cache", True)
```
````

Resolved recheck ([example](https://github.com/jolovicdev/cashet/pull/18)):

```md
Recheck: resolved. The `duration_from_seconds` function now uses `if seconds is not None` instead of truthiness, so passing `0` returns `timedelta(0)` instead of `None`.
```

## Severity levels

| Marker | Meaning |
|--------|---------|
| **`P0`** | Critical — exploitable vulnerabilities, data loss, crashes |
| **`P1`** | High — bugs, auth bypass, data leaks |
| **`P2`** | Medium — performance issues, maintainability problems |
| **`P3`** | Low — suppressed by default; DS-Review avoids style nits |

## Architecture

```mermaid
flowchart TD
    event[GitHub webhook or Action event] --> dispatch[Webhook or Action entrypoint]
    dispatch --> queue[Per-PR review queue]
    queue --> snapshot[Capture PR head and diff snapshot]
    snapshot --> flow[Blackgeorge review pipeline]
    flow --> render[Deterministic Markdown renderer]
    render --> guard{PR head still current?}
    guard -- yes --> publish[Publish review, reactions, check run]
    guard -- no --> requeue[Drop stale output and queue latest head]
    requeue --> queue
    publish --> state[Persist last review id and head SHA]
    state --> recovery[Crash recovery replay]
    recovery --> queue
```

The critical rule is that analysis and publishing are separate. DS-Review captures the PR head before analysis, reviews that snapshot, then checks the PR head again before posting. If the branch moved, the stale result is dropped and the latest head is queued.

## Development

```bash
uv sync --dev
uv run pytest tests/ -v
```

Action mode uses `src.action_runner`, not the webhook server. To smoke-test it against a real PR, save a GitHub
`pull_request` event payload locally and run:

```bash
GITHUB_EVENT_NAME=pull_request \
GITHUB_EVENT_PATH=/tmp/pull_request_event.json \
GITHUB_TOKEN=ghp_or_github_token \
DEEPSEEK_API_KEY=sk-your-key \
uv run python -m src.action_runner
```

That path posts as the workflow token user, not as the GitHub App bot. Threaded review-comment replies are webhook/App
only.

## Release

The current release is tagged `v0.1.3`. For future releases:

```bash
git tag -a v0.1.3 -m "v0.1.3"
git push origin v0.1.3
```

Prefer version tags in examples and production installs. Keep `@master` only for bleeding-edge testing.

## License

MIT — see [LICENSE](LICENSE).
