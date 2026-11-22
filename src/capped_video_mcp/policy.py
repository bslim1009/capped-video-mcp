"""생성 요청 검증(모델 허용 목록, 해상도·길이 상한, 예상 비용)과 모델별 요청 본문 만들기."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .config import ModelConfig, ModelKind, Settings, resolution_rank

if TYPE_CHECKING:
    from .media import MediaFile

# 입력 이미지의 비율을 따르는 모델의 aspect_ratio 기록값
IMAGE_RATIO = "auto"
# 참조 오디오 길이 합계 상한 (Replicate 30초, fal 30.2초 → 둘 다 만족하는 값)
AUDIO_TOTAL_MAX_SEC = 30.0
# 프롬프트의 참조 표기: [Image1] (Replicate) 또는 @Image1 (fal)
# 번호는 1~999 (앞자리 0·유니코드 숫자 제외). @ 표기는 앞에 글자가 붙은 경우(이메일 a@image1.com)를 제외한다.
_REF_TAG = re.compile(
    r"\[(image|audio|video)([1-9][0-9]{0,2})\]|(?<![0-9A-Za-z_.@])@(image|audio|video)([1-9][0-9]{0,2})(?![0-9A-Za-z_])",
    re.IGNORECASE | re.ASCII,
)


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
    image: MediaFile | None = None                 # 첫 프레임 (이미지→영상)
    ref_images: tuple[MediaFile, ...] = ()         # 참조 이미지 ([Image1] 부터 순서대로)
    ref_audios: tuple[MediaFile, ...] = ()         # 참조 오디오 ([Audio1] 부터 순서대로)

    def fingerprint(self) -> str:
        """같은 영상을 만드는 요청이면 같은 값. 프롬프트는 앞뒤 공백과 연속 공백만 정리한다 (대소문자는 구분).

        입력 파일은 경로가 아니라 내용(sha256)으로 구분한다. 참조는 순서도 구분한다 ([Image1] 이 바뀌므로).
        """
        prompt = " ".join(self.prompt.split())
        if self.ref_images or self.ref_audios:
            prompt = rewrite_ref_tags(prompt, "bracket")   # [Image1] 과 @Image1 은 같은 요청이다
        parts = [prompt, self.model.alias, self.resolution, str(self.duration_sec), self.aspect_ratio]
        if self.image is not None:
            parts.append(self.image.sha256)
        if self.ref_images:
            parts.append("ref_images:" + ",".join(f.sha256 for f in self.ref_images))
        if self.ref_audios:
            parts.append("ref_audios:" + ",".join(f.sha256 for f in self.ref_audios))
        key = "\n".join(parts)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def input_files(self) -> list[MediaFile]:
        return [f for f in (self.image, *self.ref_images, *self.ref_audios) if f is not None]

    def reference_labels(self) -> dict[str, str]:
        return reference_labels(self.ref_images, self.ref_audios)


def reference_labels(ref_images: tuple[MediaFile, ...], ref_audios: tuple[MediaFile, ...]) -> dict[str, str]:
    """프롬프트 표기 → 파일 경로. {"[Image1]": "C:/.../a.png", "[Audio1]": ...}"""
    out = {f"[Image{i}]": f.path for i, f in enumerate(ref_images, 1)}
    out.update({f"[Audio{i}]": f.path for i, f in enumerate(ref_audios, 1)})
    return out


@dataclass(frozen=True)
class UploadedInputs:
    """제공처 저장소에 올린 입력 파일의 URL."""

    image: str | None = None
    ref_images: tuple[str, ...] = ()
    ref_audios: tuple[str, ...] = ()


def allowed_resolutions(settings: Settings, m: ModelConfig) -> list[str]:
    cap = resolution_rank(settings.limits.max_resolution)
    return [r for r in m.resolutions() if resolution_rank(r) <= cap]


def allowed_aspect_ratios(settings: Settings, m: ModelConfig) -> list[str]:
    if m.follows_image_ratio:
        return [IMAGE_RATIO]
    global_ratios = settings.limits.aspect_ratios
    return [r for r in (m.aspect_ratios or global_ratios) if r in global_ratios]


def estimate_cost(model: ModelConfig, resolution: str, duration_sec: int) -> float:
    price = model.price(resolution, duration_sec)
    if price is None:
        raise PolicyError(f"{model.alias}: {resolution} {duration_sec}초 가격 정보가 없습니다.")
    return round(price, 4)


def _fmt(values: list[Any]) -> str:
    return ", ".join(str(v) for v in values)


def models_of(settings: Settings, kind: ModelKind) -> list[str]:
    return [m.alias for m in settings.models if m.kind == kind]


def ref_tags(prompt: str) -> list[tuple[str, int]]:
    """프롬프트의 참조 표기 목록 [("image", 1), ("audio", 2), ...]."""
    out = []
    for mt in _REF_TAG.finditer(prompt):
        kind = (mt.group(1) or mt.group(3)).lower()
        out.append((kind, int(mt.group(2) or mt.group(4))))
    return out


def rewrite_ref_tags(prompt: str, syntax: str) -> str:
    """[Image1] / @Image1 을 모델 표기로 바꾼다."""
    def sub(mt: re.Match[str]) -> str:
        kind = (mt.group(1) or mt.group(3)).capitalize()
        n = mt.group(2) or mt.group(4)
        return f"[{kind}{n}]" if syntax == "bracket" else f"@{kind}{n}"
    return _REF_TAG.sub(sub, prompt)


def _select_model(settings: Settings, model: str | None, image: MediaFile | None, has_refs: bool) -> ModelConfig:
    d = settings.defaults
    if model:
        alias = model
    elif has_refs:
        if d.reference_model is None:
            raise PolicyError("참조 파일을 줄 때는 model 에 참조→영상 모델을 지정하세요. "
                              f"사용 가능한 모델: {_fmt(models_of(settings, 'reference'))}")
        alias = d.reference_model
    elif image is not None:
        if d.image_model is None:
            raise PolicyError("이미지를 줄 때는 model 에 이미지→영상 모델을 지정하세요. "
                              f"사용 가능한 모델: {_fmt(models_of(settings, 'image'))}")
        alias = d.image_model
    else:
        alias = d.model
    m = settings.model(alias)
    if m is None:
        allowed = _fmt([x.alias for x in settings.models])
        raise PolicyError(f"허용되지 않은 모델: {alias!r}. 사용 가능한 모델: {allowed}")
    return m


def _check_inputs(settings: Settings, m: ModelConfig, prompt: str, image: MediaFile | None,
                  ref_images: tuple[MediaFile, ...], ref_audios: tuple[MediaFile, ...]) -> None:
    a, has_refs = m.alias, bool(ref_images or ref_audios)
    others = (f"이미지→영상 모델: {_fmt(models_of(settings, 'image'))}. "
              f"참조→영상 모델: {_fmt(models_of(settings, 'reference'))}")
    if m.kind == "text":
        if image is not None or has_refs:
            raise PolicyError(f"{a} 는 텍스트→영상 모델이라 이미지·참조 파일을 받지 않습니다. {others}")
        return
    if m.kind == "image":
        if has_refs:
            raise PolicyError(f"{a} 는 첫 프레임 이미지(image_path) 하나만 받습니다. "
                              f"참조 이미지·오디오는 참조→영상 모델로 보내세요: {_fmt(models_of(settings, 'reference'))}")
        if image is None:
            raise PolicyError(f"{a} 는 이미지→영상 모델입니다. image_path 로 첫 프레임 이미지를 주세요.")
        return
    # 참조→영상
    spec = m.input
    if image is not None:
        raise PolicyError(f"{a} 는 참조→영상 모델이라 첫 프레임(image_path)과 함께 쓸 수 없습니다. "
                          "그 이미지를 reference_image_paths 에 넣으세요.")
    if not ref_images:
        raise PolicyError(f"{a} 는 참조 이미지가 1장 이상 필요합니다 (reference_image_paths 또는 reference_dir).")
    if len(ref_images) > spec.max_ref_images:
        raise PolicyError(f"{a} 의 참조 이미지는 {spec.max_ref_images}장까지입니다. 요청: {len(ref_images)}장")
    if ref_audios:
        if spec.ref_audios_field is None:
            raise PolicyError(f"{a} 는 참조 오디오를 받지 않습니다.")
        if len(ref_audios) > spec.max_ref_audios:
            raise PolicyError(f"{a} 의 참조 오디오는 {spec.max_ref_audios}개까지입니다. 요청: {len(ref_audios)}개")
        total = sum(f.duration_sec or 0 for f in ref_audios)
        if total > AUDIO_TOTAL_MAX_SEC:
            raise PolicyError(f"참조 오디오 길이 합계가 {total:.1f}초입니다. {AUDIO_TOTAL_MAX_SEC:.0f}초 이하여야 합니다.")
    counts = {"image": len(ref_images), "audio": len(ref_audios)}
    for kind, n in ref_tags(prompt):
        if kind == "video":
            raise PolicyError("참조 영상([Video1])은 지원하지 않습니다. 참조 이미지와 오디오만 쓸 수 있습니다.")
        if not 1 <= n <= counts[kind]:
            label = "Image" if kind == "image" else "Audio"
            raise PolicyError(f"프롬프트의 [{label}{n}] 에 해당하는 참조 {'이미지' if kind == 'image' else '오디오'}가 "
                              f"없습니다 (받은 개수: {counts[kind]}).")


def build_request(
    settings: Settings,
    prompt: str,
    model: str | None = None,
    resolution: str | None = None,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
    image: MediaFile | None = None,
    ref_images: tuple[MediaFile, ...] = (),
    ref_audios: tuple[MediaFile, ...] = (),
) -> GenerationRequest:
    d, lim = settings.defaults, settings.limits
    ref_images, ref_audios = tuple(ref_images), tuple(ref_audios)
    m = _select_model(settings, model, image, bool(ref_images or ref_audios))
    alias = m.alias

    prompt = (prompt or "").strip()
    if not prompt:
        raise PolicyError("프롬프트가 비어 있습니다.")
    if len(prompt) > lim.max_prompt_chars:
        raise PolicyError(f"프롬프트가 너무 깁니다 ({len(prompt)}자). 최대 {lim.max_prompt_chars}자입니다.")
    _check_inputs(settings, m, prompt, image, ref_images, ref_audios)

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
    if m.follows_image_ratio and aspect_ratio and aspect_ratio != IMAGE_RATIO:
        raise PolicyError(f"{alias} 는 입력 이미지의 화면 비율을 따릅니다. aspect_ratio 를 지정하지 마세요.")
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
        image=image,
        ref_images=ref_images,
        ref_audios=ref_audios,
    )


def build_payload(req: GenerationRequest, uploaded: UploadedInputs | None = None) -> dict[str, Any]:
    """모델 입력 스키마에 맞춘 요청 본문. 자동 길이·기본 해상도에 맡기지 않도록 항상 명시한다.

    uploaded: 이미지·참조 파일이 있는 요청이면 제공처에 올린 URL (필수).
    """
    spec = req.model.input
    up = uploaded or UploadedInputs()
    prompt = req.prompt
    if req.model.kind == "reference":
        prompt = rewrite_ref_tags(prompt, spec.ref_syntax)
    payload: dict[str, Any] = {"prompt": prompt}
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
    if spec.image_field is not None:
        if not up.image:
            raise ValueError(f"{req.model.alias}: 입력 이미지 URL 이 없다")
        payload[spec.image_field] = up.image
    if spec.ref_images_field is not None:
        if len(up.ref_images) != len(req.ref_images) or not up.ref_images:
            raise ValueError(f"{req.model.alias}: 참조 이미지 URL 개수가 맞지 않는다")
        payload[spec.ref_images_field] = list(up.ref_images)
    if req.ref_audios:
        if spec.ref_audios_field is None or len(up.ref_audios) != len(req.ref_audios):
            raise ValueError(f"{req.model.alias}: 참조 오디오 URL 개수가 맞지 않는다")
        payload[spec.ref_audios_field] = list(up.ref_audios)
    payload.update(spec.extra)
    return payload
