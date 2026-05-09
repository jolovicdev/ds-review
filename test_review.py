"""Run DS-Review against a PR from the command line."""

import argparse
import asyncio
import logging

from src.config import settings
from src.pipeline import run_review_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DS-Review against a pull request.")
    parser.add_argument("repo", help="Repository full name, for example owner/repo")
    parser.add_argument("pr_number", type=int, help="Pull request number")
    parser.add_argument("--app", type=int, metavar="INSTALLATION_ID", help="Use GitHub App installation mode")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.app is not None:
        settings.deployment_type = "app"
        installation_id = args.app
    else:
        settings.deployment_type = "user"
        installation_id = 0

    result = await run_review_pipeline(args.repo, args.pr_number, installation_id)
    if result is None:
        print("No review posted.")
        return
    print(f"Review completed with {len(result.get('comments', []))} model findings.")


if __name__ == "__main__":
    asyncio.run(main())
