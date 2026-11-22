"""더 비싼 대체 모델로의 제공처 전환은 사용자가 고른다.

- 대체 모델이 같거나 싸면 묻지 않고 넘어간다.
- 더 비싸면 사용자에게 묻고, 승인했을 때만 넘어간다.
- 확인 요청을 지원하지 않는 클라이언트면 넘어가지 않고, 오류로 대체 모델과 가격을 알려 준다 (과금 없음).
"""

import asyncio
import json

import pytest
from mcp import Client
from mcp.types import ElicitResult

from capped_video_mcp import ledger as L
from capped_video_mcp import service
from capped_video_mcp.providers.base import ProviderError
from capped_video_mcp.providers.mock import MockProvider
from capped_video_mcp.server import create_server
from capped_video_mcp.service import AppError


class Rejecting(MockProvider):
    async def create_task(self, inp):
        raise ProviderError("모델 점검 중")


def asker(answer):
    calls = []

    async def confirm(message):
        calls.append(message)
        return answer
    confirm.calls = calls
    return confirm


def events(app):
    return [json.loads(x)["event"] for x in app.audit.path.read_text(encoding="utf-8").splitlines()]


async def test_pricier_failover_declined_is_not_charged(make_app):
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})
    no = asker(False)
    with pytest.raises(AppError, match="거절했습니다. 과금되지 않았습니다"):
        await app.generate("a boat", confirm=no)
    assert len(no.calls) == 1 and not fal.payloads
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED and job["model"] == "seedance-2.5"
    assert app.usage()["today_usd"] == 0 and "failover_declined" in events(app)


@pytest.mark.parametrize("confirm", [None, asker(None)], ids=["no-callback", "client-without-elicitation"])
async def test_pricier_failover_without_user_choice_stops_with_hint(make_app, confirm):
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})
    with pytest.raises(AppError) as e:
        await app.generate("a boat", confirm=confirm)
    msg = str(e.value)
    assert "과금되지 않았습니다" in msg and 'model="seedance-2.5-fal"' in msg and "$1.10" in msg and "$0.51" in msg
    assert not fal.payloads
    assert app.ledger.summary()["today_usd"] == 0 and "failover_needs_choice" in events(app)


async def test_cheaper_failover_is_automatic(make_app):
    """fal($1.10) → Replicate($0.514) 는 더 싸므로 묻지 않는다."""
    replicate = MockProvider()
    app = make_app(providers={"replicate": replicate, "fal": Rejecting()})
    ask = asker(False)
    r = await app.generate("a boat", model="seedance-2.5-fal", confirm=ask)
    assert r["model"] == "seedance-2.5" and r["failover_from"] == "seedance-2.5-fal"
    assert ask.calls == [] and replicate.payloads
    assert r["estimated_usd"] == pytest.approx(0.514)


async def test_reservation_kept_alive_while_waiting_for_answer(make_app, clock, monkeypatch):
    """사용자가 늦게 답해도(5분 이상) 수거기가 예약을 회수하지 않는다."""
    monkeypatch.setattr(service, "RESERVED_HEARTBEAT_SEC", 0.01)
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})

    async def slow_yes(message):
        (job_id,) = app.ledger.jobs()
        for _ in range(4):                        # 3분씩 4번 = 12분
            clock.advance(minutes=3)
            await asyncio.sleep(0.05)
            await app.process_job(job_id)
        return True

    r = await app.generate("a boat", confirm=slow_yes)
    assert r["model"] == "seedance-2.5-fal" and fal.payloads
    assert "reserved_expired" not in events(app)


async def test_reservation_lost_while_waiting_stops(make_app, monkeypatch):
    monkeypatch.setattr(service, "RESERVED_HEARTBEAT_SEC", 0.01)
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})

    async def never(message):
        (job_id,) = app.ledger.jobs()
        app.ledger.transition(job_id, {L.RESERVED}, status=L.UNKNOWN)    # 다른 프로세스가 회수
        await asyncio.sleep(3600)

    with pytest.raises(AppError, match="사용자 확인을 기다리는 동안 작업 예약이 만료"):
        await app.generate("a boat", confirm=never)
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED and not fal.payloads


async def test_cancel_while_waiting_is_not_charged(make_app):
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})

    async def forever(message):
        await asyncio.sleep(3600)

    task = asyncio.create_task(app.generate("a boat", confirm=forever))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED and app.usage()["today_usd"] == 0


async def test_confirm_error_is_treated_as_no_answer(make_app):
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})

    async def broken(message):
        raise RuntimeError("client went away")

    with pytest.raises(AppError, match='model="seedance-2.5-fal"'):
        await app.generate("a boat", confirm=broken)
    assert not fal.payloads


async def test_pricier_failover_via_elicitation(make_app):
    seen = []

    async def accept(context, params):
        seen.append(params.message)
        return ElicitResult(action="accept", content={"proceed": True})

    fal = MockProvider()
    server = create_server(make_app(providers={"replicate": Rejecting(), "fal": fal}))
    async with Client(server, elicitation_callback=accept, mode="legacy") as c:
        r = await c.call_tool("generate_video", {"prompt": "a boat"})
    assert not r.is_error and r.structured_content["model"] == "seedance-2.5-fal"
    assert len(seen) == 1 and "대체 모델 seedance-2.5-fal" in seen[0] and "$1.10" in seen[0]


async def test_pricier_failover_without_elicitation_via_protocol(make_app):
    fal = MockProvider()
    server = create_server(make_app(providers={"replicate": Rejecting(), "fal": fal}))
    async with Client(server) as c:            # 콜백 없음 → 확인 요청 능력 미선언
        r = await c.call_tool("generate_video", {"prompt": "a boat"})
    assert r.is_error and 'model="seedance-2.5-fal"' in r.content[0].text
    assert not fal.payloads


# ── 리뷰 반영: 실행될 수 없는 전환은 묻지 않는다 ──

async def test_over_budget_fallback_is_not_asked(make_app, settings):
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})
    app.settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"daily_max_usd": 0.6})})
    ask = asker(True)
    with pytest.raises(AppError, match="남은 예산을 넘어 전환하지 않았습니다"):
        await app.generate("a boat", confirm=ask)      # Replicate $0.514 는 예산 안, fal $1.10 은 밖
    assert ask.calls == [] and not fal.payloads


async def test_reservation_lost_during_upload_is_not_a_failover(make_app, tmp_path):
    png = tmp_path / "f.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    holder = {}

    class Expiring(MockProvider):
        async def upload_file(self, media):
            (job_id,) = holder["app"].ledger.jobs()
            holder["app"].ledger.transition(job_id, {L.RESERVED}, status=L.UNKNOWN)
            return await super().upload_file(media)

    fal = MockProvider()
    app = holder["app"] = make_app(providers={"replicate": Expiring(), "fal": fal})
    ask = asker(True)
    with pytest.raises(AppError, match="예약이 만료돼"):
        await app.generate("a boat", image_path=str(png), confirm=ask)
    assert ask.calls == [] and not fal.uploads and not fal.payloads
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED


async def test_approved_but_budget_gone_reports_why(make_app, settings):
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})

    async def yes_but_budget_shrinks(message):
        app.settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"daily_max_usd": 0.6})})
        return True

    with pytest.raises(AppError, match="전환하려 했지만 남은 예산이 부족하거나"):
        await app.generate("a boat", confirm=yes_but_budget_shrinks)
    assert not fal.payloads and "failover_blocked" in events(app)
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED and app.usage()["today_usd"] == 0


async def test_unanswered_question_times_out(make_app, monkeypatch):
    monkeypatch.setattr(service, "FAILOVER_CONFIRM_TIMEOUT_SEC", 0.05)
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})

    async def silent(message):
        await asyncio.sleep(3600)

    with pytest.raises(AppError, match='model="seedance-2.5-fal"'):
        await app.generate("a boat", confirm=silent)
    assert not fal.payloads and app.ledger.summary()["pending_jobs"] == 0
