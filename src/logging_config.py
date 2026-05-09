import logging

from src.config import settings

NOISY_LOGGERS = (
    "LiteLLM",
    "LiteLLM Proxy",
    "LiteLLM Router",
    "httpx",
    "httpcore",
    "openai",
    "instructor",
)


def configure_logging(*, action_mode: bool = False) -> None:
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] DS-Review: %(message)s"
        if action_mode
        else "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    for logger_name in NOISY_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.WARNING)
        logger.handlers.clear()
        logger.propagate = True
