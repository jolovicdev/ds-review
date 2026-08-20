import os
import tomllib
from pathlib import Path


def _load_toml(path: str) -> dict:
    p = Path(path)
    if p.exists():
        with open(p, "rb") as f:
            return tomllib.load(f)
    return {}


def _env_or(config: dict, section: str, key: str, env_var: str, default=None):
    val = os.environ.get(env_var)
    if val is not None:
        return val
    return config.get(section, {}).get(key, default)


def _float_env_or(config: dict, section: str, key: str, env_var: str, default: float) -> float:
    return float(_env_or(config, section, key, env_var, default))


def _bool_env_or(config: dict, section: str, key: str, env_var: str, default: bool) -> bool:
    value = _env_or(config, section, key, env_var, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int_env_or(config: dict, section: str, key: str, env_var: str, default: int) -> int:
    return int(_env_or(config, section, key, env_var, default))


def _list_env_or(config: dict, section: str, key: str, env_var: str, default: list[str]) -> list[str]:
    value = _env_or(config, section, key, env_var, default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


class Settings:
    def __init__(self):
        base = _load_toml("ds_review.toml")
        secrets = _load_toml(".secrets.toml")

        models = base.get("models", {})
        server = base.get("server", {})
        pipeline = base.get("pipeline", {})
        gh = secrets.get("github", {})
        ds = secrets.get("deepseek", {})

        self.deepseek_api_key = _env_or({}, "", "", "DEEPSEEK_API_KEY") or ds.get("api_key", "")
        if self.deepseek_api_key and not os.environ.get("DEEPSEEK_API_KEY"):
            os.environ["DEEPSEEK_API_KEY"] = self.deepseek_api_key
        self.fast_model = models.get("fast", "deepseek/deepseek-v4-flash")
        self.pro_model = models.get("pro", "deepseek/deepseek-v4-pro")
        self.temperature = _float_env_or(base, "models", "temperature", "DS_REVIEW_TEMPERATURE", 0.0)

        self.log_level = pipeline.get("log_level", "INFO")

        self.deployment_type = os.environ.get("DEPLOYMENT_TYPE") or gh.get("deployment_type", "app")
        if os.environ.get("GITHUB_ACTIONS"):
            self.deployment_type = "user"

        self.github_token = _env_or({}, "", "", "GITHUB_TOKEN") or gh.get("token", "")
        self.github_app_id = int(_env_or({}, "", "", "GITHUB_APP_ID") or gh.get("app_id", 0))
        self.github_webhook_secret = _env_or({}, "", "", "GITHUB_WEBHOOK_SECRET") or gh.get("webhook_secret", "")
        self.github_private_key = _env_or({}, "", "", "GITHUB_PRIVATE_KEY") or gh.get("private_key", "")

        self.server_host = server.get("host", "0.0.0.0")
        self.server_port = server.get("port", 8765)

        # Triggers
        triggers = base.get("triggers", {})
        self.trigger_on_pull_request_open = triggers.get("on_pull_request_open", True)
        self.trigger_on_pull_request_sync = triggers.get("on_pull_request_sync", True)
        self.trigger_on_pull_request_reopen = triggers.get("on_pull_request_reopen", True)
        self.trigger_on_mention = triggers.get("on_mention", True)
        self.mention_author_associations = [
            item.upper()
            for item in _list_env_or(
                base,
                "triggers",
                "mention_author_associations",
                "DS_REVIEW_MENTION_AUTHOR_ASSOCIATIONS",
                ["OWNER", "MEMBER", "COLLABORATOR"],
            )
        ]

        # Auto behavior
        auto = base.get("auto", {})
        self.auto_approve_enabled = _bool_env_or(
            base, "review", "auto_approve_enabled", "DS_REVIEW_AUTO_APPROVE_ENABLED", False
        )
        if "approve_enabled" in auto:
            self.auto_approve_enabled = bool(auto["approve_enabled"])
        self.auto_approve_no_critical = bool(auto.get("approve_no_critical", True))
        self.auto_approve_no_high = bool(auto.get("approve_no_high", True))

        self.min_severity = str(
            _env_or(base, "review", "min_severity", "DS_REVIEW_MIN_SEVERITY", "P2")
        ).upper()
        self.max_findings = _int_env_or(base, "review", "max_findings", "DS_REVIEW_MAX_FINDINGS", 12)
        self.require_suggestion_for_p2 = _bool_env_or(
            base, "review", "require_suggestion_for_p2", "DS_REVIEW_REQUIRE_SUGGESTION_FOR_P2", False
        )
        self.inline_comments_enabled = _bool_env_or(
            base, "review", "inline_comments_enabled", "DS_REVIEW_INLINE_COMMENTS_ENABLED", True
        )
        self.summary_comment_enabled = _bool_env_or(
            base, "review", "summary_comment_enabled", "DS_REVIEW_SUMMARY_COMMENT_ENABLED", True
        )
        self.check_runs_enabled = _bool_env_or(
            base, "review", "check_runs_enabled", "DS_REVIEW_CHECK_RUNS_ENABLED", False
        )
        fail_check_on = _env_or(base, "review", "fail_check_on", "DS_REVIEW_FAIL_CHECK_ON", ["P0"])
        if isinstance(fail_check_on, str):
            fail_check_on = [item.strip() for item in fail_check_on.split(",")]
        self.fail_check_on = [str(item).upper() for item in fail_check_on if str(item).strip()]


settings = Settings()
