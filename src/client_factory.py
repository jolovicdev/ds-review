from src.config import settings
from src.github_client import GitHubClient


def make_github_client() -> GitHubClient:
    if settings.deployment_type == "user":
        return GitHubClient(token=settings.github_token)
    return GitHubClient(
        app_id=settings.github_app_id,
        private_key=settings.github_private_key,
    )
