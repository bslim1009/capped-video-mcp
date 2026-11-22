"""설정 파일(config.toml)과 비밀값(.env) 로드."""

from __future__ import annotations

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# 해상도는 낮은 것부터. limits.max_resolution 비교에 쓴다.
RESOLUTIONS: tuple[str, ...] = ("360p", "480p", "720p", "768p", "1080p", "4k")
ProviderName = Literal["replicate", "fal"]
# 요청 본문의 duration 형식: 정수(5), 문자열("5"), 초 접미사 문자열("5s")
DurationFormat = Literal["int", "str", "str_s"]
# 모델 입력 종류: 텍스트만 / 첫 프레임 이미지 / 참조(이미지 여러 장 + 오디오)
ModelKind = Literal["text", "image", "reference"]
KIND_NAMES = {"text": "텍스트→영상", "image": "이미지→영상", "reference": "참조→영상"}
# 서버가 직접 채우는 입력 필드. extra 로 덮어쓰지 못하게 한다.
RESERVED_INPUT_KEYS = frozenset({"prompt", "duration", "resolution", "aspect_ratio", "generate_audio"})


def resolution_rank(resolution: str) -> int:
    return RESOLUTIONS.index(resolution)


def expand_path(raw: str) -> str:
    """경로 문자열의 ~ 와 환경 변수(%USERPROFILE%, $HOME)를 푼다. 클라이언트 설정은 이것을 풀어 주지 않는다."""
    return os.path.expanduser(os.path.expandvars(raw))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelInput(_Strict):
    """모델별 요청 본문 형식. 제공처 모델 페이지의 입력 스키마에 맞춘다."""

    duration_format: DurationFormat = "str"
    send_resolution: bool = True        # 해상도 입력이 없는 모델은 false (고정 해상도)
    send_aspect_ratio: bool = True      # 화면 비율 입력이 없는 모델은 false
    generate_audio: bool | None = None  # None 이면 보내지 않는다. 가격표는 이 값 기준이어야 한다
    # 이미지→영상 모델: 입력 이미지 URL 을 담을 필드 이름 (image_url, start_image_url, image 등).
    # 값이 있으면 이미지가 반드시 필요한 모델이고, 없으면 텍스트→영상 모델이다.
    image_field: str | None = Field(default=None, min_length=1)
    # 참조→영상 모델: 참조 이미지 URL 목록·참조 오디오 URL 목록을 담을 필드 이름.
    # ref_images_field 가 있으면 참조 이미지가 1장 이상 필요한 모델이다.
    ref_images_field: str | None = Field(default=None, min_length=1)
    ref_audios_field: str | None = Field(default=None, min_length=1)
    max_ref_images: int = Field(default=30, ge=1)
    max_ref_audios: int = Field(default=10, ge=0)
    # 프롬프트에서 참조를 가리키는 표기: bracket = [Image1] (Replicate), at = @Image1 (fal).
    # 어느 표기로 써도 서버가 이 표기로 바꿔 보낸다 (제공처 전환 때도 프롬프트가 맞도록).
    ref_syntax: Literal["bracket", "at"] = "bracket"
    extra: dict[str, Any] = Field(default_factory=dict)  # 고정으로 함께 보낼 값

    @model_validator(mode="after")
    def _check(self) -> ModelInput:
        reserved = set(RESERVED_INPUT_KEYS)
        if self.image_field is not None and not self.send_aspect_ratio:
            # 비율을 입력 이미지에서 정하는 모델은 고정값("adaptive", "auto")을 extra 로 보낼 수 있다.
            reserved.discard("aspect_ratio")
        if self.image_field is not None and self.ref_images_field is not None:
            raise ValueError("input.image_field 와 ref_images_field 는 함께 쓸 수 없다 (첫 프레임과 참조 모드는 섞을 수 없음)")
        if self.ref_audios_field is not None and self.ref_images_field is None:
            raise ValueError("input.ref_audios_field 는 ref_images_field 와 함께 써야 한다 (오디오는 참조 이미지가 필요)")
        fields = [f for f in (self.image_field, self.ref_images_field, self.ref_audios_field) if f is not None]
        if len(fields) != len(set(fields)):
            raise ValueError(f"input 의 이미지·참조 필드 이름이 겹친다: {fields}")
        for f in fields:
            if f in RESERVED_INPUT_KEYS:
                raise ValueError(f"input 의 이미지·참조 필드에 서버가 채우는 필드를 쓸 수 없다: {f!r}")
            reserved.add(f)
        bad = reserved & set(self.extra)
        if bad:
            raise ValueError(f"input.extra 에 서버가 채우는 필드를 넣을 수 없다: {sorted(bad)}")
        return self


class ModelConfig(_Strict):
    alias: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9.\-]*$")
    provider: ProviderName
    model_ref: str = Field(min_length=3, pattern=r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-/]+$")
    note: str = ""                                  # list_models 에 보여 줄 짧은 설명
    fallback_model: str | None = None               # 이 제공처가 확정 거부하면 대신 쓸 모델 alias
    # 길이: 연속 범위(min~max) 또는 정해진 값 목록(durations) 중 하나
    min_duration_sec: int | None = Field(default=None, ge=1)
    max_duration_sec: int | None = Field(default=None, ge=1)
    durations: list[int] | None = None
    default_duration_sec: int | None = None         # 없으면 전역 기본값, 그것도 안 되면 가장 짧은 길이
    aspect_ratios: list[str] | None = None          # 없으면 전역 목록
    # 가격: 초당(해상도별) 또는 한 편당(해상도별 → 길이별) 중 하나
    price_per_sec: dict[str, float] | None = None
    price_per_clip: dict[str, dict[str, float]] | None = None
    price_checked: str = "unverified"
    input: ModelInput = Field(default_factory=ModelInput)

    @model_validator(mode="after")
    def _check(self) -> ModelConfig:
        a = self.alias
        if self.durations is not None:
            if not self.durations or any(d < 1 for d in self.durations):
                raise ValueError(f"{a}: durations 는 1 이상의 값 목록이어야 한다")
            if self.min_duration_sec is not None or self.max_duration_sec is not None:
                raise ValueError(f"{a}: durations 와 min/max_duration_sec 는 함께 쓸 수 없다")
        else:
            if self.min_duration_sec is None or self.max_duration_sec is None:
                raise ValueError(f"{a}: durations 또는 min/max_duration_sec 가 필요하다")
            if self.min_duration_sec > self.max_duration_sec:
                raise ValueError(f"{a}: min_duration_sec > max_duration_sec")
        if self.default_duration_sec is not None and not self.supports_duration(self.default_duration_sec):
            raise ValueError(f"{a}: default_duration_sec 가 허용 길이에 없다")

        if (self.price_per_sec is None) == (self.price_per_clip is None):
            raise ValueError(f"{a}: price_per_sec 와 price_per_clip 중 하나만 써야 한다")
        table: dict[str, Any] = self.price_per_sec or self.price_per_clip or {}
        if not table:
            raise ValueError(f"{a}: 가격표가 비어 있다")
        for res, value in table.items():
            if res not in RESOLUTIONS:
                raise ValueError(f"{a}: 알 수 없는 해상도 {res!r}")
            prices = value.values() if isinstance(value, dict) else [value]
            if any(p <= 0 for p in prices):
                raise ValueError(f"{a}: {res} 가격은 0보다 커야 한다")
            if isinstance(value, dict):
                for d in value:
                    if not d.isdigit() or not self.supports_duration(int(d)):
                        raise ValueError(f"{a}: price_per_clip 의 길이 {d!r} 가 허용 길이에 없다")
        if not self.input.send_resolution and len(table) != 1:
            raise ValueError(f"{a}: 해상도 입력이 없는 모델은 가격표에 해상도가 하나여야 한다")
        if self.follows_image_ratio and self.aspect_ratios is not None:
            raise ValueError(f"{a}: 입력 이미지의 비율을 따르는 모델(send_aspect_ratio=false)에는 aspect_ratios 를 쓰지 않는다")
        return self

    @property
    def kind(self) -> ModelKind:
        if self.input.ref_images_field is not None:
            return "reference"
        return "image" if self.input.image_field is not None else "text"

    @property
    def needs_image(self) -> bool:
        """첫 프레임 이미지가 필요한 모델."""
        return self.kind == "image"

    @property
    def follows_image_ratio(self) -> bool:
        """이미지→영상 모델 중 화면 비율을 입력 이미지에서 정하는 모델."""
        return self.needs_image and not self.input.send_aspect_ratio

    def supports_duration(self, sec: int) -> bool:
        if self.durations is not None:
            return sec in self.durations
        assert self.min_duration_sec is not None and self.max_duration_sec is not None
        return self.min_duration_sec <= sec <= self.max_duration_sec

    def allowed_durations(self, global_max: int) -> list[int]:
        if self.durations is not None:
            return sorted(d for d in self.durations if d <= global_max)
        assert self.min_duration_sec is not None and self.max_duration_sec is not None
        return list(range(self.min_duration_sec, min(self.max_duration_sec, global_max) + 1))

    def resolutions(self) -> list[str]:
        table = self.price_per_sec or self.price_per_clip or {}
        return sorted(table, key=resolution_rank)

    def price(self, resolution: str, duration_sec: int) -> float | None:
        """예상 비용(USD). 가격표에 없으면 None."""
        if self.price_per_sec is not None:
            per = self.price_per_sec.get(resolution)
            return None if per is None else per * duration_sec
        clip = (self.price_per_clip or {}).get(resolution, {}).get(str(duration_sec))
        return clip


class Defaults(_Strict):
    model: str
    image_model: str | None = None     # image_path 를 주고 모델을 지정하지 않았을 때 쓸 이미지→영상 모델
    reference_model: str | None = None  # 참조 파일을 주고 모델을 지정하지 않았을 때 쓸 참조→영상 모델
    resolution: str
    duration_sec: int = Field(ge=1)
    aspect_ratio: str


class Limits(_Strict):
    max_resolution: str
    max_duration_sec: int = Field(ge=1)
    daily_max_count: int = Field(ge=1)
    daily_max_usd: float = Field(gt=0)
    monthly_max_usd: float = Field(gt=0)
    max_pending_jobs: int = Field(ge=1)
    max_wait_seconds: int = Field(ge=0, le=300)
    job_timeout_minutes: int = Field(ge=1)
    download_retry_minutes: int = Field(ge=1)
    poll_interval_sec: float = Field(gt=0)
    max_prompt_chars: int = Field(ge=1)
    max_image_bytes: int = Field(default=20_971_520, gt=0)   # 이미지 파일 하나의 크기 상한
    max_audio_bytes: int = Field(default=15_728_640, gt=0)   # 오디오 파일 하나의 크기 상한 (fal 15MB)
    duplicate_window_minutes: int = Field(default=10, ge=0)  # 0 이면 중복 감지 끔
    confirm_above_usd: float = Field(default=0.0, ge=0)      # 0 이면 확인 요청 안 함
    confirm_fallback: Literal["allow", "deny"] = "allow"
    failover: bool = True
    aspect_ratios: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Limits:
        if self.max_resolution not in RESOLUTIONS:
            raise ValueError(f"limits.max_resolution: 알 수 없는 해상도 {self.max_resolution!r}")
        return self


class PreviewConfig(_Strict):
    ffmpeg: str = ""                 # 비우면 PATH
    frames: int = Field(default=6, ge=1, le=12)
    frame_width: int = Field(default=320, ge=64, le=1920)

    @field_validator("ffmpeg")
    @classmethod
    def _expand(cls, v: str) -> str:
        return expand_path(v) if v else v


class DownloadConfig(_Strict):
    dir: str = "output"                 # 상대 경로면 데이터 폴더 기준
    max_bytes: int = Field(gt=0)
    allowed_hosts: list[str]

    @field_validator("dir")
    @classmethod
    def _expand(cls, v: str) -> str:
        return expand_path(v)


class Settings(_Strict):
    defaults: Defaults
    limits: Limits
    download: DownloadConfig
    preview: PreviewConfig = Field(default_factory=PreviewConfig)
    models: list[ModelConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Settings:
        aliases = [m.alias for m in self.models]
        if len(aliases) != len(set(aliases)):
            raise ValueError("models: alias 가 중복된다")
        if self.defaults.model not in aliases:
            raise ValueError(f"defaults.model {self.defaults.model!r} 이 허용 목록에 없다")
        for key, kind in (("model", "text"), ("image_model", "image"), ("reference_model", "reference")):
            alias = getattr(self.defaults, key)
            if alias is None:
                continue
            m = self.model(alias)
            if m is None:
                raise ValueError(f"defaults.{key} {alias!r} 이 허용 목록에 없다")
            if m.kind != kind:
                raise ValueError(f"defaults.{key} {alias!r} 은 {KIND_NAMES[m.kind]} 모델이다. {KIND_NAMES[kind]} 모델을 쓴다")
        # 모델에 없는 해상도는 정책이 대체하지만, 아예 모르는 값은 오타다.
        if self.defaults.resolution not in RESOLUTIONS:
            raise ValueError(f"defaults.resolution: 알 수 없는 해상도 {self.defaults.resolution!r}")
        for m in self.models:
            fb = m.fallback_model
            if fb is not None and (fb == m.alias or fb not in aliases):
                raise ValueError(f"{m.alias}: fallback_model {fb!r} 이 허용 목록에 없거나 자기 자신이다")
            if fb is not None and self.model(fb).kind != m.kind:  # type: ignore[union-attr]
                raise ValueError(f"{m.alias}: fallback_model {fb!r} 은 입력 종류(텍스트/이미지/참조)가 같아야 한다")
        if self.defaults.aspect_ratio not in self.limits.aspect_ratios:
            raise ValueError("defaults.aspect_ratio 가 limits.aspect_ratios 에 없다")
        return self

    def model(self, alias: str) -> ModelConfig | None:
        return next((m for m in self.models if m.alias == alias), None)


@dataclass(frozen=True)
class Secrets:
    replicate_api_token: str | None
    fal_key: str | None

    def for_provider(self, provider: str) -> str | None:
        return {"replicate": self.replicate_api_token, "fal": self.fal_key}.get(provider)

    def values(self) -> list[str]:
        return [v for v in (self.replicate_api_token, self.fal_key) if v]


def default_home() -> Path:
    """데이터 폴더. 사용자 config.toml, .env, output/, data/, logs/ 가 여기 있다.

    1. CAPPED_VIDEO_HOME 환경 변수 (절대 경로. 클라이언트가 띄우는 작업 폴더는 예측할 수 없다)
    2. 소스 checkout 에서 실행 중이면 프로젝트 루트 (`uv sync` 의 editable 설치)
    3. ~/.capped-video-mcp (uvx·pip 로 설치했을 때)

    현재 폴더는 보지 않는다. 클라이언트가 띄운 폴더에 다른 도구의 config.toml 이 있을 수 있다.
    """
    env = os.environ.get("CAPPED_VIDEO_HOME", "").strip()
    if env:
        home = Path(expand_path(env))
        if not home.is_absolute():
            raise ValueError(f"CAPPED_VIDEO_HOME={env!r}: 절대 경로여야 한다")
        return home.resolve()
    pkg = Path(__file__).resolve().parent
    if pkg.parent.name == "src" and (pkg.parent.parent / "pyproject.toml").is_file():
        return pkg.parent.parent
    return Path.home() / ".capped-video-mcp"


def default_config_text() -> str:
    """패키지에 들어 있는 기본 config.toml."""
    return resources.files(__package__).joinpath("config.toml").read_text(encoding="utf-8")


# 환경 변수 → (설정 표, 키, 형). MCP 클라이언트에 서버를 등록할 때 값을 넘기는 용도다.
ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "CAPPED_VIDEO_DEFAULT_MODEL": ("defaults", "model", str),
    "CAPPED_VIDEO_DEFAULT_IMAGE_MODEL": ("defaults", "image_model", str),
    "CAPPED_VIDEO_DEFAULT_REFERENCE_MODEL": ("defaults", "reference_model", str),
    "CAPPED_VIDEO_DEFAULT_RESOLUTION": ("defaults", "resolution", str),
    "CAPPED_VIDEO_DEFAULT_DURATION_SEC": ("defaults", "duration_sec", int),
    "CAPPED_VIDEO_DEFAULT_ASPECT_RATIO": ("defaults", "aspect_ratio", str),
    "CAPPED_VIDEO_MAX_RESOLUTION": ("limits", "max_resolution", str),
    "CAPPED_VIDEO_MAX_DURATION_SEC": ("limits", "max_duration_sec", int),
    "CAPPED_VIDEO_DAILY_MAX_COUNT": ("limits", "daily_max_count", int),
    "CAPPED_VIDEO_DAILY_MAX_USD": ("limits", "daily_max_usd", float),
    "CAPPED_VIDEO_MONTHLY_MAX_USD": ("limits", "monthly_max_usd", float),
    "CAPPED_VIDEO_MAX_PENDING_JOBS": ("limits", "max_pending_jobs", int),
    "CAPPED_VIDEO_MAX_IMAGE_BYTES": ("limits", "max_image_bytes", int),
    "CAPPED_VIDEO_MAX_AUDIO_BYTES": ("limits", "max_audio_bytes", int),
    "CAPPED_VIDEO_DUPLICATE_WINDOW_MINUTES": ("limits", "duplicate_window_minutes", int),
    "CAPPED_VIDEO_CONFIRM_ABOVE_USD": ("limits", "confirm_above_usd", float),
    "CAPPED_VIDEO_CONFIRM_FALLBACK": ("limits", "confirm_fallback", str),
    "CAPPED_VIDEO_FAILOVER": ("limits", "failover", bool),
    "CAPPED_VIDEO_FFMPEG": ("preview", "ffmpeg", str),
    "CAPPED_VIDEO_OUTPUT_DIR": ("download", "dir", str),   # ~ 와 환경 변수는 DownloadConfig 가 푼다
}
SECRET_ENV = ("REPLICATE_API_TOKEN", "FAL_KEY")


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """표(dict)는 키별로 합치고, 그 밖의 값(목록 포함)은 통째로 바꾼다."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_value(name: str, raw: str, kind: type) -> Any:
    if kind is str:
        return raw
    if kind is bool:
        low = raw.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"환경 변수 {name}={raw!r}: true/false 여야 한다")
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f"환경 변수 {name}={raw!r}: {'정수' if kind is int else '숫자'}가 아니다") from None
    if kind is float and not math.isfinite(value):
        raise ValueError(f"환경 변수 {name}={raw!r}: 유한한 숫자여야 한다")
    return value


def apply_env_overrides(data: dict[str, Any], environ: Mapping[str, str]) -> list[str]:
    """environ 의 CAPPED_VIDEO_* 값을 data 에 덮어쓰고, 적용한 변수 이름을 돌려준다. 빈 값은 무시한다."""
    applied = []
    for name, (table, key, kind) in ENV_OVERRIDES.items():
        raw = environ.get(name, "").strip()
        if not raw:
            continue
        data.setdefault(table, {})[key] = _env_value(name, raw, kind)
        applied.append(name)
    return applied


def load_settings(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    """기본 설정 ← path(있으면) ← 환경 변수 순서로 덮어써서 검증한다.

    path 에는 바꿀 항목만 써도 된다. [[models]] 를 쓰면 모델 목록 전체가 바뀐다.
    """
    data = tomllib.loads(default_config_text())
    context = []
    if path is not None and path.is_file():
        try:
            with path.open("rb") as f:
                user = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            # Windows 경로는 작은따옴표('D:\Videos')나 / 로 쓴다.
            raise ValueError(f"{path}: {e}") from e
        data = _merge(data, user)
        # 모델 목록을 통째로 바꾼 사용자 설정에는 기본 이미지·참조 모델이 없을 수 있다.
        # 사용자가 직접 정하지 않았으면 기본값을 버린다 (그때는 model 을 지정해야 함).
        user_defaults = user.get("defaults")
        user_defaults = user_defaults if isinstance(user_defaults, dict) else {}
        if "models" in user and isinstance(data.get("models"), list) and isinstance(data.get("defaults"), dict):
            aliases = {m.get("alias") for m in data["models"] if isinstance(m, dict)}
            for key in ("image_model", "reference_model"):
                if key not in user_defaults and data["defaults"].get(key) not in aliases:
                    data["defaults"].pop(key, None)
        context.append(f"사용자 설정 파일: {path}")
    applied = apply_env_overrides(data, os.environ if environ is None else environ)
    if applied:
        context.append(f"적용된 환경 변수: {', '.join(applied)}")
    limits = dict(data.get("limits", {}))
    if "aspect_ratios" in data:
        limits["aspect_ratios"] = data["aspect_ratios"]
    data["limits"] = limits
    data.pop("aspect_ratios", None)
    try:
        return Settings.model_validate(data)
    except ValidationError as e:
        # 오류 형은 그대로 두고 어디서 온 값인지만 덧붙인다 (traceback 에 함께 나온다).
        for line in context:
            e.add_note(line)
        raise


def load_secrets(home: Path) -> Secrets:
    """home/.env 를 환경 변수로 읽고 키를 돌려준다. 이미 설정된 환경 변수가 우선한다."""
    # 등록 설정의 "${VAR:-}" 처럼 빈 값으로 넘어온 변수는 없는 것으로 본다.
    # 그대로 두면 load_dotenv(override=False) 가 .env 의 값을 건너뛴다 (키가 꺼지거나 예산 상한이 기본값으로 돌아감).
    for name in (*SECRET_ENV, "CAPPED_VIDEO_PROVIDER", *ENV_OVERRIDES):
        if name in os.environ and not os.environ[name].strip():
            del os.environ[name]
    load_dotenv(home / ".env", override=False)
    return Secrets(
        replicate_api_token=os.environ.get("REPLICATE_API_TOKEN") or None,
        fal_key=os.environ.get("FAL_KEY") or None,
    )
