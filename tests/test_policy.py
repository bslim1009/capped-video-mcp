import pytest
from pydantic import ValidationError

from capped_video_mcp.config import Settings, load_settings
from capped_video_mcp.policy import PolicyError, build_request


def test_project_config_loads(settings):
    assert settings.defaults.model == "seedance-2.5"
    assert {m.alias for m in settings.models} == {
        "seedance-2.5", "seedance-2.5-fal", "kling-3.0", "wan-2.5", "veo-3.1-fast", "veo-3.1-lite",
        "hailuo-2.3", "gemini-omni-1.2-flash",
    }


def test_defaults(settings):
    req = build_request(settings, "a boat")
    assert req.model.alias == "seedance-2.5"
    assert req.model.provider == "replicate"
    assert (req.resolution, req.duration_sec, req.aspect_ratio) == ("480p", 5, "16:9")
    assert req.estimated_usd == pytest.approx(0.514)


def test_fal_720p_cost(settings):
    req = build_request(settings, "a boat", model="seedance-2.5-fal", resolution="720p", duration_sec=4)
    assert req.estimated_usd == pytest.approx(0.473 * 4)


@pytest.mark.parametrize(
    "kwargs, msg",
    [
        ({"model": "veo-3.1"}, "허용되지 않은 모델"),
        ({"resolution": "1080p"}, "1080p"),
        ({"resolution": "4k"}, "4k"),
        ({"duration_sec": 3}, "4~10초"),
        ({"duration_sec": 11}, "4~10초"),
        ({"duration_sec": 30}, "4~10초"),
        ({"duration_sec": -1}, "4~10초"),
        ({"duration_sec": "auto"}, "자동 길이"),
        ({"duration_sec": True}, "자동 길이"),
        ({"aspect_ratio": "2:1"}, "화면 비율"),
    ],
)
def test_rejects(settings, kwargs, msg):
    with pytest.raises(PolicyError, match=msg):
        build_request(settings, "a boat", **kwargs)


def test_rejects_empty_and_long_prompt(settings):
    with pytest.raises(PolicyError, match="비어"):
        build_request(settings, "   ")
    with pytest.raises(PolicyError, match="너무 깁니다"):
        build_request(settings, "x" * (settings.limits.max_prompt_chars + 1))


def test_invalid_config_fails(home):
    text = (home / "config.toml").read_text(encoding="utf-8")
    (home / "config.toml").write_text(text.replace('model = "seedance-2.5"', 'model = "nope"', 1), encoding="utf-8")
    with pytest.raises(ValidationError, match="허용 목록에 없다"):
        load_settings(home / "config.toml", environ={})


def test_unknown_config_key_fails(settings):
    data = settings.model_dump()
    data["limits"]["daily_max_cnt"] = 3
    with pytest.raises(ValidationError):
        Settings.model_validate(data)
