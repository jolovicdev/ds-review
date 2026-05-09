from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger("ds-review")

ReviewKey = tuple[str, int]
IsCurrent = Callable[[], bool]
ReviewDispatch = Callable[[str, int, int, IsCurrent], Awaitable[None]]
IdleCallback = Callable[[str, int], None]


@dataclass
class _ReviewSlot:
    generation: int
    installation_id: int
    task: asyncio.Task[None] | None = None


class ReviewCoordinator:
    def __init__(self, dispatch: ReviewDispatch, *, on_idle: IdleCallback | None = None) -> None:
        self._dispatch = dispatch
        self._on_idle = on_idle
        self._slots: dict[ReviewKey, _ReviewSlot] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, repo: str, pr_number: int, installation_id: int) -> int:
        key = (repo, pr_number)
        async with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                slot = _ReviewSlot(generation=0, installation_id=installation_id)
                self._slots[key] = slot

            slot.generation += 1
            slot.installation_id = installation_id
            generation = slot.generation

            if slot.task is None or slot.task.done():
                slot.task = asyncio.create_task(self._run(repo, pr_number), name=f"review:{repo}#{pr_number}")
                logger.info("Queued review repo=%s pr=%d generation=%d", repo, pr_number, generation)
            else:
                logger.info("Queued newer review repo=%s pr=%d generation=%d", repo, pr_number, generation)

            return generation

    async def wait_idle(self) -> None:
        while True:
            async with self._lock:
                tasks = [slot.task for slot in self._slots.values() if slot.task is not None]
            if not tasks:
                return
            await asyncio.gather(*tasks)

    def _is_current(self, key: ReviewKey, generation: int) -> bool:
        slot = self._slots.get(key)
        return slot is not None and slot.generation == generation

    async def _run(self, repo: str, pr_number: int) -> None:
        key = (repo, pr_number)
        while True:
            async with self._lock:
                slot = self._slots.get(key)
                if slot is None:
                    return
                generation = slot.generation
                installation_id = slot.installation_id

            def is_current(key: ReviewKey = key, generation: int = generation) -> bool:
                return self._is_current(key, generation)

            try:
                await self._dispatch(repo, pr_number, installation_id, is_current)
            except Exception:
                logger.exception("Review dispatch failed repo=%s pr=%d generation=%d", repo, pr_number, generation)

            async with self._lock:
                slot = self._slots.get(key)
                if slot is None:
                    return
                if slot.generation == generation:
                    self._slots.pop(key, None)
                    if self._on_idle is not None:
                        self._on_idle(repo, pr_number)
                    logger.info("Review queue drained repo=%s pr=%d generation=%d", repo, pr_number, generation)
                    return

                logger.info(
                    "Review generation superseded repo=%s pr=%d finished_generation=%d latest_generation=%d",
                    repo,
                    pr_number,
                    generation,
                    slot.generation,
                )
