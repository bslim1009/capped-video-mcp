"""생성 요청 검증(모델 허용 목록, 해상도·길이 상한, 예상 비용)과 모델별 요청 본문 만들기."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .config import ModelConfig, Settings, resolution_rank


class PolicyError(Exception):
    """정책 위반. 메시지는 에이전트에게 그대로 보여 준다."""


@dataclass(frozen=True)
class GenerationRequest:
    model: ModelConfig
    prompt: str
    resolution: str
    duration_sec: int
    aspect_ratio: str
    estimated_usd: float

    def fingerprint(self) -> str:
        """같은 영상을 만드는 요청이면 같은 값. 프롬프트는 앞뒤 공백과 연속 공백만 정리한다 (대소문자는 구분)."""
        prompt = " ".join(self.prompt.split())
        key = "\n".join([prompt, self.model.alias, self.resolution, str(self.duration_sec), self.aspect_ratio])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def allowed_resolutions(settings: Settings, m: ModelConfig) -> list[str]:
    cap = resolution_rank(settings.limits.max_resolution)
    return [r for r in m.resolutions() if resolution_rank(r) <= cap]


def allowed_aspect_ratios(settings: Settings, m: ModelConfig) -> list[str]:
    global_ratios = settings.limits.aspect_ratios
    return [r for r in (m.aspect_ratios or global_ratios) if r in global_ratios]


def estimate_cost(model: ModelConfig, resolution: str, duration_sec: int) -> float:
    price = model.price(resolution, duration_sec)
    if price is None:
        raise PolicyError(f"{model.alias}: {resolution} {duration_sec}초 가격 정보가 없습니다.")
    return round(price, 4)


def _fmt(values: list[Any]) -> str:
    return ", ".join(str(v) for v in values)


def build_request(
    settings: Settings,
    prompt: str,
    model: str | None = None,
    resolution: str | None = None,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
) -> GenerationRequest:
    d, lim = settings.defaults, settings.limits

    alias = model or d.model
    m = settings.model(alias)
    if m is None:
        allowed = _fmt([x.alias for x in settings.models])
        raise PolicyError(f"허용되지 않은 모델: {alias!r}. 사용 가능한 모델: {allowed}")

    prompt = (prompt or "").strip()
    if not prompt:
        raise PolicyError("프롬프트가 비어 있습니다.")
    if len(prompt) > lim.max_prompt_chars:
        raise PolicyError(f"프롬프트가 너무 깁니다 ({len(prompt)}자). 최대 {lim.max_prompt_chars}자입니다.")

    # 해상도: 지정하지 않으면 전역 기본값, 이 모델에 없으면 가장 낮은 해상도
    res_ok = allowed_resolutions(settings, m)
    if not res_ok:
        raise PolicyError(f"{alias} 모델은 해상도 상한({lim.max_resolution}) 안에서 쓸 수 있는 해상도가 없습니다.")
    if resolution is None:
        res = d.resolution if d.resolution in res_ok else res_ok[0]
    else:
        res = resolution
        if res not in m.resolutions():
            raise PolicyError(f"{alias} 모델은 {res} 해상도를 허용하지 않습니다. 허용: {_fmt(res_ok)}")
        if res not in res_ok:
            raise PolicyError(f"해상도 상한은 {lim.max_resolution}입니다. 요청: {res}")

    # 길이: 지정하지 않으면 모델 기본값 → 전역 기본값 → 가장 짧은 길이
    dur_ok = m.allowed_durations(lim.max_duration_sec)
    if not dur_ok:
        raise PolicyError(f"{alias} 모델은 길이 상한({lim.max_duration_sec}초) 안에서 쓸 수 있는 길이가 없습니다.")
    if duration_sec is None:
        if m.default_duration_sec in dur_ok:
            dur = m.default_duration_sec
        else:
            dur = d.duration_sec if d.duration_sec in dur_ok else dur_ok[0]
    else:
        dur = duration_sec
        # bool 은 int 의 하위 타입이라 따로 막는다. 'auto', -1 같은 자동 길이도 여기서 걸린다.
        if isinstance(dur, bool) or not isinstance(dur, int):
            raise PolicyError("duration_sec 는 정수(초)여야 합니다. 자동 길이(auto, -1)는 허용하지 않습니다.")
        if dur not in dur_ok:
            if m.durations is not None:
                raise PolicyError(f"{alias} 모델의 길이는 {_fmt(dur_ok)}초 중 하나만 허용합니다. 요청: {dur}초")
            raise PolicyError(f"길이는 {dur_ok[0]}~{dur_ok[-1]}초만 허용합니다. 요청: {dur}초")

    ar_ok = allowed_aspect_ratios(settings, m)
    ar = aspect_ratio or (d.aspect_ratio if d.aspect_ratio in ar_ok else ar_ok[0])
    if ar not in ar_ok:
        raise PolicyError(f"{alias} 모델에서 허용되지 않은 화면 비율: {ar!r}. 허용: {_fmt(ar_ok)}")

    return GenerationRequest(
        model=m,
        prompt=prompt,
        resolution=res,
        duration_sec=dur,
        aspect_ratio=ar,
        estimated_usd=estimate_cost(m, res, dur),
    )


def build_payload(req: GenerationRequest) -> dict[str, Any]:
    """모델 입력 스키마에 맞춘 요청 본문. 자동 길이·기본 해상도에 맡기지 않도록 항상 명시한다."""
    spec = req.model.input
    payload: dict[str, Any] = {"prompt": req.prompt}
    if spec.duration_format == "int":
        payload["duration"] = req.duration_sec
    elif spec.duration_format == "str_s":
        payload["duration"] = f"{req.duration_sec}s"
    else:
        payload["duration"] = str(req.duration_sec)
    if spec.send_resolution:
        payload["resolution"] = req.resolution
    if spec.send_aspect_ratio:
        payload["aspect_ratio"] = req.aspect_ratio
    if spec.generate_audio is not None:
        payload["generate_audio"] = spec.generate_audio
    payload.update(spec.extra)
    return payload
