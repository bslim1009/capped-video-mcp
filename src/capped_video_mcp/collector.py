"""백그라운드 수거: 진행 중인 작업을 주기적으로 조회하고, 완료되면 바로 내려받는다.

Replicate 는 결과를 완료 1시간 뒤 지우므로, 에이전트가 상태를 묻지 않아도 영상을 잃지 않게 한다.
서버가 다시 시작되면 ledger 에서 끝나지 않은 작업을 읽어 이어서 수거한다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import App

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, app: App, interval_sec: float):
        self._app = app
        self._interval = interval_sec
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """새 작업이 생기면 다음 주기를 기다리지 않고 바로 한 번 돈다."""
        self._wake.set()

    async def poll_once(self) -> None:
        for job_id in self._app.ledger.collectable_jobs():
            try:
                await self._app.process_job(job_id)
            except Exception:
                log.exception("작업 수거 중 오류: %s", job_id)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("수거 주기 오류")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                pass
            self._wake.clear()
