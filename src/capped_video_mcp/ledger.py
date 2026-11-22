"""작업·사용량 기록 (data/usage.json).

서버가 다시 시작되거나 여러 프로세스가 동시에 떠도 상한이 지켜지도록
파일 + 파일 잠금(filelock)으로 기록한다.

상태 변경은 transition() 으로 "기대한 상태일 때만" 바꾼다 (compare-and-set).
await 사이에 다른 프로세스·작업이 상태를 바꿨다면 덮어쓰지 않는다.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .config import Limits
from .policy import GenerationRequest, PolicyError

# 한국은 서머타임이 없으므로 고정 오프셋을 쓴다 (Windows 에 tzdata 가 없어도 동작).
KST = timezone(timedelta(hours=9), "KST")

# 상태
RESERVED = "reserved"          # 상한 검사 통과, 원격 생성 전
QUEUED = "queued"              # 원격 작업 생성됨
RUNNING = "running"            # 원격에서 생성 중 (또는 다운로드 재시도 대기)
DOWNLOADING = "downloading"    # 한 프로세스가 다운로드 중
SUCCEEDED = "succeeded"        # 로컬 저장 완료
FAILED = "failed"              # 원격 생성 실패 또는 타임아웃 (과금 여부 불명 → 비용 합계에 포함)
CREATE_FAILED = "create_failed"  # 원격 작업 생성 자체가 확정 거부됨 (과금 없음 → 합계 제외)
UNKNOWN = "unknown"            # 생성 요청이 나갔는지/만들어졌는지 모름 (비용 포함)
LOST = "lost"                  # 원격에서 끝났지만 내려받지 못함 (download_video 로 재시도 가능)

PENDING = frozenset({RESERVED, QUEUED, RUNNING, DOWNLOADING})
COLLECTABLE = frozenset({QUEUED, RUNNING})
TERMINAL = frozenset({SUCCEEDED, FAILED, CREATE_FAILED, UNKNOWN, LOST})
NOT_CHARGED = frozenset({CREATE_FAILED})

STALE_DOWNLOAD = timedelta(minutes=10)   # 이보다 오래 heartbeat 가 없으면 다운로드한 프로세스가 죽은 것으로 본다
STALE_RESERVED = timedelta(minutes=5)    # 이보다 오래 reserved 면 생성 도중 중단된 것으로 본다


class LedgerError(Exception):
    pass


class DuplicateRequest(Exception):
    """같은 요청이 최근에 이미 접수됐다. job 은 기존 작업 기록."""

    def __init__(self, job_id: str, job: dict[str, Any], matched_by: str):
        super().__init__(job_id)
        self.job_id = job_id
        self.job = job
        self.matched_by = matched_by  # "request_id" 또는 "fingerprint"


# 중복으로 보지 않는 상태: 과금되지 않았거나(create_failed) 확정 실패(failed)라서 다시 시도해도 되는 작업.
RETRYABLE = frozenset({CREATE_FAILED, FAILED})


def _now() -> datetime:
    return datetime.now(KST)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(KST)


class Ledger:
    def __init__(
        self,
        path: Path,
        clock: Callable[[], datetime] = _now,
        *,
        unknown_pending: timedelta = timedelta(minutes=60),
        lock_timeout: float = 10.0,
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(path) + ".lock", timeout=lock_timeout)
        self._clock = clock
        # unknown 작업은 원격에서 실제로 돌고 있을 수 있으므로 이 시간 동안 진행 중으로 센다.
        self._unknown_pending = unknown_pending

    # ── 파일 입출력 ──
    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            with self._lock:
                yield
        except Timeout as e:
            raise LedgerError("사용량 기록 파일이 잠겨 있습니다. 잠시 뒤 다시 시도하세요.") from e

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"jobs": {}}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # 기록이 깨졌으면 상한을 확인할 수 없으므로 생성을 막는다 (fail-closed).
            raise LedgerError(f"사용량 기록 파일이 손상됐습니다: {self.path}. 백업 후 확인하세요.") from e
        data.setdefault("jobs", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # Windows 에서 백신·인덱서가 파일을 잠깐 잡고 있으면 os.replace 가 실패할 수 있어 몇 번 재시도한다.
        for i in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if i == 4:
                    raise
                time.sleep(0.05 * (i + 1))

    def now(self) -> datetime:
        return self._clock()

    # ── 집계 ──
    def _is_pending(self, job: dict[str, Any], now: datetime) -> bool:
        if job["status"] in PENDING:
            return True
        return job["status"] == UNKNOWN and now - _ts(job["created_at"]) < self._unknown_pending

    def _summary(self, jobs: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any]:
        today = now.date()
        month = (now.year, now.month)
        day_count = 0
        day_usd = 0.0
        month_usd = 0.0
        pending = 0
        for job in jobs.values():
            if self._is_pending(job, now):
                pending += 1
            if job["status"] in NOT_CHARGED:
                continue
            created = _ts(job["created_at"])
            if created.date() == today:
                day_count += 1
                day_usd += job["estimated_usd"]
            if (created.year, created.month) == month:
                month_usd += job["estimated_usd"]
        return {
            "today_count": day_count,
            "today_usd": round(day_usd, 4),
            "month_usd": round(month_usd, 4),
            "pending_jobs": pending,
        }

    def summary(self) -> dict[str, Any]:
        with self._locked():
            return self._summary(self._read()["jobs"], self.now())

    # ── 예약 ──
    def reserve(self, req: GenerationRequest, limits: Limits, *, request_id: str | None = None,
                force: bool = False) -> str:
        """상한을 검사하고 예상 비용을 예약한다. 넘으면 PolicyError.

        duplicate_window_minutes 안에 같은 요청(request_id 또는 fingerprint)이 있으면 DuplicateRequest.
        force=True 면 중복 검사를 건너뛴다. 중복 검사와 예약이 같은 잠금 안에서 일어나므로
        두 프로세스가 동시에 같은 요청을 보내도 하나만 만들어진다.
        """
        with self._locked():
            data = self._read()
            now = self.now()
            fp = req.fingerprint()
            if not force and limits.duplicate_window_minutes > 0:
                window = timedelta(minutes=limits.duplicate_window_minutes)
                dup = self._find_duplicate(data["jobs"], now, window, fp, request_id)
                if dup is not None:
                    raise DuplicateRequest(*dup)
            s = self._summary(data["jobs"], now)
            cost = req.estimated_usd
            if s["pending_jobs"] >= limits.max_pending_jobs:
                raise PolicyError(
                    f"진행 중인 작업이 {s['pending_jobs']}개입니다 (상한 {limits.max_pending_jobs}). 끝난 뒤 다시 요청하세요."
                )
            if s["today_count"] + 1 > limits.daily_max_count:
                raise PolicyError(f"오늘 생성 횟수 상한({limits.daily_max_count}회)에 도달했습니다.")
            if s["today_usd"] + cost > limits.daily_max_usd:
                left = max(0.0, limits.daily_max_usd - s["today_usd"])
                raise PolicyError(
                    f"오늘 예산을 넘습니다. 예상 ${cost:.2f}, 남은 예산 ${left:.2f}. 해상도나 길이를 줄여 보세요."
                )
            if s["month_usd"] + cost > limits.monthly_max_usd:
                left = max(0.0, limits.monthly_max_usd - s["month_usd"])
                raise PolicyError(f"이번 달 예산을 넘습니다. 예상 ${cost:.2f}, 남은 예산 ${left:.2f}.")

            job_id = f"j_{now:%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"
            data["jobs"][job_id] = {
                "provider": req.model.provider,
                "model": req.model.alias,
                "model_ref": req.model.model_ref,
                "resolution": req.resolution,
                "duration_sec": req.duration_sec,
                "aspect_ratio": req.aspect_ratio,
                "image": req.image.path if req.image is not None else None,   # 입력 이미지 경로 (내용은 남기지 않음)
                "references": req.reference_labels() or None,                  # 참조 표기 → 파일 경로
                "estimated_usd": cost,
                "status": RESERVED,
                "fingerprint": fp,
                "request_id": request_id,
                "remote_id": None,
                "remote_urls": {},
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "completed_at": None,       # 원격에서 완료를 처음 본 시각 (다운로드 재시도 기한 기준)
                "next_download_at": None,   # 다운로드 재시도 백오프
                "file": None,
                "error": None,
                "download_attempts": 0,
            }
            self._write(data)
            return job_id

    def find_duplicate(self, req: GenerationRequest, limits: Limits, request_id: str | None = None) -> str | None:
        """예약하지 않고 중복 여부만 본다 (estimate 용). 중복이면 기존 job_id."""
        if limits.duplicate_window_minutes <= 0:
            return None
        with self._locked():
            dup = self._find_duplicate(self._read()["jobs"], self.now(), timedelta(minutes=limits.duplicate_window_minutes),
                                       req.fingerprint(), request_id)
        return dup[0] if dup else None

    def _find_duplicate(self, jobs: dict[str, dict[str, Any]], now: datetime, window: timedelta,
                        fp: str, request_id: str | None) -> tuple[str, dict[str, Any], str] | None:
        """창 안의 동일 요청. request_id 일치가 fingerprint 일치보다 우선하고, 같은 종류면 가장 최근 것."""
        best: tuple[str, dict[str, Any], str] | None = None
        for job_id, job in jobs.items():
            if job["status"] in RETRYABLE:
                continue
            created = _ts(job["created_at"])
            if now - created > window:
                continue
            if request_id and job.get("request_id") == request_id:
                by = "request_id"
            elif job.get("fingerprint") == fp:
                by = "fingerprint"
            else:
                continue
            rank = (by == "request_id", created)
            if best is None or rank > (best[2] == "request_id", _ts(best[1]["created_at"])):
                best = (job_id, dict(job), by)
        return best

    def _reprice_ok(self, data: dict[str, Any], job_id: str, cost: float, limits: Limits) -> bool:
        job = data["jobs"].get(job_id)
        if job is None or job["status"] != RESERVED:
            return False
        others = {k: v for k, v in data["jobs"].items() if k != job_id}
        s = self._summary(others, self.now())
        return s["today_usd"] + cost <= limits.daily_max_usd and s["month_usd"] + cost <= limits.monthly_max_usd

    def can_reprice(self, job_id: str, req: GenerationRequest, limits: Limits) -> bool:
        """reprice 가 지금 성공할지 (바꾸지 않고 보기만 한다). 사용자에게 묻기 전에 쓴다."""
        with self._locked():
            return self._reprice_ok(self._read(), job_id, req.estimated_usd, limits)

    def reprice(self, job_id: str, req: GenerationRequest, limits: Limits) -> bool:
        """reserved 작업을 다른 모델(제공처 전환)로 바꾼다. 바뀐 비용이 예산 안이면 갱신하고 True, 아니면 False."""
        with self._locked():
            data = self._read()
            cost = req.estimated_usd
            if not self._reprice_ok(data, job_id, cost, limits):
                return False
            now = self.now()
            job = data["jobs"][job_id]
            job.update(provider=req.model.provider, model=req.model.alias, model_ref=req.model.model_ref,
                       resolution=req.resolution, duration_sec=req.duration_sec, aspect_ratio=req.aspect_ratio,
                       estimated_usd=cost, updated_at=now.isoformat())
            self._write(data)
            return True

    # ── 갱신 ──
    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        """조건 없이 갱신한다. 생성 직후처럼 이 작업을 확실히 혼자 다루는 경우에만 쓴다."""
        with self._locked():
            data = self._read()
            job = data["jobs"].get(job_id)
            if job is None:
                raise LedgerError(f"작업을 찾을 수 없습니다: {job_id}")
            job.update(fields)
            job["updated_at"] = self.now().isoformat()
            self._write(data)
            return dict(job)

    def transition(self, job_id: str, expected: Iterable[str], **fields: Any) -> dict[str, Any] | None:
        """현재 상태가 expected 중 하나일 때만 갱신한다. 아니면 아무것도 쓰지 않고 None."""
        expected = frozenset(expected)
        with self._locked():
            data = self._read()
            job = data["jobs"].get(job_id)
            if job is None or job["status"] not in expected:
                return None
            job.update(fields)
            job["updated_at"] = self.now().isoformat()
            self._write(data)
            return dict(job)

    def touch(self, job_id: str) -> None:
        """다운로드 heartbeat. 진행 중인 다운로드가 '죽은 것'으로 회수되지 않게 한다."""
        self.transition(job_id, {DOWNLOADING})

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._locked():
            job = self._read()["jobs"].get(job_id)
            return dict(job) if job else None

    def jobs(self, statuses: frozenset[str] | None = None) -> dict[str, dict[str, Any]]:
        with self._locked():
            jobs = self._read()["jobs"]
        return {k: dict(v) for k, v in jobs.items() if statuses is None or v["status"] in statuses}

    def collectable_jobs(self) -> dict[str, dict[str, Any]]:
        """수거 대상: 진행 중 작업, 오래 멈춘 다운로드, 생성 도중 중단된 예약."""
        now = self.now()
        out = {}
        for job_id, job in self.jobs().items():
            st = job["status"]
            if st in COLLECTABLE:
                out[job_id] = job
            elif st == DOWNLOADING and now - _ts(job["updated_at"]) > STALE_DOWNLOAD:
                out[job_id] = job
            elif st == RESERVED and now - _ts(job["updated_at"]) > STALE_RESERVED:
                out[job_id] = job
        return out

    def claim_download(self, job_id: str) -> dict[str, Any] | None:
        """이 프로세스가 다운로드를 맡는다. 다른 프로세스가 이미 맡았으면 None."""
        with self._locked():
            data = self._read()
            job = data["jobs"].get(job_id)
            if job is None:
                return None
            now = self.now()
            if job["status"] == DOWNLOADING:
                if now - _ts(job["updated_at"]) <= STALE_DOWNLOAD:
                    return None
            elif job["status"] not in COLLECTABLE:
                return None
            job["status"] = DOWNLOADING
            job["download_attempts"] = job.get("download_attempts", 0) + 1
            job["updated_at"] = now.isoformat()
            self._write(data)
            return dict(job)
