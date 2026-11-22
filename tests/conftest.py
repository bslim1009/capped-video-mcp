from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from capped_video_mcp import service
from capped_video_mcp.config import Secrets, default_config_text, load_settings
from capped_video_mcp.ledger import KST, Ledger
from capped_video_mcp.providers import base
from capped_video_mcp.providers.mock import MOCK_HOST, MockProvider, mock_transport
from capped_video_mcp.service import App

FIXTURES = Path(__file__).parent / "fixtures"

FAKE_REPLICATE = "r8_TESTTOKEN_abcdefghijklmnop"
FAKE_FAL = "fal-test-key:abcdefghijklmnop"


@dataclass
class FakeClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now = self.now + timedelta(**kw)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 22, 12, 0, 0, tzinfo=KST))


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "config.toml").write_text(default_config_text(), encoding="utf-8")
    return tmp_path


@pytest.fixture
def settings(home: Path):
    s = load_settings(home / "config.toml", environ={})
    # 테스트에서는 수거 주기를 짧게 한다.
    return s.model_copy(update={"limits": s.limits.model_copy(update={"poll_interval_sec": 0.01})})


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(replicate_api_token=FAKE_REPLICATE, fal_key=FAKE_FAL)


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    """재시도 백오프(base._sleep)와 상태 대기 간격만 짧게 한다. 전역 asyncio.sleep 은 건드리지 않는다."""
    async def quick(_s: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(base, "_sleep", quick)
    monkeypatch.setattr(service, "STATUS_WAIT_STEP_SEC", 0.01)


@pytest.fixture
def make_app(home, settings, secrets, clock):
    def _make(provider=None, transport: httpx.MockTransport | None = None, providers=None) -> App:
        p = provider or MockProvider()
        return App(
            settings, secrets, home,
            providers if providers is not None else {"replicate": p, "fal": p},
            httpx.AsyncClient(transport=transport or mock_transport()),
            extra_allowed_hosts=[MOCK_HOST],
            ledger=Ledger(home / "data" / "usage.json", clock=clock),
        )

    return _make


@contextlib.asynccontextmanager
async def running(app: App):
    """서버 lifespan 처럼 백그라운드 수거기를 돌린다."""
    task = asyncio.create_task(app.collector.run_forever())
    try:
        yield app
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
