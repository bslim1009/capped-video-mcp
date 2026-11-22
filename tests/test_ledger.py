from concurrent.futures import ThreadPoolExecutor

import pytest

from capped_video_mcp import ledger as L
from capped_video_mcp.ledger import Ledger
from capped_video_mcp.policy import PolicyError, build_request


@pytest.fixture
def ledger(home, clock):
    return Ledger(home / "data" / "usage.json", clock=clock)


def _limits(settings, **kw):
    # 상한 테스트는 같은 요청을 반복하므로 중복 감지를 끈다. 중복 감지는 아래 "중복 요청" 절에서 따로 본다.
    return settings.limits.model_copy(update={"duplicate_window_minutes": 0, **kw})


def test_reserve_records_job(ledger, settings):
    job_id = ledger.reserve(build_request(settings, "a"), settings.limits)
    job = ledger.get(job_id)
    assert job["status"] == L.RESERVED
    assert job["estimated_usd"] == pytest.approx(0.514)
    assert ledger.summary()["today_count"] == 1


def test_daily_count_limit(ledger, settings):
    lim = _limits(settings, daily_max_count=2, max_pending_jobs=10)
    req = build_request(settings, "a")
    ledger.reserve(req, lim)
    ledger.reserve(req, lim)
    with pytest.raises(PolicyError, match="횟수 상한"):
        ledger.reserve(req, lim)


def test_daily_usd_limit(ledger, settings):
    lim = _limits(settings, daily_max_usd=1.0, max_pending_jobs=10)
    req = build_request(settings, "a")  # $0.514
    ledger.reserve(req, lim)
    with pytest.raises(PolicyError, match="오늘 예산"):
        ledger.reserve(req, lim)


def test_monthly_usd_limit(ledger, settings, clock):
    lim = _limits(settings, monthly_max_usd=1.2, max_pending_jobs=10)
    req = build_request(settings, "a")
    ledger.reserve(req, lim)
    clock.advance(days=1)
    ledger.reserve(req, lim)
    clock.advance(days=1)
    with pytest.raises(PolicyError, match="이번 달 예산"):
        ledger.reserve(req, lim)


def test_pending_limit_and_release(ledger, settings):
    lim = _limits(settings, max_pending_jobs=1)
    req = build_request(settings, "a")
    j1 = ledger.reserve(req, lim)
    with pytest.raises(PolicyError, match="진행 중인 작업"):
        ledger.reserve(req, lim)
    ledger.update(j1, status=L.SUCCEEDED)
    ledger.reserve(req, lim)


def test_create_failed_not_counted_but_failed_is(ledger, settings):
    req = build_request(settings, "a")
    lim = _limits(settings)
    j1 = ledger.reserve(req, lim)
    j2 = ledger.reserve(req, lim)
    ledger.update(j1, status=L.CREATE_FAILED)
    ledger.update(j2, status=L.FAILED)
    s = ledger.summary()
    assert s["today_count"] == 1
    assert s["today_usd"] == pytest.approx(0.514)


def test_kst_day_boundary(ledger, settings, clock):
    clock.now = clock.now.replace(hour=23, minute=59)
    ledger.reserve(build_request(settings, "a"), settings.limits)
    assert ledger.summary()["today_count"] == 1
    clock.advance(minutes=2)  # 한국 시간 자정을 넘김
    s = ledger.summary()
    assert s["today_count"] == 0
    assert s["month_usd"] == pytest.approx(0.514)


def test_concurrent_reserve_respects_limit(home, settings, clock):
    """여러 프로세스(여기서는 별도 Ledger 인스턴스 + 스레드)가 동시에 예약해도 상한을 넘지 않는다."""
    lim = _limits(settings, daily_max_count=5, max_pending_jobs=100, daily_max_usd=100.0)
    req = build_request(settings, "a")

    def attempt(_):
        lg = Ledger(home / "data" / "usage.json", clock=clock)
        try:
            lg.reserve(req, lim)
            return True
        except PolicyError:
            return False

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(attempt, range(20)))
    assert sum(results) == 5
    assert len(Ledger(home / "data" / "usage.json", clock=clock).jobs()) == 5


def test_claim_download_once_and_stale_reclaim(ledger, settings, clock):
    j = ledger.reserve(build_request(settings, "a"), settings.limits)
    ledger.update(j, status=L.RUNNING)
    assert ledger.claim_download(j) is not None
    assert ledger.claim_download(j) is None
    assert j not in ledger.collectable_jobs()
    clock.advance(minutes=11)
    assert j in ledger.collectable_jobs()
    assert ledger.claim_download(j) is not None
    assert ledger.get(j)["download_attempts"] == 2


def test_claim_download_rejects_terminal(ledger, settings):
    j = ledger.reserve(build_request(settings, "a"), settings.limits)
    ledger.update(j, status=L.SUCCEEDED)
    assert ledger.claim_download(j) is None


def test_job_id_format(ledger, settings):
    from capped_video_mcp.download import JOB_ID_RE

    j = ledger.reserve(build_request(settings, "a"), settings.limits)
    assert JOB_ID_RE.match(j)


def test_transition_compare_and_set(ledger, settings):
    j = ledger.reserve(build_request(settings, "a"), settings.limits)
    assert ledger.transition(j, {L.QUEUED}, status=L.RUNNING) is None   # 기대 상태가 아니면 안 바꾼다
    assert ledger.get(j)["status"] == L.RESERVED
    assert ledger.transition(j, {L.RESERVED}, status=L.QUEUED)["status"] == L.QUEUED


def test_heartbeat_keeps_download_alive(ledger, settings, clock):
    j = ledger.reserve(build_request(settings, "a"), settings.limits)
    ledger.update(j, status=L.RUNNING)
    ledger.claim_download(j)
    clock.advance(minutes=9)
    ledger.touch(j)
    clock.advance(minutes=9)
    assert j not in ledger.collectable_jobs()   # 18분째지만 heartbeat 덕에 회수되지 않는다


def test_unknown_counts_as_pending_for_a_while(ledger, settings, clock):
    lim = _limits(settings, max_pending_jobs=1)
    j = ledger.reserve(build_request(settings, "a"), lim)
    ledger.update(j, status=L.UNKNOWN)
    with pytest.raises(PolicyError, match="진행 중인 작업"):
        ledger.reserve(build_request(settings, "a"), lim)
    clock.advance(minutes=61)
    ledger.reserve(build_request(settings, "a"), lim)


def test_corrupted_file_fails_closed(ledger, settings):
    ledger.path.write_text("{broken", encoding="utf-8")
    with pytest.raises(L.LedgerError, match="손상"):
        ledger.reserve(build_request(settings, "a"), settings.limits)


def test_lock_timeout_is_ledger_error(home, settings, clock):
    from threading import Event, Thread

    from filelock import FileLock

    path = home / "data" / "usage.json"
    lg = Ledger(path, clock=clock, lock_timeout=0.1)
    held, release = Event(), Event()

    def hold():
        with FileLock(str(path) + ".lock"):
            held.set()
            release.wait(5)

    t = Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(L.LedgerError, match="잠겨"):
            lg.summary()
    finally:
        release.set()
        t.join()


# ── 중복 요청 ──

def test_duplicate_request_returns_existing_job(ledger, settings):
    req = build_request(settings, "a boat")
    first = ledger.reserve(req, settings.limits)
    with pytest.raises(L.DuplicateRequest) as e:
        ledger.reserve(build_request(settings, "  a   boat "), settings.limits)  # 공백만 달라도 같은 요청
    assert e.value.job_id == first and e.value.matched_by == "fingerprint"
    assert ledger.summary()["today_count"] == 1


def test_duplicate_detection_is_case_sensitive_and_param_sensitive(ledger, settings):
    lim = _limits(settings, max_pending_jobs=10, duplicate_window_minutes=10)
    ledger.reserve(build_request(settings, "a boat"), lim)
    ledger.reserve(build_request(settings, "A boat"), lim)
    ledger.reserve(build_request(settings, "a boat", duration_sec=4), lim)
    ledger.reserve(build_request(settings, "a boat", aspect_ratio="9:16"), lim)
    assert ledger.summary()["today_count"] == 4


def test_duplicate_window_expires(ledger, settings, clock):
    req = build_request(settings, "a")
    ledger.reserve(req, settings.limits)
    clock.advance(minutes=settings.limits.duplicate_window_minutes, seconds=1)
    ledger.reserve(req, settings.limits)
    assert ledger.summary()["today_count"] == 2


def test_duplicate_window_zero_disables(ledger, settings):
    lim = _limits(settings)  # duplicate_window_minutes=0
    req = build_request(settings, "a")
    ledger.reserve(req, lim)
    ledger.reserve(req, lim)
    assert ledger.summary()["today_count"] == 2


def test_force_bypasses_duplicate_check(ledger, settings):
    req = build_request(settings, "a")
    ledger.reserve(req, settings.limits)
    ledger.reserve(req, settings.limits, force=True)
    assert ledger.summary()["today_count"] == 2


@pytest.mark.parametrize("status", sorted(L.RETRYABLE))
def test_failed_jobs_are_not_duplicates(ledger, settings, status):
    req = build_request(settings, "a")
    first = ledger.reserve(req, settings.limits)
    ledger.update(first, status=status)
    assert ledger.reserve(req, settings.limits) != first


@pytest.mark.parametrize("status", [L.QUEUED, L.RUNNING, L.SUCCEEDED, L.UNKNOWN, L.LOST])
def test_charged_or_inflight_jobs_are_duplicates(ledger, settings, status):
    req = build_request(settings, "a")
    first = ledger.reserve(req, settings.limits)
    ledger.update(first, status=status)
    with pytest.raises(L.DuplicateRequest):
        ledger.reserve(req, settings.limits)


def test_request_id_matches_even_if_prompt_differs(ledger, settings):
    first = ledger.reserve(build_request(settings, "a"), settings.limits, request_id="req-1")
    with pytest.raises(L.DuplicateRequest) as e:
        ledger.reserve(build_request(settings, "completely different"), settings.limits, request_id="req-1")
    assert e.value.job_id == first and e.value.matched_by == "request_id"
    # 다른 request_id 에 같은 프롬프트는 fingerprint 로 잡힌다
    with pytest.raises(L.DuplicateRequest) as e2:
        ledger.reserve(build_request(settings, "a"), settings.limits, request_id="req-2")
    assert e2.value.matched_by == "fingerprint"


def test_duplicate_picks_most_recent_match(ledger, settings, clock):
    req = build_request(settings, "a")
    ledger.reserve(req, settings.limits)
    clock.advance(minutes=1)
    second = ledger.reserve(req, settings.limits, force=True)
    with pytest.raises(L.DuplicateRequest) as e:
        ledger.reserve(req, settings.limits)
    assert e.value.job_id == second


def test_concurrent_identical_requests_create_one_job(ledger, settings):
    """두 프로세스(스레드)가 동시에 같은 요청을 보내도 하나만 만들어진다 (검사와 예약이 같은 잠금 안)."""
    req = build_request(settings, "a")

    def go(_):
        try:
            return ledger.reserve(req, settings.limits)
        except L.DuplicateRequest:
            return None

    with ThreadPoolExecutor(20) as ex:
        results = list(ex.map(go, range(20)))
    assert sum(r is not None for r in results) == 1


# ── 제공처 전환(reprice) ──

def test_reprice_swaps_model_and_cost_within_budget(ledger, settings):
    req = build_request(settings, "a")                       # replicate $0.514
    fb = build_request(settings, "a", model="seedance-2.5-fal")  # fal $1.1025
    job_id = ledger.reserve(req, settings.limits)
    assert ledger.reprice(job_id, fb, settings.limits) is True
    job = ledger.get(job_id)
    assert (job["provider"], job["model"], job["estimated_usd"]) == ("fal", "seedance-2.5-fal", pytest.approx(1.1025))
    assert job["status"] == L.RESERVED and job["fingerprint"] == req.fingerprint()  # 원래 요청의 지문은 유지
    assert ledger.summary()["today_usd"] == pytest.approx(1.1025)


def test_reprice_refuses_when_over_budget_or_not_reserved(ledger, settings):
    lim = _limits(settings, daily_max_usd=1.0)
    req = build_request(settings, "a")
    fb = build_request(settings, "a", model="seedance-2.5-fal")
    job_id = ledger.reserve(req, lim)
    assert ledger.reprice(job_id, fb, lim) is False
    assert ledger.get(job_id)["model"] == "seedance-2.5"
    ledger.update(job_id, status=L.QUEUED)
    assert ledger.reprice(job_id, fb, settings.limits) is False
    assert ledger.reprice("j_none", fb, settings.limits) is False
