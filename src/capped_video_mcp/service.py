"""MCP 도구 뒤에서 실제 일을 하는 애플리케이션 계층.

원칙
- 원격 상태 조회·다운로드·취소 같은 부수 작업은 백그라운드 수거기(collector)만 한다.
  도구(get_video_status, download_video)는 수거기를 깨우고 기록을 읽기만 한다.
  → 도구 호출이 취소·타임아웃돼도 다운로드가 중간에 끊기지 않는다.
- 상태 변경은 ledger.transition() 으로 "기대한 상태일 때만" 한다 (다른 프로세스와 경합해도 덮어쓰지 않음).
- 취소(CancelledError)를 포함한 모든 예외에서 기록을 정리한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import ledger as L
from .collector import Collector
from .config import Secrets, Settings, default_home, load_secrets, load_settings
from .download import DownloadError, download_video
from .ledger import Ledger
from .log import Audit
from .policy import GenerationRequest, PolicyError, allowed_aspect_ratios, allowed_resolutions, build_payload, build_request
from .preview import PreviewUnavailable, contact_sheet
from .providers.base import CreateInput, CreateUncertainError, Provider, ProviderError

log = logging.getLogger(__name__)

STATUS_WAIT_STEP_SEC = 2.0
# 비용 확인 콜백: True 진행, False 거부, None 클라이언트가 확인을 지원하지 않음
Confirm = Callable[[GenerationRequest], Awaitable[bool | None]]
DOWNLOAD_BACKOFF_BASE_SEC = 10
DOWNLOAD_BACKOFF_MAX_SEC = 300

STATUS_MESSAGES = {
    L.RESERVED: "생성 요청을 보내는 중입니다.",
    L.QUEUED: "대기열에 있습니다. 잠시 뒤 get_video_status 를 다시 부르세요.",
    L.RUNNING: "생성 중입니다. 잠시 뒤 get_video_status 를 다시 부르세요.",
    L.DOWNLOADING: "완료되어 내려받는 중입니다. 잠시 뒤 get_video_status 를 다시 부르세요.",
    L.SUCCEEDED: "완료. 영상이 저장됐습니다.",
    L.FAILED: "생성에 실패했습니다. 과금 여부는 제공처 대시보드에서 확인하세요.",
    L.CREATE_FAILED: "생성 요청이 거부됐습니다. 과금되지 않습니다.",
    L.UNKNOWN: "생성 요청의 결과를 알 수 없습니다. 같은 요청을 다시 보내기 전에 제공처 대시보드를 확인하세요.",
    L.LOST: "생성은 끝났지만 내려받지 못했습니다. download_video 로 다시 시도할 수 있습니다.",
}


class AppError(Exception):
    """에이전트에게 보여 줄 오류."""


class App:
    def __init__(
        self,
        settings: Settings,
        secrets: Secrets,
        home: Path,
        providers: dict[str, Provider],
        download_client: httpx.AsyncClient,
        *,
        extra_allowed_hosts: list[str] | None = None,
        ledger: Ledger | None = None,
        output_dir: Path | None = None,
    ):
        self.settings = settings
        self.secrets = secrets
        self.home = home
        self.providers = providers
        lim = settings.limits
        self.ledger = ledger or Ledger(home / "data" / "usage.json",
                                       unknown_pending=timedelta(minutes=lim.job_timeout_minutes))
        self.audit = Audit(home / "logs" / "audit.jsonl", secrets.values())
        self.output_dir = (output_dir or home / settings.download.dir).resolve()
        self.allowed_hosts = list(settings.download.allowed_hosts) + list(extra_allowed_hosts or [])
        self._download_client = download_client
        self.collector = Collector(self, lim.poll_interval_sec)

    async def aclose(self) -> None:
        seen: set[int] = set()
        for p in self.providers.values():
            if id(p) not in seen:
                seen.add(id(p))
                await p.aclose()
        await self._download_client.aclose()

    # ── 도구: generate_video ──
    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        resolution: str | None = None,
        duration_sec: int | None = None,
        aspect_ratio: str | None = None,
        request_id: str | None = None,
        force: bool = False,
        confirm: Confirm | None = None,
    ) -> dict[str, Any]:
        """생성 작업을 만든다.

        confirm: 예상 비용이 limits.confirm_above_usd 이상일 때 부르는 콜백. True(진행)·False(거부)·
        None(클라이언트가 확인을 지원하지 않음 → limits.confirm_fallback 적용).
        """
        lim = self.settings.limits
        req = build_request(self.settings, prompt, model, resolution, duration_sec, aspect_ratio)
        request_id = (request_id or "").strip() or None
        if request_id and len(request_id) > 128:
            raise AppError("request_id 는 128자 이하여야 합니다.")
        self._require_provider(req)

        # 중복이면 사용자에게 묻기 전에 돌려준다 (어차피 과금이 없다). reserve 가 같은 검사를 잠금 안에서 한 번 더 한다.
        if not force:
            dup_id = self.ledger.find_duplicate(req, lim, request_id)
            if dup_id is not None:
                job = self.ledger.get(dup_id)
                if job is not None:
                    return self._duplicate_view(dup_id, job, "request_id" if request_id and job.get("request_id") == request_id
                                                else "fingerprint", request_id)

        if confirm is not None and lim.confirm_above_usd > 0 and req.estimated_usd >= lim.confirm_above_usd:
            answer = await confirm(req)
            if answer is None:
                if lim.confirm_fallback == "deny":
                    self.audit.write("confirm_unsupported_denied", model=req.model.alias, estimated_usd=req.estimated_usd)
                    raise AppError(
                        f"예상 비용 ${req.estimated_usd:.2f} 는 확인이 필요한 금액(${lim.confirm_above_usd:.2f} 이상)인데 "
                        "이 클라이언트는 확인 요청(elicitation)을 지원하지 않습니다. 설정 confirm_fallback=allow 로 바꾸거나 "
                        "confirm_above_usd 를 올리세요."
                    )
                log.info("클라이언트가 확인 요청을 지원하지 않아 그대로 진행합니다 (confirm_fallback=allow).")
            elif not answer:
                self.audit.write("confirm_declined", model=req.model.alias, estimated_usd=req.estimated_usd)
                raise AppError(f"사용자가 생성을 취소했습니다 (예상 비용 ${req.estimated_usd:.2f}). 과금되지 않았습니다.")
            else:
                self.audit.write("confirm_accepted", model=req.model.alias, estimated_usd=req.estimated_usd)

        try:
            job_id = self.ledger.reserve(req, lim, request_id=request_id, force=force)
        except L.DuplicateRequest as d:
            return self._duplicate_view(d.job_id, d.job, d.matched_by, request_id)
        return await self._create(job_id, req)

    def _require_provider(self, req: GenerationRequest) -> Provider:
        provider = self.providers.get(req.model.provider)
        if provider is None:
            env = "REPLICATE_API_TOKEN" if req.model.provider == "replicate" else "FAL_KEY"
            raise AppError(f"{req.model.alias} 모델을 쓸 수 없습니다. {env} 가 설정되지 않았습니다 "
                           f"(MCP 등록 env 또는 {self.home / '.env'}).")
        return provider

    def _duplicate_view(self, job_id: str, job: dict[str, Any], matched_by: str, request_id: str | None) -> dict[str, Any]:
        self.audit.write("duplicate", job_id=job_id, matched_by=matched_by, request_id=request_id)
        view = self._view(job_id, job)
        view["duplicate"] = True
        view["matched_by"] = matched_by
        view["message"] = (
            f"{self.settings.limits.duplicate_window_minutes}분 안에 같은 요청이 이미 접수돼 새 작업을 만들지 않았습니다 "
            f"(과금 없음). 기존 작업 상태: {job['status']}. get_video_status 로 확인하세요. "
            "정말 같은 영상을 하나 더 만들려면 force=true 로 다시 부르세요."
        )
        return view

    def _fallback_request(self, req: GenerationRequest) -> GenerationRequest | None:
        """제공처 전환에 쓸 요청. 대체 모델이 없거나 키가 없거나 같은 조건을 허용하지 않으면 None."""
        alias = req.model.fallback_model
        if not self.settings.limits.failover or alias is None:
            return None
        m = self.settings.model(alias)
        if m is None or m.provider not in self.providers:
            return None
        try:
            return build_request(self.settings, req.prompt, alias, req.resolution, req.duration_sec, req.aspect_ratio)
        except PolicyError as e:
            log.info("제공처 전환 불가 (%s → %s): %s", req.model.alias, alias, e)
            return None

    async def _create(self, job_id: str, req: GenerationRequest) -> dict[str, Any]:
        """예약된 작업을 원격에 만든다. 1차 제공처가 확정 거부하면 fallback_model 로 한 번 더 시도한다."""
        failover_from: str | None = None
        first_error: str | None = None
        while True:
            provider = self.providers[req.model.provider]
            base = {"job_id": job_id, "model": req.model.alias, "provider": req.model.provider,
                    "resolution": req.resolution, "duration_sec": req.duration_sec, "estimated_usd": req.estimated_usd}
            try:
                self.audit.write("create_requested", prompt=req.prompt, **base)
                res = await provider.create_task(CreateInput(model_ref=req.model.model_ref, payload=build_payload(req)))
            except CreateUncertainError as e:
                # 과금됐을 수 있다. 다른 제공처로 넘기면 이중 과금이 될 수 있으므로 여기서 멈춘다.
                self.audit.write("create_unknown", job_id=job_id, remote_id=e.remote_id, error=e.message)
                self._record_quietly(job_id, status=L.UNKNOWN, error=e.message, remote_id=e.remote_id)
                raise AppError(f"{e.message} 같은 요청을 다시 보내지 마세요. (job_id={job_id})") from e
            except ProviderError as e:
                self.audit.write("create_failed", job_id=job_id, provider=req.model.provider, error=e.message)
                fb = self._fallback_request(req) if failover_from is None else None
                if fb is not None and self.ledger.reprice(job_id, fb, self.settings.limits):
                    self.audit.write("failover", job_id=job_id, from_model=req.model.alias, to_model=fb.model.alias,
                                     estimated_usd=fb.estimated_usd)
                    log.warning("%s 확정 거부 → %s 로 전환: %s", req.model.alias, fb.model.alias, e.message)
                    failover_from, first_error, req = req.model.alias, e.message, fb
                    continue
                # 기록에 실패하면 reserved 로 남고, 수거기가 나중에 unknown 으로 정리한다 (보수적).
                self._record_quietly(job_id, status=L.CREATE_FAILED, error=e.message)
                msg = e.message if first_error is None else f"{failover_from}: {first_error} / {req.model.alias}: {e.message}"
                raise AppError(msg) from e
            except BaseException as e:
                # 예상 못 한 오류나 취소(클라이언트 타임아웃, 서버 종료):
                # 요청이 나갔는지 모르므로 비용을 남기는 쪽(unknown)으로 정리한다. 동기 호출이라 취소 중에도 기록된다.
                self.audit.write("create_unknown", job_id=job_id, error=type(e).__name__)
                self._record_quietly(job_id, status=L.UNKNOWN, error="생성 요청이 중단됐습니다.")
                if isinstance(e, Exception):
                    log.exception("생성 요청 중 예상하지 못한 오류: %s", job_id)
                    raise AppError(
                        f"생성 요청 중 오류가 났습니다. 같은 요청을 다시 보내지 말고 대시보드를 확인하세요. (job_id={job_id})"
                    ) from e
                raise
            break

        # 생성 성공. 원격 ID 를 먼저 감사 로그(append-only)에 남긴다.
        self.audit.write("created", job_id=job_id, provider=req.model.provider, remote_id=res.remote_id)
        try:
            self.ledger.update(job_id, status=L.QUEUED, remote_id=res.remote_id, remote_urls=res.remote_urls)
        except Exception as e:
            log.error("생성은 접수됐지만 기록 실패: job_id=%s remote_id=%s", job_id, res.remote_id)
            raise AppError(
                f"생성 요청은 접수됐지만 기록에 실패했습니다. 같은 요청을 다시 보내지 마세요. "
                f"(job_id={job_id}, remote_id={res.remote_id}, {self.audit.path} 참고)"
            ) from e
        self.collector.wake()

        try:
            remaining = round(max(0.0, self.settings.limits.daily_max_usd - self.ledger.summary()["today_usd"]), 4)
        except L.LedgerError:
            remaining = None
        out = {
            **base,
            "status": L.QUEUED,
            "remaining_today_usd": remaining,
            "message": "생성 작업을 만들었습니다. 완료되면 서버가 출력 폴더에 자동 저장합니다. "
                       "get_video_status 로 진행 상황을 확인하고, preview_video 로 결과를 눈으로 확인할 수 있습니다.",
        }
        if failover_from is not None:
            out["failover_from"] = failover_from
            out["message"] = (f"{failover_from} 이(가) 요청을 거부해서({first_error}) {req.model.alias} 로 만들었습니다. "
                              f"예상 비용 ${req.estimated_usd:.2f}. " + out["message"])
        return out

    def _record_quietly(self, job_id: str, **fields: Any) -> None:
        """생성 결과 기록. 실패해도 예외를 던지지 않는다 (호출자가 정한 안내 문구를 지키기 위해)."""
        try:
            self.ledger.update(job_id, **fields)
        except Exception:
            log.error("작업 기록 실패 (reserved 로 남음, 수거기가 나중에 unknown 으로 정리): %s %s", job_id, fields)

    def _heartbeat(self, job_id: str) -> None:
        try:
            self.ledger.touch(job_id)
        except L.LedgerError as e:
            log.warning("다운로드 heartbeat 기록 실패 %s: %s", job_id, e)

    def _remove_stale_parts(self, job_id: str) -> None:
        """강제 종료로 남은 이전 시도의 임시 파일을 지운다. 다른 프로세스가 쓰는 중이면 지워지지 않으므로 무시한다."""
        for part in self.output_dir.glob(f"{job_id}.*.part"):
            try:
                part.unlink()
            except OSError:
                pass

    # ── 수거 한 단계 (collector 만 부른다) ──
    async def process_job(self, job_id: str) -> None:
        job = self.ledger.get(job_id)
        if job is None:
            return
        st = job["status"]
        now = self.ledger.now()

        # 생성 도중 중단된 예약 → unknown (비용 포함, 일정 시간 동안 진행 중으로 셈)
        if st == L.RESERVED:
            if now - datetime.fromisoformat(job["updated_at"]) > L.STALE_RESERVED:
                if self.ledger.transition(job_id, {L.RESERVED}, status=L.UNKNOWN,
                                          error="생성 요청이 중단돼 결과를 알 수 없습니다."):
                    self.audit.write("reserved_expired", job_id=job_id)
            return
        if st not in L.COLLECTABLE and st != L.DOWNLOADING:
            return
        if job.get("next_download_at") and now < datetime.fromisoformat(job["next_download_at"]):
            return
        provider = self.providers.get(job["provider"])
        if provider is None:
            return

        lim = self.settings.limits
        age = now - datetime.fromisoformat(job["created_at"])
        try:
            state = await provider.get_task(job)
        except ProviderError as e:
            log.warning("상태 조회 실패 %s: %s", job_id, e.message)
            if age > timedelta(minutes=lim.job_timeout_minutes + lim.download_retry_minutes):
                # 원격 완료를 본 적이 있으면 '받지 못함(lost)', 아니면 '실패'. 회수 중인 다운로드도 정리한다.
                final = L.LOST if job.get("completed_at") else L.FAILED
                if self.ledger.transition(job_id, L.COLLECTABLE | {L.DOWNLOADING}, status=final,
                                          error=f"상태 조회가 계속 실패해 중단했습니다: {e.message}"):
                    self.audit.write("gave_up", job_id=job_id, status=final, error=e.message)
            elif st != L.DOWNLOADING and job.get("error") != e.message:
                # downloading 은 갱신하지 않는다 (updated_at 이 바뀌면 heartbeat 처럼 회수가 미뤄진다)
                self.ledger.transition(job_id, {st}, error=e.message)
            return

        if state.status in ("queued", "running"):
            # 시간 초과는 원격이 아직 끝나지 않았을 때만 적용한다 (이미 끝난 결과를 버리지 않는다).
            if age > timedelta(minutes=lim.job_timeout_minutes):
                try:
                    await provider.cancel(job)
                except ProviderError as e:
                    log.warning("취소 실패 %s: %s", job_id, e.message)
                if self.ledger.transition(job_id, L.COLLECTABLE, status=L.FAILED, error="시간 초과로 취소했습니다."):
                    self.audit.write("timeout", job_id=job_id)
                return
            if st in L.COLLECTABLE and st != state.status:
                self.ledger.transition(job_id, {st}, status=state.status, error=None)
            return

        if state.status == "failed":
            if self.ledger.transition(job_id, L.COLLECTABLE | {L.DOWNLOADING}, status=L.FAILED, error=state.error):
                self.audit.write("failed", job_id=job_id, error=state.error)
            return

        # succeeded
        if not job.get("completed_at"):
            job = self.ledger.transition(job_id, L.COLLECTABLE | {L.DOWNLOADING}, completed_at=now.isoformat()) or job
        claimed = self.ledger.claim_download(job_id)
        if claimed is None:
            return
        completed_at = datetime.fromisoformat(claimed.get("completed_at") or now.isoformat())
        assert state.video_url is not None
        self._remove_stale_parts(job_id)
        try:
            path = await download_video(
                self._download_client, state.video_url, self.output_dir, job_id,
                self.allowed_hosts, self.settings.download.max_bytes,
                on_progress=lambda: self._heartbeat(job_id),
            )
        except Exception as e:
            # DownloadError 와 그 밖의 오류(디스크 가득 참, 파일 잠김 등) 모두 백오프 + 기한을 적용한다.
            if isinstance(e, DownloadError):
                message, gone = e.message, e.gone
            else:
                log.exception("다운로드 중 예상하지 못한 오류: %s", job_id)
                message, gone = f"다운로드 중 오류: {type(e).__name__}", False
            expired = self.ledger.now() - completed_at > timedelta(minutes=lim.download_retry_minutes)
            if gone or expired:
                if self.ledger.transition(job_id, {L.DOWNLOADING}, status=L.LOST, error=message):
                    self.audit.write("lost", job_id=job_id, error=message)
            else:
                attempts = claimed.get("download_attempts", 1)
                backoff = min(DOWNLOAD_BACKOFF_BASE_SEC * 2 ** (attempts - 1), DOWNLOAD_BACKOFF_MAX_SEC)
                log.warning("다운로드 실패, %s초 뒤 다시 시도 %s: %s", backoff, job_id, message)
                self.ledger.transition(job_id, {L.DOWNLOADING}, status=L.RUNNING, error=message,
                                       next_download_at=(self.ledger.now() + timedelta(seconds=backoff)).isoformat())
            return
        except BaseException:
            # 취소(서버 종료 등): 다음에 다시 받도록 되돌린다 (10분 동안 갇히지 않게).
            self.ledger.transition(job_id, {L.DOWNLOADING}, status=L.RUNNING, error="다운로드가 중단됐습니다.")
            raise

        home = self.home.resolve()
        rel = path.relative_to(home).as_posix() if path.is_relative_to(home) else str(path)
        done = self.ledger.transition(job_id, {L.DOWNLOADING}, status=L.SUCCEEDED, file=rel,
                                      error=None, next_download_at=None)
        if done is None:
            return
        self.audit.write("downloaded", job_id=job_id, file=rel)
        try:
            await provider.delete_remote(done)
        except ProviderError as e:
            log.warning("원격 삭제 실패 %s: %s", job_id, e.message)

    # ── 도구: get_video_status ──
    async def status(self, job_id: str, wait_seconds: int = 30) -> dict[str, Any]:
        """수거기를 깨우고 기록을 읽는다. 완료되거나 wait_seconds 가 지나면 반환한다."""
        job = self.ledger.get(job_id)
        if job is None:
            raise AppError(f"작업을 찾을 수 없습니다: {job_id}")
        wait = max(0, min(int(wait_seconds), self.settings.limits.max_wait_seconds))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        if job["status"] not in L.TERMINAL:
            # 한 번만 깨운다. 기다리는 동안은 수거기 주기(poll_interval_sec)를 따른다 (제공처 요청 과다 방지).
            self.collector.wake()
        while True:
            if job["status"] in L.TERMINAL:
                return self._view(job_id, job)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._view(job_id, job)
            await asyncio.sleep(min(STATUS_WAIT_STEP_SEC, remaining))
            job = self.ledger.get(job_id) or job

    # ── 도구: download_video ──
    async def download(self, job_id: str, wait_seconds: int = 30) -> dict[str, Any]:
        job = self.ledger.get(job_id)
        if job is None:
            raise AppError(f"작업을 찾을 수 없습니다: {job_id}")
        st = job["status"]
        if st == L.SUCCEEDED:
            if job.get("file") and (self.home / job["file"]).exists():
                return self._view(job_id, job)
            raise AppError("기록상 완료됐지만 파일이 없습니다. 원격 결과는 이미 삭제됐을 수 있습니다.")
        if st == L.LOST:
            # 내려받지 못한 작업을 다시 수거 대상으로 돌린다 (원격에 결과가 남아 있으면 받아진다).
            lim = self.settings.limits
            if self.ledger.summary()["pending_jobs"] >= lim.max_pending_jobs:
                raise AppError(f"진행 중인 작업이 상한({lim.max_pending_jobs}개)이라 지금은 다시 시도할 수 없습니다.")
            self.ledger.transition(job_id, {L.LOST}, status=L.RUNNING, error=None, next_download_at=None,
                                   completed_at=self.ledger.now().isoformat(), download_attempts=0)
            self.audit.write("download_retry", job_id=job_id)
        elif st not in L.PENDING:
            raise AppError(f"내려받을 수 없는 상태입니다: {st}. {STATUS_MESSAGES.get(st, '')}")
        return await self.status(job_id, wait_seconds)

    def _view(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        view = {
            "job_id": job_id,
            "status": job["status"],
            "model": job["model"],
            "resolution": job["resolution"],
            "duration_sec": job["duration_sec"],
            "estimated_usd": job["estimated_usd"],
            "message": STATUS_MESSAGES.get(job["status"], ""),
        }
        if job["status"] == L.RUNNING and job.get("next_download_at"):
            view["message"] = "생성은 끝났고 다운로드 재시도를 기다리는 중입니다. 잠시 뒤 get_video_status 를 다시 부르세요."
        if job.get("file"):
            view["file"] = str((self.home / job["file"]).resolve())
        if job.get("error"):
            view["error"] = job["error"]
        return view

    # ── 도구: preview_video ──
    async def preview(self, job_id: str) -> tuple[dict[str, Any], bytes | None]:
        """완성된 영상의 프레임 묶음 이미지(JPEG). (상태 뷰, 이미지 또는 None)."""
        job = self.ledger.get(job_id)
        if job is None:
            raise AppError(f"작업을 찾을 수 없습니다: {job_id}")
        view = self._view(job_id, job)
        if job["status"] != L.SUCCEEDED or not job.get("file"):
            view["preview_available"] = False
            view["preview_reason"] = f"아직 완성되지 않았습니다 (상태: {job['status']}). get_video_status 로 기다리세요."
            return view, None
        video = self.home / job["file"]
        if not video.is_file():
            view["preview_available"] = False
            view["preview_reason"] = "기록상 완료됐지만 파일이 없습니다. download_video 로 다시 받아 보세요."
            return view, None
        try:
            data = await contact_sheet(video, int(job["duration_sec"]), self.settings.preview)
        except PreviewUnavailable as e:
            view["preview_available"] = False
            view["preview_reason"] = str(e)
            return view, None
        view["preview_available"] = True
        view["preview_frames"] = self.settings.preview.frames
        view["message"] = (f"프레임 {self.settings.preview.frames}장을 시간 순서(왼쪽 위 → 오른쪽 아래)로 붙인 이미지입니다. "
                           "결과가 요청과 다르면 프롬프트를 고쳐 다시 생성하세요 (과금됩니다).")
        return view, data

    # ── 도구: estimate_video ──
    def estimate(
        self,
        duration_sec: int | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """과금 없이 조건에 맞는 모델별 예상 비용을 싼 순서로 돌려준다. 남은 예산과 함께.

        모델별 검증은 build_request 와 같은 규칙을 쓴다 (실제 생성과 결과가 다르지 않도록).
        """
        lim = self.settings.limits
        s = self.ledger.summary()
        remaining_today = round(max(0.0, lim.daily_max_usd - s["today_usd"]), 4)
        remaining_month = round(max(0.0, lim.monthly_max_usd - s["month_usd"]), 4)
        count_ok = s["today_count"] < lim.daily_max_count
        pending_ok = s["pending_jobs"] < lim.max_pending_jobs
        probe = (prompt or "").strip() or "estimate"  # 프롬프트는 비용에 영향이 없다

        targets = [m for m in self.settings.models if model is None or m.alias == model]
        if not targets:
            raise AppError(f"허용되지 않은 모델: {model!r}. list_models 로 확인하세요.")
        options: list[dict[str, Any]] = []
        unavailable: list[dict[str, str]] = []
        duplicate_of: str | None = None
        for m in targets:
            if m.provider not in self.providers:
                unavailable.append({"model": m.alias, "reason": f"{m.provider} 키가 없습니다."})
                continue
            try:
                req = build_request(self.settings, probe, m.alias, resolution, duration_sec, aspect_ratio)
            except PolicyError as e:
                unavailable.append({"model": m.alias, "reason": str(e)})
                continue
            cost = req.estimated_usd
            options.append({
                "model": m.alias, "provider": m.provider, "resolution": req.resolution,
                "duration_sec": req.duration_sec, "aspect_ratio": req.aspect_ratio, "estimated_usd": cost,
                "fits_budget": count_ok and pending_ok and cost <= remaining_today and cost <= remaining_month,
                "price_checked": m.price_checked, "note": m.note,
            })
            if prompt and duplicate_of is None and lim.duplicate_window_minutes > 0:
                dup = self.ledger.find_duplicate(req, lim)
                if dup is not None:
                    duplicate_of = dup
        options.sort(key=lambda o: (o["estimated_usd"], o["model"]))
        affordable = [o for o in options if o["fits_budget"]]
        out: dict[str, Any] = {
            "options": options,
            "cheapest": affordable[0]["model"] if affordable else None,
            "unavailable": unavailable,
            "remaining_today_usd": remaining_today,
            "remaining_month_usd": remaining_month,
            "today_count": s["today_count"], "daily_max_count": lim.daily_max_count,
            "pending_jobs": s["pending_jobs"], "max_pending_jobs": lim.max_pending_jobs,
            "note": "과금되지 않았습니다. 비용은 config.toml 가격표 기준 예상값이며, 생성은 generate_video 로 합니다.",
        }
        if duplicate_of is not None:
            out["duplicate_of"] = duplicate_of
            out["note"] += f" 같은 요청이 최근에 이미 있습니다 (job_id={duplicate_of}). generate_video 는 그 작업을 돌려줍니다."
        if not affordable:
            why = ("오늘 생성 횟수 상한" if not count_ok else "진행 중 작업 상한" if not pending_ok
                   else "남은 예산")
            out["note"] += f" 지금은 {why} 때문에 생성할 수 있는 모델이 없습니다."
        return out

    # ── 도구: get_usage ──
    def usage(self) -> dict[str, Any]:
        s = self.ledger.summary()
        lim = self.settings.limits
        unknown = sorted(self.ledger.jobs(frozenset({L.UNKNOWN})))
        return {
            "today_count": s["today_count"],
            "daily_max_count": lim.daily_max_count,
            "today_usd": s["today_usd"],
            "daily_max_usd": lim.daily_max_usd,
            "remaining_today_usd": round(max(0.0, lim.daily_max_usd - s["today_usd"]), 4),
            "month_usd": s["month_usd"],
            "monthly_max_usd": lim.monthly_max_usd,
            "pending_jobs": s["pending_jobs"],
            "max_pending_jobs": lim.max_pending_jobs,
            "unknown_jobs": unknown,
            "note": "비용은 config.toml 가격표로 계산한 예상값입니다. 실제 청구액은 제공처 대시보드에서 확인하세요.",
        }

    # ── 도구: list_models ──
    def list_models(self) -> dict[str, Any]:
        lim = self.settings.limits
        models = []
        for m in self.settings.models:
            resolutions = allowed_resolutions(self.settings, m)
            durations = m.allowed_durations(lim.max_duration_sec)
            info: dict[str, Any] = {
                "model": m.alias,
                "provider": m.provider,
                "note": m.note,
                "available": m.provider in self.providers and bool(resolutions) and bool(durations),
                "default": m.alias == self.settings.defaults.model,
                "resolutions": resolutions,
                "durations_sec": durations if m.durations is not None or not durations else [durations[0], durations[-1]],
                "durations_kind": "choices" if m.durations is not None else "range",
                "aspect_ratios": allowed_aspect_ratios(self.settings, m),
                "price_checked": m.price_checked,
            }
            if m.price_per_sec is not None:
                info["price_per_sec_usd"] = {r: m.price_per_sec[r] for r in resolutions}
            else:
                info["price_per_clip_usd"] = {
                    r: {d: p for d, p in (m.price_per_clip or {})[r].items() if int(d) <= lim.max_duration_sec}
                    for r in resolutions
                }
            models.append(info)
        d = self.settings.defaults
        return {
            "models": models,
            "defaults": {"model": d.model, "resolution": d.resolution, "duration_sec": d.duration_sec,
                         "aspect_ratio": d.aspect_ratio},
            "aspect_ratios": lim.aspect_ratios,
        }

    def pending_job_ids(self) -> list[str]:
        return sorted(self.ledger.jobs(L.PENDING))


def build_app(home: Path | None = None) -> App:
    """설정과 환경 변수로 App 을 만든다. CAPPED_VIDEO_PROVIDER=mock 이면 실제 API 를 부르지 않는다."""
    from .providers.fal import FalProvider
    from .providers.mock import MOCK_HOST, MockProvider, mock_transport
    from .providers.replicate import ReplicateProvider

    home = (home or default_home()).resolve()
    # .env 를 먼저 읽어 그 안의 CAPPED_VIDEO_* 값도 설정에 반영되게 한다.
    secrets = load_secrets(home)
    settings = load_settings(home / "config.toml")
    unknown_pending = timedelta(minutes=settings.limits.job_timeout_minutes)

    mode = os.environ.get("CAPPED_VIDEO_PROVIDER", "").strip().lower()
    if mode not in ("", "mock"):
        # 오타("mok")가 조용히 실제 과금 모드가 되지 않게 한다.
        raise ValueError(f"CAPPED_VIDEO_PROVIDER={mode!r}: 비우거나 mock 만 쓸 수 있다")
    if mode == "mock":
        mp = MockProvider()
        log.warning("mock 모드: 실제 API 를 부르지 않습니다.")
        # 가짜 작업이 실제 예산을 깎지 않도록 기록 파일과 출력 폴더를 분리한다.
        return App(settings, secrets, home, {"replicate": mp, "fal": mp},
                   httpx.AsyncClient(transport=mock_transport()), extra_allowed_hosts=[MOCK_HOST],
                   ledger=Ledger(home / "data" / "usage-mock.json", unknown_pending=unknown_pending),
                   output_dir=home / settings.download.dir / "mock")

    providers: dict[str, Provider] = {}
    if secrets.replicate_api_token:
        providers["replicate"] = ReplicateProvider(secrets.replicate_api_token)
    if secrets.fal_key:
        providers["fal"] = FalProvider(secrets.fal_key)
    for m in settings.models:
        if m.provider not in providers:
            log.warning("%s 모델 비활성화: %s 키가 없습니다.", m.alias, m.provider)
    dl = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    return App(settings, secrets, home, providers, dl)
