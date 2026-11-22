import asyncio
import json

import httpx
import pytest

from capped_video_mcp import ledger as L
from capped_video_mcp.policy import PolicyError
from capped_video_mcp.providers.base import CreateUncertainError, ProviderError, TaskState
from capped_video_mcp.providers.mock import MOCK_VIDEO_BYTES, MockProvider
from capped_video_mcp.service import AppError

from .conftest import FAKE_FAL, FAKE_REPLICATE, running


async def poll(app, times: int = 3) -> None:
    for _ in range(times):
        await app.collector.poll_once()


# ── 기본 흐름 ──

async def test_full_flow_generate_then_status(make_app, home):
    app = make_app()
    async with running(app):
        r = await app.generate("a red paper boat")
        assert r["status"] == L.QUEUED and r["provider"] == "replicate"
        assert r["estimated_usd"] == pytest.approx(0.514)
        s = await app.status(r["job_id"], wait_seconds=60)
    assert s["status"] == L.SUCCEEDED
    assert s["file"].endswith(f"{r['job_id']}.mp4")
    assert (home / "output" / f"{r['job_id']}.mp4").exists()


async def test_collector_downloads_without_status_call(make_app, home):
    app = make_app()
    r = await app.generate("a boat")
    await poll(app)
    job = app.ledger.get(r["job_id"])
    assert job["status"] == L.SUCCEEDED
    assert (home / job["file"]).exists()


async def test_status_does_not_touch_remote_without_collector(make_app):
    """도구는 기록만 읽는다. 수거기가 없으면 상태가 바뀌지 않는다 (부수 작업은 수거기만 한다)."""
    mp = MockProvider()
    app = make_app(provider=mp)
    r = await app.generate("a boat")
    s = await app.status(r["job_id"], wait_seconds=0)
    assert s["status"] == L.QUEUED
    assert mp._calls[app.ledger.get(r["job_id"])["remote_id"]] == 0


async def test_fal_model_routes_to_fal(make_app):
    replicate, fal = MockProvider(), MockProvider()
    app = make_app(providers={"replicate": replicate, "fal": fal})
    r = await app.generate("a boat", model="seedance-2.5-fal", resolution="720p", duration_sec=4)
    assert r["provider"] == "fal"
    assert r["estimated_usd"] == pytest.approx(0.473 * 4)
    assert fal._prompts and not replicate._prompts


async def test_failed_job_still_counted(make_app):
    app = make_app()
    r = await app.generate("please FAIL")
    await poll(app)
    job = app.ledger.get(r["job_id"])
    assert job["status"] == L.FAILED and "의도된 실패" in job["error"]
    assert app.usage()["today_usd"] == pytest.approx(0.514)


async def test_gone_result_is_lost(make_app):
    app = make_app()
    r = await app.generate("LOST video")
    await poll(app)
    assert app.ledger.get(r["job_id"])["status"] == L.LOST


async def test_resume_after_restart(make_app):
    app1 = make_app()
    r = await app1.generate("a boat")
    await app1.aclose()
    app2 = make_app()  # 새 프로세스: 새 MockProvider, 같은 ledger
    await app2.collector.poll_once()
    assert app2.ledger.get(r["job_id"])["status"] == L.SUCCEEDED


# ── 시간 초과 (리뷰 #2) ──

async def test_timeout_cancels_only_while_running(make_app, clock):
    mp = MockProvider(steps_to_finish=10_000)
    app = make_app(provider=mp)
    r = await app.generate("slow")
    clock.advance(minutes=61)
    await app.collector.poll_once()
    job = app.ledger.get(r["job_id"])
    assert job["status"] == L.FAILED and "시간 초과" in job["error"]
    assert mp.cancelled == [job["remote_id"]]


class AlwaysDone(MockProvider):
    async def get_task(self, job):
        return TaskState("succeeded", video_url="https://mock.invalid/ok/x.mp4")


async def test_finished_result_after_timeout_is_still_downloaded(make_app, clock, home):
    """세션을 닫았다가 61분 뒤 다시 열어도, 원격이 끝나 있으면 결과를 받는다."""
    app = make_app(provider=AlwaysDone())
    r = await app.generate("a boat")
    clock.advance(minutes=61)
    await app.collector.poll_once()
    job = app.ledger.get(r["job_id"])
    assert job["status"] == L.SUCCEEDED
    assert (home / job["file"]).exists()


# ── 생성 중 취소·오류 (리뷰 #1, #3, #7, #8, #13) ──

class SlowCreate(MockProvider):
    async def create_task(self, inp):
        await asyncio.sleep(3600)


async def test_cancel_during_create_does_not_leak_reserved(make_app, clock):
    app = make_app(provider=SlowCreate())
    task = asyncio.create_task(app.generate("a boat"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.UNKNOWN          # 요청이 나갔을 수 있다 → 비용 포함
    assert app.usage()["today_usd"] == pytest.approx(0.514)
    assert app.usage()["pending_jobs"] == 1    # 한동안은 진행 중으로 센다
    clock.advance(minutes=61)
    assert app.usage()["pending_jobs"] == 0    # 영구히 막히지 않는다


async def test_stale_reserved_becomes_unknown(make_app, settings, clock):
    """프로세스가 생성 도중 죽어 reserved 로 남은 작업을 정리한다."""
    from capped_video_mcp.policy import build_request

    app = make_app()
    job_id = app.ledger.reserve(build_request(settings, "a"), settings.limits)
    clock.advance(minutes=6)
    await app.collector.poll_once()
    assert app.ledger.get(job_id)["status"] == L.UNKNOWN


class StubCreate(MockProvider):
    def __init__(self, exc: Exception):
        super().__init__()
        self.exc = exc

    async def create_task(self, inp):
        raise self.exc


async def test_create_rejected_is_not_charged(make_app):
    app = make_app(provider=StubCreate(ProviderError("replicate: 요청 거부(HTTP 422).")))
    with pytest.raises(AppError, match="422"):
        await app.generate("a boat")
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED
    assert app.usage()["today_usd"] == 0


async def test_create_uncertain_is_charged_and_keeps_remote_id(make_app):
    app = make_app(provider=StubCreate(CreateUncertainError("fal: URL 문제", remote_id="req-1")))
    with pytest.raises(AppError, match="다시 보내지 마세요"):
        await app.generate("a boat")
    (job_id,) = app.ledger.jobs()
    job = app.ledger.get(job_id)
    assert job["status"] == L.UNKNOWN and job["remote_id"] == "req-1"
    u = app.usage()
    assert u["today_usd"] == pytest.approx(0.514) and u["unknown_jobs"] == [job_id]


async def test_unexpected_create_error_is_unknown(make_app):
    app = make_app(provider=StubCreate(RuntimeError("boom")))
    with pytest.raises(AppError, match="job_id="):
        await app.generate("a boat")
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.UNKNOWN


async def test_ledger_failure_after_create_says_do_not_retry(make_app, monkeypatch):
    app = make_app()
    real_update = app.ledger.update

    def failing_update(job_id, **fields):
        if fields.get("status") == L.QUEUED:
            raise PermissionError("locked by antivirus")
        return real_update(job_id, **fields)

    monkeypatch.setattr(app.ledger, "update", failing_update)
    with pytest.raises(AppError, match="다시 보내지 마세요") as e:
        await app.generate("a boat")
    assert "remote_id=mock-1" in str(e.value)
    audit = (app.home / "logs" / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "created"' in audit and "mock-1" in audit


async def test_audit_failure_does_not_break_generate(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setattr(app.audit, "path", app.home / "no-such-dir" / "x" / "audit.jsonl")
    r = await app.generate("a boat")
    assert r["status"] == L.QUEUED


# ── 다운로드 재시도·복구 (리뷰 #4, #6) ──

async def test_transient_download_failure_retries_with_backoff_then_lost_then_recovers(make_app, clock, home):
    healthy = {"ok": False}

    def handler(request):
        if healthy["ok"]:
            return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=MOCK_VIDEO_BYTES)
        return httpx.Response(503)

    app = make_app(provider=AlwaysDone(), transport=httpx.MockTransport(handler))
    r = await app.generate("a boat")
    jid = r["job_id"]

    await app.collector.poll_once()
    job = app.ledger.get(jid)
    assert job["status"] == L.RUNNING and job["next_download_at"]   # 바로 lost 가 되지 않는다

    await app.collector.poll_once()                                  # 백오프 중 → 시도하지 않음
    assert app.ledger.get(jid)["download_attempts"] == 1

    clock.advance(seconds=11)
    await app.collector.poll_once()
    assert app.ledger.get(jid)["download_attempts"] == 2

    clock.advance(minutes=51)                                        # 재시도 기한 경과
    await app.collector.poll_once()
    assert app.ledger.get(jid)["status"] == L.LOST

    healthy["ok"] = True                                             # 설정·네트워크를 고친 뒤 도구로 복구
    async with running(app):
        v = await app.download(jid, wait_seconds=10)
    assert v["status"] == L.SUCCEEDED
    assert (home / "output" / f"{jid}.mp4").exists()


async def test_cancel_during_download_returns_to_running(make_app):
    started = asyncio.Event()

    async def endless():
        yield MOCK_VIDEO_BYTES[:16]
        started.set()
        await asyncio.sleep(3600)
        yield b""

    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=endless())

    app = make_app(provider=AlwaysDone(), transport=httpx.MockTransport(handler))
    r = await app.generate("a boat")
    task = asyncio.create_task(app.process_job(r["job_id"]))
    await asyncio.wait_for(started.wait(), 5)
    assert app.ledger.get(r["job_id"])["status"] == L.DOWNLOADING
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app.ledger.get(r["job_id"])["status"] == L.RUNNING        # 10분 동안 갇히지 않는다
    assert not list((app.output_dir).glob("*.part"))


# ── 경합: 다른 프로세스가 상태를 바꾼 경우 (리뷰 #5) ──

class RacyStatus(MockProvider):
    """조회를 기다리는 사이 다른 프로세스가 다운로드를 맡은 상황을 흉내 낸다."""

    def __init__(self, ledger_ref):
        super().__init__()
        self.ledger_ref = ledger_ref

    async def get_task(self, job):
        self.ledger_ref["ledger"].claim_download(self.ledger_ref["job_id"])
        return TaskState("running")


async def test_stale_snapshot_does_not_overwrite_other_process(make_app):
    ref = {}
    app = make_app(provider=RacyStatus(ref))
    r = await app.generate("a boat")
    ref.update(ledger=app.ledger, job_id=r["job_id"])
    await app.process_job(r["job_id"])
    assert app.ledger.get(r["job_id"])["status"] == L.DOWNLOADING   # running 으로 덮어쓰지 않는다


class CancelRace(MockProvider):
    """시간 초과 취소를 보내는 사이 다른 프로세스가 완료를 기록한 상황."""

    def __init__(self, ref):
        super().__init__(steps_to_finish=10_000)
        self.ref = ref

    async def cancel(self, job):
        self.ref["ledger"].update(self.ref["job_id"], status=L.SUCCEEDED, file="output/x.mp4")


async def test_timeout_does_not_overwrite_success(make_app, clock):
    ref = {}
    app = make_app(provider=CancelRace(ref))
    r = await app.generate("slow")
    ref.update(ledger=app.ledger, job_id=r["job_id"])
    await app.collector.poll_once()  # queued 로 한 번
    clock.advance(minutes=61)
    await app.collector.poll_once()
    assert app.ledger.get(r["job_id"])["status"] == L.SUCCEEDED


async def test_two_apps_download_once(make_app, home):
    """같은 ledger 를 쓰는 두 서버 프로세스가 동시에 수거해도 한 번만 내려받는다."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=MOCK_VIDEO_BYTES)

    a = make_app(provider=AlwaysDone(), transport=httpx.MockTransport(handler))
    b = make_app(provider=AlwaysDone(), transport=httpx.MockTransport(handler))
    r = await a.generate("a boat")
    await asyncio.gather(a.collector.poll_once(), b.collector.poll_once())
    assert calls["n"] == 1
    assert a.ledger.get(r["job_id"])["status"] == L.SUCCEEDED


# ── 도구 수준 ──

async def test_policy_rejection_creates_no_job(make_app):
    app = make_app()
    with pytest.raises(PolicyError):
        await app.generate("a boat", resolution="1080p")
    assert app.ledger.jobs() == {}


async def test_missing_key_disables_provider(make_app):
    app = make_app(providers={"fal": MockProvider()})
    with pytest.raises(AppError, match="REPLICATE_API_TOKEN"):
        await app.generate("a boat")


async def test_download_tool_errors(make_app):
    app = make_app(provider=StubCreate(ProviderError("rejected")))
    with pytest.raises(AppError, match="찾을 수 없습니다"):
        await app.download("j_20990101_000000_ffffff")
    with pytest.raises(AppError):
        await app.generate("a boat")
    (jid,) = app.ledger.jobs()
    with pytest.raises(AppError, match="내려받을 수 없는 상태"):
        await app.download(jid)


async def test_usage_and_models(make_app):
    app = make_app()
    await app.generate("a boat")
    u = app.usage()
    assert u["today_count"] == 1 and u["pending_jobs"] == 1
    m = app.list_models()
    aliases = {x["model"]: x for x in m["models"]}
    assert aliases["seedance-2.5"]["default"] is True
    assert "1080p" not in aliases["seedance-2.5-fal"]["price_per_sec_usd"]
    assert m["defaults"]["resolution"] == "480p"


async def test_audit_log_has_no_secrets_and_short_prompt(make_app, home):
    app = make_app()
    long_prompt = f"key {FAKE_REPLICATE} and {FAKE_FAL} " + "x" * 200
    await app.generate(long_prompt)
    text = (home / "logs" / "audit.jsonl").read_text(encoding="utf-8")
    assert FAKE_REPLICATE not in text and FAKE_FAL not in text
    first = json.loads(text.splitlines()[0])
    assert len(first["prompt"]) <= 51


async def test_status_returns_after_wait(make_app):
    app = make_app(provider=MockProvider(steps_to_finish=10_000))
    async with running(app):
        r = await app.generate("slow")
        s = await app.status(r["job_id"], wait_seconds=0)
    assert s["status"] in (L.QUEUED, L.RUNNING)
    assert "다시 부르세요" in s["message"]


# ── 재검증 지적 (N1~N8) ──

class StatusAlwaysFails(MockProvider):
    async def get_task(self, job):
        raise ProviderError("fal: 인증 실패. API 키를 확인하세요.", status_code=401)


async def test_stale_download_with_failing_status_eventually_leaves_pending(make_app, clock):
    """N1: 다운로드 중 죽은 뒤 상태 조회가 계속 실패해도 진행 중 슬롯에 영원히 남지 않는다."""
    app = make_app(provider=StatusAlwaysFails())
    r = await app.generate("a boat")
    jid = r["job_id"]
    app.ledger.update(jid, status=L.RUNNING, completed_at=clock.now.isoformat())
    app.ledger.claim_download(jid)                     # 이 프로세스가 다운로드 도중 죽었다고 가정
    for _ in range(12):                                 # 10분마다 수거, 총 120분
        clock.advance(minutes=10, seconds=1)
        await app.collector.poll_once()
    job = app.ledger.get(jid)
    assert job["status"] == L.LOST                      # 완료를 본 적이 있으므로 '받지 못함'
    assert app.usage()["pending_jobs"] == 0


async def test_status_errors_do_not_refresh_stale_download(make_app, clock):
    """N1: downloading 상태에서는 오류 기록으로 updated_at 을 갱신하지 않는다 (회수가 미뤄지지 않게)."""
    app = make_app(provider=StatusAlwaysFails())
    r = await app.generate("a boat")
    jid = r["job_id"]
    app.ledger.update(jid, status=L.RUNNING)
    app.ledger.claim_download(jid)
    clock.advance(minutes=11)
    before = app.ledger.get(jid)["updated_at"]
    await app.collector.poll_once()
    assert app.ledger.get(jid)["updated_at"] == before


@pytest.mark.parametrize("exc", [OSError(28, "No space left on device"), PermissionError("locked")])
async def test_non_download_error_uses_backoff_and_deadline(make_app, clock, monkeypatch, exc):
    """N2: 디스크·파일 잠김 같은 오류도 백오프와 기한을 적용한다 (매 주기 전체 재다운로드 금지)."""
    from capped_video_mcp import service as svc

    calls = {"n": 0}

    async def failing(*a, **k):
        calls["n"] += 1
        raise exc

    monkeypatch.setattr(svc, "download_video", failing)
    app = make_app(provider=AlwaysDone())
    r = await app.generate("a boat")
    jid = r["job_id"]
    await app.collector.poll_once()
    job = app.ledger.get(jid)
    assert job["status"] == L.RUNNING and job["next_download_at"] and type(exc).__name__ in job["error"]
    await app.collector.poll_once()
    await app.collector.poll_once()
    assert calls["n"] == 1                               # 백오프 중에는 다시 받지 않는다
    clock.advance(minutes=51)
    await app.collector.poll_once()
    assert app.ledger.get(jid)["status"] == L.LOST       # 기한이 지나면 정리된다


async def test_heartbeat_failure_does_not_abort_download(make_app, monkeypatch, home):
    """N2: heartbeat 기록 실패가 정상 다운로드를 끊지 않는다."""
    from capped_video_mcp import download as d

    monkeypatch.setattr(d, "HEARTBEAT_SEC", 0.0)
    app = make_app(provider=AlwaysDone())

    def broken_touch(job_id):
        raise L.LedgerError("잠김")

    monkeypatch.setattr(app.ledger, "touch", broken_touch)
    r = await app.generate("a boat")
    await app.collector.poll_once()
    assert app.ledger.get(r["job_id"])["status"] == L.SUCCEEDED


@pytest.mark.parametrize("exc", [CreateUncertainError("replicate: 응답 없음"), RuntimeError("boom")])
async def test_uncertain_create_with_ledger_failure_still_says_do_not_retry(make_app, monkeypatch, exc):
    """N3: 결과를 모르는 생성 뒤 기록까지 실패해도 '다시 보내지 마세요'를 지킨다."""
    app = make_app(provider=StubCreate(exc))
    real_update = app.ledger.update

    def failing_update(job_id, **fields):
        if fields.get("status") == L.UNKNOWN:
            raise L.LedgerError("사용량 기록 파일이 잠겨 있습니다. 잠시 뒤 다시 시도하세요.")
        return real_update(job_id, **fields)

    monkeypatch.setattr(app.ledger, "update", failing_update)
    with pytest.raises(AppError) as e:
        await app.generate("a boat")
    assert "다시 보내지" in str(e.value)
    assert "잠시 뒤 다시 시도" not in str(e.value)
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.RESERVED                   # 비용·진행 중 슬롯은 유지 → 나중에 unknown 으로 정리


async def test_status_wait_does_not_flood_provider(make_app):
    """N4: 상태를 기다리는 동안 제공처 조회가 수거기 주기를 넘지 않는다."""
    mp = MockProvider(steps_to_finish=10_000)
    app = make_app(provider=mp)
    app.collector._interval = 0.5
    r = await app.generate("slow")
    async with running(app):
        await app.status(r["job_id"], wait_seconds=1)
    rid = app.ledger.get(r["job_id"])["remote_id"]
    assert mp._calls[rid] <= 4                          # 시작 시 1회 + 0.5초 주기 몇 회


async def test_leftover_part_files_are_removed(make_app, home):
    """N6: 강제 종료로 남은 이전 시도의 임시 파일을 정리한다."""
    app = make_app(provider=AlwaysDone())
    r = await app.generate("a boat")
    app.output_dir.mkdir(parents=True, exist_ok=True)
    leftover = app.output_dir / f"{r['job_id']}.deadbeef.part"
    leftover.write_bytes(b"partial")
    await app.collector.poll_once()
    assert not leftover.exists()
    assert app.ledger.get(r["job_id"])["status"] == L.SUCCEEDED


async def test_lost_retry_respects_pending_limit(make_app, settings):
    """N7: lost 를 되살릴 때도 동시 작업 상한을 지킨다."""
    app = make_app(provider=MockProvider(steps_to_finish=10_000))
    jobs = [(await app.generate(f"a boat {i}"))["job_id"] for i in range(settings.limits.max_pending_jobs)]
    app.ledger.update(jobs[0], status=L.LOST)
    await app.generate("one more")                       # 슬롯 하나가 비어 새 작업이 들어감
    with pytest.raises(AppError, match="상한"):
        await app.download(jobs[0], wait_seconds=0)


async def test_backoff_message_is_not_generating(make_app):
    """N8: 다운로드 재시도 대기 중에는 '생성 중'이라고 안내하지 않는다."""
    def handler(request):
        return httpx.Response(503)

    app = make_app(provider=AlwaysDone(), transport=httpx.MockTransport(handler))
    r = await app.generate("a boat")
    await app.collector.poll_once()
    s = await app.status(r["job_id"], wait_seconds=0)
    assert "다운로드 재시도" in s["message"]


# ── 중복 요청 ──

def _audit_events(app):
    return [json.loads(line) for line in app.audit.path.read_text(encoding="utf-8").splitlines()]


async def test_duplicate_generate_returns_existing_job_without_charging(make_app):
    mp = MockProvider()
    app = make_app(provider=mp)
    r1 = await app.generate("a boat")
    r2 = await app.generate("a  boat ")
    assert r2["duplicate"] is True and r2["job_id"] == r1["job_id"] and r2["matched_by"] == "fingerprint"
    assert r2["status"] == L.QUEUED and "force=true" in r2["message"]
    assert len(mp._prompts) == 1                       # 제공처 호출은 한 번
    assert app.ledger.summary()["today_count"] == 1    # 예산도 한 번만 깎임
    assert any(e.get("event") == "duplicate" for e in _audit_events(app))


async def test_duplicate_after_success_returns_file(make_app):
    app = make_app()
    r1 = await app.generate("a boat")
    await poll(app)
    r2 = await app.generate("a boat")
    assert r2["duplicate"] is True and r2["status"] == L.SUCCEEDED
    assert r2["file"].endswith(f"{r1['job_id']}.mp4")


async def test_force_creates_second_job(make_app):
    mp = MockProvider()
    app = make_app(provider=mp)
    r1 = await app.generate("a boat")
    r2 = await app.generate("a boat", force=True)
    assert "duplicate" not in r2 and r2["job_id"] != r1["job_id"]
    assert len(mp._prompts) == 2


async def test_request_id_dedupes_and_is_validated(make_app):
    app = make_app()
    r1 = await app.generate("a boat", request_id="k1")
    r2 = await app.generate("another prompt", request_id=" k1 ")   # 공백은 무시
    assert r2["duplicate"] is True and r2["job_id"] == r1["job_id"] and r2["matched_by"] == "request_id"
    with pytest.raises(AppError, match="128"):
        await app.generate("x", request_id="k" * 129)


async def test_create_failed_can_be_retried(make_app):
    class Rejecting(MockProvider):
        async def create_task(self, inp):
            raise ProviderError("거부")

    ok = MockProvider()
    app = make_app(providers={"replicate": Rejecting()})   # fal 키 없음 → 전환도 없음
    with pytest.raises(AppError):
        await app.generate("a boat")
    app.providers["replicate"] = ok
    r = await app.generate("a boat")       # 과금되지 않은 실패는 중복으로 보지 않는다
    assert "duplicate" not in r and r["status"] == L.QUEUED


# ── 견적 ──

async def test_estimate_sorted_cheapest_first_and_free(make_app):
    app = make_app()
    e = app.estimate(duration_sec=5)
    costs = [o["estimated_usd"] for o in e["options"]]
    assert costs == sorted(costs) and len(costs) >= 3
    assert e["cheapest"] == e["options"][0]["model"] and e["options"][0]["fits_budget"] is True
    assert e["remaining_today_usd"] == pytest.approx(app.settings.limits.daily_max_usd)
    assert app.ledger.summary()["today_count"] == 0
    # 5초를 못 만드는 모델은 이유와 함께 unavailable (veo 는 4·6·8초, hailuo 는 6·10초)
    un = {u["model"]: u["reason"] for u in e["unavailable"]}
    assert "veo-3.1-fast" in un and "hailuo-2.3" in un and "초" in un["hailuo-2.3"]
    assert "veo-3.1-fast" not in {o["model"] for o in e["options"]}


async def test_estimate_matches_generate_cost(make_app):
    app = make_app()
    e = app.estimate(duration_sec=4, model="seedance-2.5-fal", resolution="720p")
    assert [o["model"] for o in e["options"]] == ["seedance-2.5-fal"]
    r = await app.generate("a boat", model="seedance-2.5-fal", resolution="720p", duration_sec=4)
    assert e["options"][0]["estimated_usd"] == pytest.approx(r["estimated_usd"])


async def test_estimate_marks_missing_key_and_budget(make_app, settings):
    ok = MockProvider()
    app = make_app(providers={"fal": ok})          # replicate 키 없음
    e = app.estimate(duration_sec=5)
    assert {u["model"] for u in e["unavailable"] if "키" in u["reason"]} == {"seedance-2.5"}
    assert all(o["provider"] == "fal" for o in e["options"])

    tight = settings.model_copy(update={"limits": settings.limits.model_copy(update={"daily_max_usd": 0.3})})
    app.settings = tight
    e2 = app.estimate(duration_sec=5)
    # 5초 기준: gemini 360p $0.1365, wan 480p $0.25 만 $0.3 안에 들어간다
    assert e2["cheapest"] == "gemini-omni-1.2-flash"
    assert {o["model"] for o in e2["options"] if o["fits_budget"]} == {"gemini-omni-1.2-flash", "wan-2.5"}
    app.settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"daily_max_usd": 0.1})})
    e3 = app.estimate(duration_sec=5)
    assert e3["cheapest"] is None and "남은 예산" in e3["note"]


async def test_estimate_reports_duplicate(make_app):
    app = make_app()
    r = await app.generate("a boat")
    e = app.estimate(prompt="a  boat")
    assert e["duplicate_of"] == r["job_id"]
    assert "duplicate_of" not in app.estimate(prompt="other")
    assert "duplicate_of" not in app.estimate()


def test_estimate_rejects_unknown_model(make_app):
    with pytest.raises(AppError, match="허용되지 않은 모델"):
        make_app().estimate(model="nope")


# ── 제공처 전환 ──

class Rejecting(MockProvider):
    async def create_task(self, inp):
        raise ProviderError("모델 점검 중")


class Uncertain(MockProvider):
    async def create_task(self, inp):
        raise CreateUncertainError("응답 시간 초과", remote_id=None)


async def test_failover_to_fallback_model_on_confirmed_rejection(make_app):
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})
    r = await app.generate("a boat")
    assert r["failover_from"] == "seedance-2.5" and r["model"] == "seedance-2.5-fal" and r["provider"] == "fal"
    assert r["estimated_usd"] == pytest.approx(0.2205 * 5) and "거부" in r["message"]
    job = app.ledger.get(r["job_id"])
    assert job["model"] == "seedance-2.5-fal" and job["status"] == L.QUEUED and fal._prompts
    assert app.ledger.summary()["today_usd"] == pytest.approx(0.2205 * 5)
    events = [e["event"] for e in _audit_events(app)]
    assert events.count("create_requested") == 2 and "failover" in events
    await poll(app)
    assert app.ledger.get(r["job_id"])["status"] == L.SUCCEEDED


async def test_no_failover_when_outcome_unknown(make_app):
    fal = MockProvider()
    app = make_app(providers={"replicate": Uncertain(), "fal": fal})
    with pytest.raises(AppError, match="다시 보내지 마세요"):
        await app.generate("a boat")
    assert not fal._prompts
    assert list(app.ledger.jobs().values())[0]["status"] == L.UNKNOWN


async def test_no_failover_when_fallback_over_budget(make_app, settings):
    fal = MockProvider()
    app = make_app(providers={"replicate": Rejecting(), "fal": fal})
    app.settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"daily_max_usd": 0.6})})
    with pytest.raises(AppError, match="점검"):
        await app.generate("a boat")        # replicate $0.514 는 예산 안, fal $1.10 은 밖
    assert not fal._prompts
    job = list(app.ledger.jobs().values())[0]
    assert job["status"] == L.CREATE_FAILED and job["model"] == "seedance-2.5"


@pytest.mark.parametrize("why", ["disabled", "no_key", "no_fallback"])
async def test_no_failover_cases(make_app, settings, why):
    fal = MockProvider()
    providers = {"replicate": Rejecting(), "fal": fal}
    kwargs = {}
    if why == "no_key":
        providers.pop("fal")
    app = make_app(providers=providers)
    if why == "disabled":
        app.settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"failover": False})})
    if why == "no_fallback":
        kwargs = {"model": "kling-3.0"}
        app.providers["fal"] = Rejecting()
    with pytest.raises(AppError):
        await app.generate("a boat", **kwargs)
    assert not fal._prompts
    assert all(j["status"] == L.CREATE_FAILED for j in app.ledger.jobs().values())


async def test_failover_reports_both_errors_when_fallback_also_fails(make_app):
    class Rejecting2(MockProvider):
        async def create_task(self, inp):
            raise ProviderError("잔액 부족")
    app = make_app(providers={"replicate": Rejecting(), "fal": Rejecting2()})
    with pytest.raises(AppError, match="seedance-2.5: 모델 점검 중 / seedance-2.5-fal: 잔액 부족"):
        await app.generate("a boat")
    assert list(app.ledger.jobs().values())[0]["status"] == L.CREATE_FAILED


# ── 비용 확인 ──

def _confirming(answer):
    calls = []

    async def confirm(req):
        calls.append(req.estimated_usd)
        return answer
    confirm.calls = calls
    return confirm


def _with_confirm(app, settings, above, fallback="allow"):
    app.settings = settings.model_copy(update={"limits": settings.limits.model_copy(
        update={"confirm_above_usd": above, "confirm_fallback": fallback})})
    return app


async def test_confirm_asked_only_at_or_above_threshold(make_app, settings):
    app = _with_confirm(make_app(), settings, 0.5)
    yes = _confirming(True)
    r = await app.generate("a boat", confirm=yes)                                 # $0.514 ≥ 0.5 → 묻는다
    assert r["status"] == L.QUEUED and yes.calls == [pytest.approx(0.514)]
    r2 = await app.generate("cheap", model="wan-2.5", duration_sec=5, confirm=yes)  # $0.25 → 안 묻는다
    assert r2["status"] == L.QUEUED and len(yes.calls) == 1
    assert "confirm_accepted" in [e["event"] for e in _audit_events(app)]


async def test_confirm_declined_charges_nothing(make_app, settings):
    mp = MockProvider()
    app = _with_confirm(make_app(provider=mp), settings, 0.1)
    with pytest.raises(AppError, match="취소"):
        await app.generate("a boat", confirm=_confirming(False))
    assert not mp._prompts and app.ledger.summary()["today_count"] == 0
    assert "confirm_declined" in [e["event"] for e in _audit_events(app)]


async def test_confirm_unsupported_client_follows_fallback(make_app, settings):
    app = _with_confirm(make_app(), settings, 0.1, "allow")
    assert (await app.generate("a boat", confirm=_confirming(None)))["status"] == L.QUEUED
    app = _with_confirm(make_app(), settings, 0.1, "deny")
    with pytest.raises(AppError, match="지원하지 않습니다"):
        await app.generate("another boat", confirm=_confirming(None))   # 같은 home 이라 다른 프롬프트
    assert app.ledger.summary()["today_count"] == 1


async def test_confirm_off_by_default_and_duplicate_skips_confirm(make_app, settings):
    app = make_app()
    yes = _confirming(True)
    await app.generate("a boat", confirm=yes)
    assert yes.calls == []                                 # confirm_above_usd=0 → 묻지 않음
    app = _with_confirm(app, settings, 0.1)
    r = await app.generate("a boat", confirm=yes)
    assert r["duplicate"] is True and yes.calls == []      # 중복은 과금이 없으니 묻지 않음


# ── 미리보기 ──

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"preview" * 10 + b"\xff\xd9"


async def test_preview_returns_image_and_caches(make_app, home, monkeypatch, tmp_path):
    from capped_video_mcp import preview as P
    calls = []

    async def fake_run(args):
        calls.append(args)
        return FAKE_JPEG
    monkeypatch.setattr(P, "_run_ffmpeg", fake_run)
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"")
    app = make_app()
    app.settings = app.settings.model_copy(update={"preview": app.settings.preview.model_copy(update={"ffmpeg": str(fake)})})
    r = await app.generate("a boat", duration_sec=4)
    view, img = await app.preview(r["job_id"])
    assert img is None and view["preview_available"] is False and "완성되지" in view["preview_reason"]
    await poll(app)
    view, img = await app.preview(r["job_id"])
    assert img == FAKE_JPEG and view["preview_available"] is True and view["preview_frames"] == 6
    assert calls and calls[0][0] == str(fake) and any("tile=3x2" in a and "scale=320" in a for a in calls[0])
    assert (home / "output" / f"{r['job_id']}.preview.jpg").read_bytes() == FAKE_JPEG
    await app.preview(r["job_id"])
    assert len(calls) == 1                                  # 두 번째는 캐시


async def test_preview_without_ffmpeg_explains(make_app, monkeypatch):
    from capped_video_mcp import preview as P
    monkeypatch.setattr(P.shutil, "which", lambda _: None)
    app = make_app()
    r = await app.generate("a boat")
    await poll(app)
    view, img = await app.preview(r["job_id"])
    assert img is None and view["preview_available"] is False and "ffmpeg" in view["preview_reason"]
    assert view["status"] == L.SUCCEEDED and view["file"]  # 영상 자체는 그대로 쓸 수 있다


async def test_preview_unknown_job(make_app):
    with pytest.raises(AppError, match="찾을 수 없습니다"):
        await make_app().preview("j_none")
