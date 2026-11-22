"""모델별 요청 본문·검증·가격 테스트.

기대값은 각 제공처 모델 페이지(fal llms.txt, 2026-11-22)의 입력 스키마에서 옮겨 적었다.
코드가 만든 값을 복사한 것이 아니다.
"""

import pytest
from pydantic import ValidationError

from capped_video_mcp.config import ModelConfig
from capped_video_mcp.policy import PolicyError, build_payload, build_request

P = "a red paper boat"


@pytest.mark.parametrize(
    "alias, kwargs, expected_payload, expected_usd",
    [
        # Replicate: duration 정수
        ("seedance-2.5", {}, {"prompt": P, "duration": 5, "resolution": "480p", "aspect_ratio": "16:9",
                              "generate_audio": True}, 0.514),
        # fal Seedance: duration 문자열 ("4"~"30", 기본 "auto" 라 항상 보냄)
        ("seedance-2.5-fal", {}, {"prompt": P, "duration": "5", "resolution": "480p", "aspect_ratio": "16:9",
                                  "generate_audio": True}, 1.1025),
        # Kling v3: duration "3"~"15" 문자열, 해상도 필드 없음, 소리 켬 $0.126/초
        ("kling-3.0", {}, {"prompt": P, "duration": "5", "aspect_ratio": "16:9", "generate_audio": True}, 0.63),
        # Wan 2.5: duration "5"/"10", 해상도 기본 1080p 라 반드시 명시, 480p $0.05/초
        ("wan-2.5", {}, {"prompt": P, "duration": "5", "resolution": "480p", "aspect_ratio": "16:9"}, 0.25),
        ("wan-2.5", {"duration_sec": 10, "resolution": "720p"},
         {"prompt": P, "duration": "10", "resolution": "720p", "aspect_ratio": "16:9"}, 1.0),
        # Veo 3.1 Fast: duration "4s"/"6s"/"8s" (기본 "8s"), 720p 소리 켬 $0.15/초
        ("veo-3.1-fast", {}, {"prompt": P, "duration": "4s", "resolution": "720p", "aspect_ratio": "16:9",
                              "generate_audio": True}, 0.6),
        # Veo 3.1 Lite: 720p 소리 켬 $0.05/초
        ("veo-3.1-lite", {"duration_sec": 8, "aspect_ratio": "9:16"},
         {"prompt": P, "duration": "8s", "resolution": "720p", "aspect_ratio": "9:16", "generate_audio": True}, 0.4),
        # Hailuo 2.3 Standard: duration "6"/"10" 만, 해상도·비율 필드 없음, 한 편당 $0.28 / $0.56
        ("hailuo-2.3", {}, {"prompt": P, "duration": "6"}, 0.28),
        ("hailuo-2.3", {"duration_sec": 10}, {"prompt": P, "duration": "10"}, 0.56),
        # Gemini Omni Flash 1.2: duration 정수 3~15, 360p $0.0273/초
        ("gemini-omni-1.2-flash", {}, {"prompt": P, "duration": 5, "resolution": "360p", "aspect_ratio": "16:9"},
         0.1365),
        ("gemini-omni-1.2-flash", {"resolution": "720p", "duration_sec": 3},
         {"prompt": P, "duration": 3, "resolution": "720p", "aspect_ratio": "16:9"}, 0.2457),
    ],
)
def test_payload_and_cost(settings, alias, kwargs, expected_payload, expected_usd):
    req = build_request(settings, P, model=alias, **kwargs)
    assert build_payload(req) == expected_payload
    assert req.estimated_usd == pytest.approx(expected_usd)


@pytest.mark.parametrize(
    "alias, kwargs, msg",
    [
        ("wan-2.5", {"duration_sec": 6}, "5, 10초 중 하나"),
        ("veo-3.1-fast", {"duration_sec": 5}, "4, 6, 8초 중 하나"),
        ("hailuo-2.3", {"duration_sec": 5}, "6, 10초 중 하나"),
        ("wan-2.5", {"resolution": "1080p"}, "해상도 상한"),            # 모델은 지원하지만 전역 상한 밖
        ("veo-3.1-fast", {"resolution": "480p"}, "480p 해상도를 허용하지 않습니다"),
        ("gemini-omni-1.2-flash", {"resolution": "4k"}, "해상도 상한"),
        ("kling-3.0", {"duration_sec": 11}, "3~10초"),                  # 모델은 15초까지지만 전역 상한 10초
        ("kling-3.0", {"duration_sec": 2}, "3~10초"),
        ("veo-3.1-lite", {"aspect_ratio": "1:1"}, "화면 비율"),
        ("hailuo-2.3", {"aspect_ratio": "9:16"}, "화면 비율"),
        ("gemini-omni-1.2-flash", {"duration_sec": "auto"}, "자동 길이"),
    ],
)
def test_model_specific_rejections(settings, alias, kwargs, msg):
    with pytest.raises(PolicyError, match=msg):
        build_request(settings, P, model=alias, **kwargs)


def test_hailuo_768p_needs_cap_at_least_768p(settings):
    low_cap = settings.model_copy(update={"limits": settings.limits.model_copy(update={"max_resolution": "720p"})})
    with pytest.raises(PolicyError, match="쓸 수 있는 해상도가 없습니다"):
        build_request(low_cap, P, model="hailuo-2.3")


def test_hailuo_10s_blocked_when_cap_below(settings):
    short = settings.model_copy(update={"limits": settings.limits.model_copy(update={"max_duration_sec": 8})})
    with pytest.raises(PolicyError, match="6초 중 하나"):
        build_request(short, P, model="hailuo-2.3", duration_sec=10)


# ── 설정 검증 ──

BASE = {"alias": "m", "provider": "fal", "model_ref": "a/b"}


@pytest.mark.parametrize(
    "extra, msg",
    [
        ({"min_duration_sec": 4, "max_duration_sec": 8, "price_per_sec": {"480p": 1}, "price_per_clip": {"480p": {"4": 1}}},
         "하나만"),
        ({"min_duration_sec": 4, "max_duration_sec": 8}, "하나만"),
        ({"price_per_sec": {"480p": 1}}, "durations 또는"),
        ({"durations": [5], "min_duration_sec": 4, "max_duration_sec": 8, "price_per_sec": {"480p": 1}}, "함께 쓸 수 없다"),
        ({"durations": [6, 10], "price_per_clip": {"768p": {"7": 1}}}, "허용 길이에 없다"),
        ({"durations": [5], "price_per_sec": {"8k": 1}}, "알 수 없는 해상도"),
        ({"durations": [5], "price_per_sec": {"480p": 0}}, "0보다"),
        ({"durations": [5], "price_per_sec": {"480p": 1, "720p": 2}, "input": {"send_resolution": False}},
         "해상도가 하나"),
        ({"durations": [5], "price_per_sec": {"480p": 1}, "input": {"extra": {"duration": "auto"}}}, "서버가 채우는"),
        ({"durations": [5], "price_per_sec": {"480p": 1}, "input": {"duration_format": "float"}}, "duration_format"),
        ({"durations": [5], "price_per_sec": {"480p": 1}, "default_duration_sec": 7}, "default_duration_sec"),
    ],
)
def test_invalid_model_config(extra, msg):
    with pytest.raises(ValidationError, match=msg):
        ModelConfig.model_validate({**BASE, **extra})


def test_extra_input_is_sent(settings):
    m = ModelConfig.model_validate({**BASE, "durations": [5], "price_per_sec": {"480p": 0.1},
                                    "input": {"extra": {"prompt_optimizer": False}}})
    s = settings.model_copy(update={"models": [*settings.models, m]})
    payload = build_payload(build_request(s, P, model="m"))
    assert payload["prompt_optimizer"] is False


async def test_list_models_shows_each_model(make_app):
    app = make_app()
    out = app.list_models()
    info = {m["model"]: m for m in out["models"]}
    assert len(info) == 18
    assert out["defaults"]["image_model"] == "seedance-2.5-i2v"
    assert out["defaults"]["reference_model"] == "seedance-2.5-ref"
    assert info["seedance-2.5-ref"]["input"] == "reference"
    assert info["kling-3.0"]["input"] == "text" and info["kling-3.0-i2v"]["input"] == "image"
    assert info["kling-3.0-i2v"]["aspect_ratios"] == ["auto"]          # 이미지 비율을 따른다
    assert info["gemini-omni-1.2-flash-i2v"]["aspect_ratios"] == ["16:9", "9:16"]
    assert info["hailuo-2.3"]["price_per_clip_usd"] == {"768p": {"6": 0.28, "10": 0.56}}
    assert info["hailuo-2.3"]["durations_kind"] == "choices"
    assert info["veo-3.1-fast"]["durations_sec"] == [4, 6, 8]
    assert info["kling-3.0"]["durations_sec"] == [3, 10] and info["kling-3.0"]["durations_kind"] == "range"
    assert info["wan-2.5"]["price_per_sec_usd"] == {"480p": 0.05, "720p": 0.10}    # 1080p 는 상한 밖이라 숨김
    assert info["gemini-omni-1.2-flash"]["resolutions"] == ["360p", "720p"]
    assert all(m["available"] for m in info.values())


async def test_generate_routes_payload_to_provider(make_app):
    from capped_video_mcp.providers.mock import MockProvider

    fal, rep = MockProvider(), MockProvider()
    app = make_app(providers={"replicate": rep, "fal": fal})
    await app.generate(P, model="veo-3.1-lite", duration_sec=6)
    await app.generate(P, model="hailuo-2.3")
    assert fal.payloads == [
        {"prompt": P, "duration": "6s", "resolution": "720p", "aspect_ratio": "16:9", "generate_audio": True},
        {"prompt": P, "duration": "6"},
    ]
    assert rep.payloads == []
    jobs = sorted(app.ledger.jobs().values(), key=lambda j: j["model"])
    assert [(j["model"], j["estimated_usd"]) for j in jobs] == [("hailuo-2.3", 0.28), ("veo-3.1-lite", 0.3)]
