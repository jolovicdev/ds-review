# Privacy

DS-Review is a code review tool. To review a pull request, it sends selected pull request context to the configured DeepSeek API. That includes private repository code when DS-Review is enabled on a private repository.

## GitHub Action Mode

In GitHub Action mode, DS-Review runs inside the user's GitHub Actions workflow.

The action uses:

- the repository's `GITHUB_TOKEN` to read pull request data and post review results
- the repository's `DEEPSEEK_API_KEY` secret to call DeepSeek

DS-Review may send the following review context to DeepSeek:

- pull request title and body
- pull request diff
- changed-file content
- selected related repository files
- repository review guidance, if present

DS-Review does not intentionally log API keys, GitHub tokens, private keys, request headers, full prompts, full diffs, or model responses.

## Self-Hosted GitHub App Mode

In GitHub App mode, DS-Review runs on the operator's server.

The operator is responsible for:

- the GitHub App private key
- the DeepSeek API key
- server logs
- database/storage retention
- access controls
- network and deployment security

## Data Processors

DeepSeek API processing is governed by the DeepSeek terms and policies for the API key used by the operator or repository owner.

GitHub Actions processing is governed by GitHub's terms and the repository/workflow configuration.

## Reporting Issues

Security or privacy issues should be reported through the process in [SECURITY.md](./SECURITY.md).
