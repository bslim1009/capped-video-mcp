"""설정 로드 순서: 패키지 기본값 ← 데이터 폴더 config.toml ← CAPPED_VIDEO_* 환경 변수."""

from pathlib import Path

import pytest

from capped_video_mcp import config
from capped_video_mcp.config import ENV_OVERRIDES, default_home, load_secrets, load_settings
from capped_video_mcp.service import build_app

SOURCE_ROOT = Path(__file__).resolve().parents[1]
EDITABLE = Path(config.__file__).resolve().parents[2] == SOURCE_ROOT


def test_packaged_default_loads_without_user_file(tmp_path):
    s = load_settings(tmp_path / "config.toml", environ={})
    assert s.defaults.model == "seedance-2.5"
    assert len(s.models) == 8


def test_user_file_overrides_only_given_keys(tmp_path):
    (tmp_path / "config.toml").write_text('[limits]\ndaily_max_usd = 2.5\n', encoding="utf-8")
    s = load_settings(tmp_path / "config.toml", environ={})
    assert s.limits.daily_max_usd == 2.5
    assert s.limits.monthly_max_usd == 30.0       # 쓰지 않은 키는 기본값
    assert s.defaults.model == "seedance-2.5"
    assert len(s.models) == 8


def test_user_models_list_replaces_whole_list(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[defaults]\nmodel = "only"\n\n'
        '[[models]]\nalias = "only"\nprovider = "fal"\nmodel_ref = "a/b"\n'
        'min_duration_sec = 4\nmax_duration_sec = 10\nprice_per_sec = { "480p" = 0.1 }\n',
        encoding="utf-8",
    )
    s = load_settings(tmp_path / "config.toml", environ={})
    assert [m.alias for m in s.models] == ["only"]


def test_top_level_aspect_ratios_in_user_file(tmp_path):
    (tmp_path / "config.toml").write_text('aspect_ratios = ["16:9", "9:16"]\n', encoding="utf-8")
    s = load_settings(tmp_path / "config.toml", environ={})
    assert s.limits.aspect_ratios == ["16:9", "9:16"]


def test_env_overrides_all_types(tmp_path):
    env = {
        "CAPPED_VIDEO_DEFAULT_MODEL": "kling-3.0",
        "CAPPED_VIDEO_DEFAULT_RESOLUTION": "720p",
        "CAPPED_VIDEO_DEFAULT_DURATION_SEC": "8",
        "CAPPED_VIDEO_DEFAULT_ASPECT_RATIO": "9:16",
        "CAPPED_VIDEO_MAX_RESOLUTION": "720p",
        "CAPPED_VIDEO_MAX_DURATION_SEC": "8",
        "CAPPED_VIDEO_DAILY_MAX_COUNT": "3",
        "CAPPED_VIDEO_DAILY_MAX_USD": "1.5",
        "CAPPED_VIDEO_MONTHLY_MAX_USD": "12",
        "CAPPED_VIDEO_MAX_PENDING_JOBS": "1",
        "CAPPED_VIDEO_DUPLICATE_WINDOW_MINUTES": "0",
        "CAPPED_VIDEO_CONFIRM_ABOVE_USD": "0.5",
        "CAPPED_VIDEO_CONFIRM_FALLBACK": "deny",
        "CAPPED_VIDEO_FAILOVER": "off",
        "CAPPED_VIDEO_FFMPEG": str(tmp_path / "ffmpeg.exe"),
        "CAPPED_VIDEO_OUTPUT_DIR": str(tmp_path / "videos"),
    }
    assert set(env) == set(ENV_OVERRIDES)  # 새 변수를 추가하면 이 테스트도 함께 고친다
    s = load_settings(None, environ=env)
    assert (s.defaults.model, s.defaults.resolution, s.defaults.duration_sec, s.defaults.aspect_ratio) == (
        "kling-3.0", "720p", 8, "9:16")
    lim = s.limits
    assert (lim.max_resolution, lim.max_duration_sec, lim.daily_max_count) == ("720p", 8, 3)
    assert (lim.daily_max_usd, lim.monthly_max_usd, lim.max_pending_jobs) == (1.5, 12.0, 1)
    assert lim.duplicate_window_minutes == 0
    assert (lim.confirm_above_usd, lim.confirm_fallback, lim.failover) == (0.5, "deny", False)
    assert Path(s.preview.ffmpeg) == tmp_path / "ffmpeg.exe"
    assert s.download.dir == str(tmp_path / "videos")


def test_env_beats_user_file(tmp_path):
    (tmp_path / "config.toml").write_text('[limits]\ndaily_max_usd = 2.5\n', encoding="utf-8")
    s = load_settings(tmp_path / "config.toml", environ={"CAPPED_VIDEO_DAILY_MAX_USD": "0.5"})
    assert s.limits.daily_max_usd == 0.5


def test_empty_env_value_is_ignored():
    # .mcp.json 의 "${VAR:-}" 처럼 빈 값으로 넘어오는 경우
    s = load_settings(None, environ={"CAPPED_VIDEO_DEFAULT_MODEL": "", "CAPPED_VIDEO_DAILY_MAX_USD": "  "})
    assert s.defaults.model == "seedance-2.5"
    assert s.limits.daily_max_usd == 5.0


def test_output_dir_expands_home():
    s = load_settings(None, environ={"CAPPED_VIDEO_OUTPUT_DIR": "~/videos"})
    assert Path(s.download.dir) == Path.home() / "videos"


@pytest.mark.parametrize(("name", "value", "message"), [
    ("CAPPED_VIDEO_DAILY_MAX_USD", "abc", "CAPPED_VIDEO_DAILY_MAX_USD"),
    ("CAPPED_VIDEO_DAILY_MAX_USD", "inf", "유한한"),
    ("CAPPED_VIDEO_DAILY_MAX_COUNT", "2.5", "정수"),
])
def test_env_bad_number_fails_with_variable_name(name, value, message):
    with pytest.raises(ValueError, match=message):
        load_settings(None, environ={name: value})


@pytest.mark.parametrize(("name", "value"), [
    ("CAPPED_VIDEO_DEFAULT_MODEL", "nope"),       # 허용 목록에 없는 모델
    ("CAPPED_VIDEO_DAILY_MAX_USD", "0"),          # gt=0
    ("CAPPED_VIDEO_MAX_RESOLUTION", "999p"),
    ("CAPPED_VIDEO_DEFAULT_RESOLUTION", "1080"),  # p 빠진 오타는 조용히 대체하지 않는다
])
def test_env_invalid_value_names_the_variable(name, value):
    with pytest.raises(ValueError, match=name):
        load_settings(None, environ={name: value})


def test_default_home_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPPED_VIDEO_HOME", str(tmp_path))
    assert default_home() == tmp_path.resolve()


def test_default_home_ignores_cwd_config(tmp_path, monkeypatch):
    # 클라이언트가 띄운 폴더에 다른 도구의 config.toml 이 있어도 쓰지 않는다.
    monkeypatch.delenv("CAPPED_VIDEO_HOME", raising=False)
    (tmp_path / "config.toml").write_text("[other]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert default_home() != tmp_path.resolve()


@pytest.mark.skipif(not EDITABLE, reason="설치된 wheel 로 테스트할 때는 해당 없음")
def test_default_home_source_checkout_is_project_root(monkeypatch):
    monkeypatch.delenv("CAPPED_VIDEO_HOME", raising=False)
    assert default_home() == SOURCE_ROOT


def test_default_home_installed_package_uses_user_dir(tmp_path, monkeypatch):
    # site-packages 에 설치된 상황을 흉내 낸다.
    monkeypatch.delenv("CAPPED_VIDEO_HOME", raising=False)
    fake = tmp_path / "site-packages" / "capped_video_mcp" / "config.py"
    monkeypatch.setattr(config, "__file__", str(fake))
    assert default_home() == Path.home() / ".capped-video-mcp"


def test_default_home_rejects_relative(monkeypatch):
    monkeypatch.setenv("CAPPED_VIDEO_HOME", "relative/dir")
    with pytest.raises(ValueError, match="절대 경로"):
        default_home()


def test_download_dir_in_user_file_expands_home_and_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("VG_TEST_DIR", str(tmp_path / "vids"))
    (tmp_path / "config.toml").write_text('[download]\ndir = "~/vg"\n', encoding="utf-8")
    assert Path(load_settings(tmp_path / "config.toml", environ={}).download.dir) == Path.home() / "vg"
    s = load_settings(None, environ={"CAPPED_VIDEO_OUTPUT_DIR": "$VG_TEST_DIR"})
    assert Path(s.download.dir) == tmp_path / "vids"


def test_bad_toml_names_the_file(tmp_path):
    # TOML 기본 문자열에서 \V 는 잘못된 이스케이프다. 오류에 파일 경로가 나와야 한다.
    (tmp_path / "config.toml").write_text('[download]\ndir = "D:\\Videos"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="config.toml"):
        load_settings(tmp_path / "config.toml", environ={})


def test_error_note_names_user_file(tmp_path):
    (tmp_path / "config.toml").write_text('[defaults]\nmodel = "nope"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="사용자 설정 파일"):
        load_settings(tmp_path / "config.toml", environ={"CAPPED_VIDEO_DAILY_MAX_USD": "3"})


def _clean_env(monkeypatch):
    for name in (*config.SECRET_ENV, "CAPPED_VIDEO_PROVIDER", *ENV_OVERRIDES):
        monkeypatch.delenv(name, raising=False)


def test_blank_env_does_not_hide_dotenv(tmp_path, monkeypatch):
    # .mcp.json 의 "${FAL_KEY:-}" 가 빈 값으로 넘어와도 .env 의 값을 써야 한다.
    _clean_env(monkeypatch)
    (tmp_path / ".env").write_text("FAL_KEY=fal-from-dotenv\nCAPPED_VIDEO_DAILY_MAX_USD=1.5\n", encoding="utf-8")
    monkeypatch.setenv("FAL_KEY", "")
    monkeypatch.setenv("CAPPED_VIDEO_DAILY_MAX_USD", " ")
    app = build_app(tmp_path)
    assert app.secrets.fal_key == "fal-from-dotenv"
    assert "fal" in app.providers
    assert app.settings.limits.daily_max_usd == 1.5   # .env 의 CAPPED_VIDEO_* 도 설정에 반영된다


def test_registered_env_beats_dotenv(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    (tmp_path / ".env").write_text("FAL_KEY=fal-from-dotenv\nCAPPED_VIDEO_DAILY_MAX_USD=1.5\n", encoding="utf-8")
    monkeypatch.setenv("FAL_KEY", "fal-registered")
    monkeypatch.setenv("CAPPED_VIDEO_DAILY_MAX_USD", "0.7")
    secrets = load_secrets(tmp_path)
    assert secrets.fal_key == "fal-registered"
    assert load_settings(None).limits.daily_max_usd == 0.7


@pytest.mark.parametrize("value", ["mok", "real"])
def test_provider_typo_fails_instead_of_real_mode(tmp_path, monkeypatch, value):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CAPPED_VIDEO_PROVIDER", value)
    with pytest.raises(ValueError, match="CAPPED_VIDEO_PROVIDER"):
        build_app(tmp_path)


def test_provider_mock_with_spaces(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CAPPED_VIDEO_PROVIDER", " Mock ")
    app = build_app(tmp_path)
    assert app.output_dir == (tmp_path / "output" / "mock").resolve()


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("TRUE", True), ("yes", True), ("0", False), ("off", False)])
def test_env_bool_parsing(raw, expected):
    assert load_settings(None, environ={"CAPPED_VIDEO_FAILOVER": raw}).limits.failover is expected


def test_env_bool_invalid():
    with pytest.raises(ValueError, match="CAPPED_VIDEO_FAILOVER"):
        load_settings(None, environ={"CAPPED_VIDEO_FAILOVER": "maybe"})


def test_confirm_fallback_invalid():
    with pytest.raises(ValueError, match="CAPPED_VIDEO_CONFIRM_FALLBACK"):
        load_settings(None, environ={"CAPPED_VIDEO_CONFIRM_FALLBACK": "ask"})


def test_fallback_model_must_exist_and_differ(tmp_path):
    base = ('[[models]]\nalias = "a"\nprovider = "fal"\nmodel_ref = "x/y"\nmin_duration_sec = 4\n'
            'max_duration_sec = 10\nprice_per_sec = { "480p" = 0.1 }\n')
    (tmp_path / "config.toml").write_text('[defaults]\nmodel = "a"\n\n' + base + 'fallback_model = "nope"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="fallback_model"):
        load_settings(tmp_path / "config.toml", environ={})
    (tmp_path / "config.toml").write_text('[defaults]\nmodel = "a"\n\n' + base + 'fallback_model = "a"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="자기 자신"):
        load_settings(tmp_path / "config.toml", environ={})


def test_default_config_has_seedance_failover_pair():
    s = load_settings(None, environ={})
    assert s.model("seedance-2.5").fallback_model == "seedance-2.5-fal"
    assert s.model("seedance-2.5-fal").fallback_model == "seedance-2.5"
    assert s.preview.frames == 6 and s.limits.confirm_above_usd == 0
