"""참조→영상: 참조 이미지 여러 장 + 참조 오디오.

요청 본문 기대값은 Replicate bytedance/seedance-2.5 api/schema 와
fal bytedance/seedance-2.5/reference-to-video llms.txt (2026-11-22) 에서 옮겨 적었다.
테스트 오디오는 표준 라이브러리 wave 와 MPEG 프레임 규격으로 만든다 (서버 파서와 독립).
"""

import hashlib
import io
import json
import wave
from pathlib import Path

import pytest
from mcp import Client
from pydantic import ValidationError

from capped_video_mcp.config import ModelConfig, Settings
from capped_video_mcp.media import list_reference_dir, load_audio, load_image
from capped_video_mcp.policy import PolicyError, UploadedInputs, build_payload, build_request, rewrite_ref_tags
from capped_video_mcp.providers.base import ProviderError
from capped_video_mcp.providers.mock import MockProvider
from capped_video_mcp.server import create_server
from capped_video_mcp.service import AppError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
P = "[Image1] 의 공룡과 [Image2] 의 공룡이 싸운다. [Audio1] 의 포효에 맞춰"


def wav_bytes(seconds: float, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


# MPEG-1 Layer III, 128kbps, 44.1kHz, 패딩 없음: 프레임 = 144*128000/44100 = 417바이트, 1152 샘플
MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
# MPEG-2 Layer III, 80kbps(인덱스 9), 22.05kHz: 프레임 = 72*80000/22050 = 261바이트, 576 샘플
MP3_FRAME_V2 = b"\xff\xf3\x90\x00" + b"\x00" * 257
ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10      # 크기 10 (syncsafe)


def mp3_bytes(frames: int, frame: bytes = MP3_FRAME, id3: bool = True) -> bytes:
    return (ID3 if id3 else b"") + frame * frames


@pytest.fixture
def sample(tmp_path):
    """C:\\Project\\Sample 같은 폴더: 공룡 이미지 2장 + 포효 음성 1개 + 무관한 파일."""
    d = tmp_path / "Sample"
    d.mkdir()
    (d / "b_dino.jpg").write_bytes(JPEG)
    (d / "a_dino.png").write_bytes(PNG)
    (d / "roar.wav").write_bytes(wav_bytes(3.0))
    (d / "notes.txt").write_text("memo", encoding="utf-8")
    (d / ".hidden.png").write_bytes(PNG)
    (d / "sub").mkdir()
    (d / "sub" / "c.png").write_bytes(PNG)
    return d


@pytest.fixture
def refs(sample):
    imgs = (load_image(str(sample / "a_dino.png"), 10**7), load_image(str(sample / "b_dino.jpg"), 10**7))
    auds = (load_audio(str(sample / "roar.wav"), 10**7),)
    return imgs, auds


# ── 오디오 읽기 ──

def test_wav_duration(tmp_path):
    path = tmp_path / "a.wav"
    path.write_bytes(wav_bytes(3.0))
    a = load_audio(str(path), 10**7)
    assert a.mime == "audio/wav" and a.duration_sec == pytest.approx(3.0)
    assert a.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("data, seconds", [
    (mp3_bytes(100), 100 * 1152 / 44100),
    (mp3_bytes(100, id3=False), 100 * 1152 / 44100),
    (mp3_bytes(200, MP3_FRAME_V2), 200 * 576 / 22050),
    (mp3_bytes(100) + b"TAG" + b"\x00" * 125, 100 * 1152 / 44100),     # 끝의 ID3v1 태그
])
def test_mp3_duration(tmp_path, data, seconds):
    path = tmp_path / "a.mp3"
    path.write_bytes(data)
    a = load_audio(str(path), 10**7)
    assert a.mime == "audio/mpeg" and a.duration_sec == pytest.approx(seconds, abs=1e-3)


def test_audio_rejections(tmp_path):
    cases = {
        "short.wav": (wav_bytes(1.0), "너무 짧습니다"),
        "short.mp3": (mp3_bytes(10), "너무 짧습니다"),                     # 0.26초
        "a.ogg": (b"OggS" + b"\x00" * 100, "MP3, WAV"),
        "broken.mp3": (ID3 + b"\x00" * 500, "길이를 읽지 못했습니다"),
        "nofmt.wav": (b"RIFF\x00\x00\x00\x00WAVEdata\x04\x00\x00\x00abcd", "길이를 읽지 못했습니다"),
    }
    for name, (data, msg) in cases.items():
        (tmp_path / name).write_bytes(data)
        with pytest.raises(PolicyError, match=msg):
            load_audio(str(tmp_path / name), 10**7)
    with pytest.raises(PolicyError, match="너무 큽니다"):
        load_audio(str(tmp_path / "short.wav"), 100)


def test_list_reference_dir(sample, tmp_path):
    images, audios = list_reference_dir(str(sample))
    assert [Path(p).name for p in images] == ["a_dino.png", "b_dino.jpg"]   # 이름순, 숨김·하위 폴더 제외
    assert len(audios) == 1 and audios[0].endswith("roar.wav")
    (tmp_path / "empty").mkdir()
    with pytest.raises(PolicyError, match="쓸 수 있는 이미지"):
        list_reference_dir(str(tmp_path / "empty"))
    with pytest.raises(PolicyError, match="폴더가 없습니다"):
        list_reference_dir(str(tmp_path / "nope"))
    with pytest.raises(PolicyError, match="절대 경로"):
        list_reference_dir("Sample")
    with pytest.raises(PolicyError, match="네트워크 경로"):
        list_reference_dir(r"\\host\share")


# ── 요청 본문 ──

UP = UploadedInputs(ref_images=("https://u/1.png", "https://u/2.jpg"), ref_audios=("https://u/a.wav",))


def test_replicate_reference_payload(settings, refs):
    imgs, auds = refs
    req = build_request(settings, P, model="seedance-2.5-ref", ref_images=imgs, ref_audios=auds)
    assert build_payload(req, UP) == {
        "prompt": P, "duration": 5, "resolution": "480p", "aspect_ratio": "16:9", "generate_audio": True,
        "reference_images": ["https://u/1.png", "https://u/2.jpg"], "reference_audios": ["https://u/a.wav"],
    }
    assert req.estimated_usd == pytest.approx(0.514)
    assert req.reference_labels() == {"[Image1]": imgs[0].path, "[Image2]": imgs[1].path, "[Audio1]": auds[0].path}


def test_fal_reference_payload_rewrites_tags(settings, refs):
    imgs, auds = refs
    req = build_request(settings, P, model="seedance-2.5-fal-ref", ref_images=imgs, ref_audios=auds, aspect_ratio="9:16")
    assert build_payload(req, UP) == {
        "prompt": "@Image1 의 공룡과 @Image2 의 공룡이 싸운다. @Audio1 의 포효에 맞춰",
        "duration": "5", "resolution": "480p", "aspect_ratio": "9:16", "generate_audio": True,
        "image_urls": ["https://u/1.png", "https://u/2.jpg"], "audio_urls": ["https://u/a.wav"], "task": "reference",
    }
    assert req.estimated_usd == pytest.approx(1.1025)


def test_tag_rewrite_both_ways():
    assert rewrite_ref_tags("@image1 and [AUDIO2], email@Image1x", "bracket") == "[Image1] and [Audio2], email@Image1x"
    assert rewrite_ref_tags("[Image10] [Video1]", "at") == "@Image10 @Video1"


def test_reference_without_audio_omits_audio_field(settings, refs):
    imgs, _ = refs
    req = build_request(settings, "[Image1] 이 달린다", model="seedance-2.5-ref", ref_images=imgs[:1])
    payload = build_payload(req, UploadedInputs(ref_images=("https://u/1.png",)))
    assert payload["reference_images"] == ["https://u/1.png"] and "reference_audios" not in payload


def test_default_reference_model(settings, refs):
    imgs, auds = refs
    assert build_request(settings, P, ref_images=imgs, ref_audios=auds).model.alias == "seedance-2.5-ref"


def test_reference_rejections(settings, refs, tmp_path):
    imgs, auds = refs
    long = tmp_path / "long.wav"
    long.write_bytes(wav_bytes(16.0))
    long_audio = load_audio(str(long), 10**7)
    cases = [
        ({"model": "seedance-2.5-ref"}, "참조 이미지가 1장 이상"),
        ({"ref_audios": auds}, "참조 이미지가 1장 이상"),                             # 오디오만
        ({"ref_images": imgs, "image": imgs[0]}, "첫 프레임"),
        ({"ref_images": imgs[:1], "ref_audios": auds}, r"\[Image2\] 에 해당하는 참조 이미지"),
        ({"ref_images": imgs}, r"\[Audio1\] 에 해당하는 참조 오디오"),
        ({"ref_images": imgs, "ref_audios": (long_audio, long_audio)}, "합계가 32.0초"),
        ({"model": "kling-3.0", "ref_images": imgs}, "텍스트→영상 모델이라 이미지·참조 파일을 받지 않습니다"),
        ({"model": "kling-3.0-i2v", "ref_images": imgs}, "첫 프레임 이미지\\(image_path\\) 하나만"),
    ]
    for kwargs, msg in cases:
        with pytest.raises(PolicyError, match=msg):
            build_request(settings, P, **kwargs)
    with pytest.raises(PolicyError, match="참조 영상"):
        build_request(settings, "[Image1] 이 [Video1] 처럼 춤춘다", ref_images=imgs)


def test_reference_count_limit(settings, refs):
    imgs, _ = refs
    data = settings.model_dump()
    for m in data["models"]:
        if m["alias"] == "seedance-2.5-ref":
            m["input"]["max_ref_images"] = 1
    with pytest.raises(PolicyError, match="1장까지"):
        build_request(Settings.model_validate(data), "[Image1]", ref_images=imgs)


def test_fingerprint_depends_on_reference_order(settings, refs):
    imgs, auds = refs
    a = build_request(settings, P, ref_images=imgs, ref_audios=auds).fingerprint()
    b = build_request(settings, P, ref_images=imgs[::-1], ref_audios=auds).fingerprint()
    assert a != b    # [Image1] 이 바뀌면 다른 영상이다


# ── 설정 검증 ──

BASE = {"alias": "m", "provider": "fal", "model_ref": "a/b", "durations": [5], "price_per_sec": {"480p": 1}}


@pytest.mark.parametrize("inp, msg", [
    ({"image_field": "image_url", "ref_images_field": "image_urls"}, "함께 쓸 수 없다"),
    ({"ref_audios_field": "audio_urls"}, "ref_images_field 와 함께"),
    ({"ref_images_field": "urls", "ref_audios_field": "urls"}, "겹친다"),
    ({"ref_images_field": "prompt"}, "서버가 채우는"),
    ({"ref_images_field": "image_urls", "extra": {"image_urls": []}}, "서버가 채우는"),
])
def test_invalid_reference_config(inp, msg):
    with pytest.raises(ValidationError, match=msg):
        ModelConfig.model_validate({**BASE, "input": inp})


def test_reference_default_must_be_reference_model(settings):
    data = settings.model_dump()
    data["defaults"]["reference_model"] = "seedance-2.5-i2v"
    with pytest.raises(ValidationError, match="참조→영상 모델을 쓴다"):
        Settings.model_validate(data)


# ── 서비스 흐름 (mock 제공처) ──

async def test_generate_from_reference_dir(make_app, sample):
    """예시 요청: "C:\\Project\\Sample 에 있는 이미지와 음성 파일로 공룡이 싸우는 영상"."""
    mp = MockProvider()
    app = make_app(provider=mp)
    r = await app.generate(P, reference_dir=str(sample))
    assert r["model"] == "seedance-2.5-ref" and r["provider"] == "replicate"
    assert list(r["references"]) == ["[Image1]", "[Image2]", "[Audio1]"]
    assert r["references"]["[Image1]"].endswith("a_dino.png") and r["references"]["[Audio1]"].endswith("roar.wav")
    # 이미지 → 오디오 순서로 모두 올린다
    expected = [hashlib.sha256(x).hexdigest() for x in (PNG, JPEG, (sample / "roar.wav").read_bytes())]
    assert mp.uploads == expected
    (payload,) = mp.payloads
    assert len(payload["reference_images"]) == 2 and len(payload["reference_audios"]) == 1
    assert payload["reference_images"][0].endswith(expected[0][:12])
    job = app.ledger.get(r["job_id"])
    assert job["references"] == r["references"]
    assert (await app.status(r["job_id"], wait_seconds=0))["references"] == r["references"]
    events = [json.loads(x) for x in app.audit.path.read_text(encoding="utf-8").splitlines()]
    assert next(e for e in events if e["event"] == "inputs_uploaded")["count"] == 3


async def test_reference_paths_and_dir_are_exclusive(make_app, sample):
    app = make_app()
    with pytest.raises(PolicyError, match="함께 쓸 수 없습니다"):
        await app.generate(P, reference_dir=str(sample), reference_image_paths=[str(sample / "a_dino.png")])
    assert app.ledger.jobs() == {}


async def test_explicit_reference_paths_keep_given_order(make_app, sample):
    mp = MockProvider()
    app = make_app(provider=mp)
    r = await app.generate(P, reference_image_paths=[str(sample / "b_dino.jpg"), str(sample / "a_dino.png")],
                           reference_audio_paths=[str(sample / "roar.wav")])
    assert r["references"]["[Image1]"].endswith("b_dino.jpg")
    assert mp.uploads[0] == hashlib.sha256(JPEG).hexdigest()


async def test_same_references_are_duplicate(make_app, sample):
    mp = MockProvider()
    app = make_app(provider=mp)
    first = await app.generate(P, reference_dir=str(sample))
    again = await app.generate(P, reference_dir=str(sample))
    assert again["duplicate"] is True and again["job_id"] == first["job_id"] and len(mp.payloads) == 1


class UploadRejecting(MockProvider):
    async def upload_file(self, media):
        raise ProviderError("replicate: 요청 거부(HTTP 413).")


async def test_reference_upload_failure_fails_over_to_fal_syntax(make_app, sample):
    fal = MockProvider()
    app = make_app(providers={"replicate": UploadRejecting(), "fal": fal})

    async def yes(message):
        return True
    r = await app.generate(P, reference_dir=str(sample), confirm=yes)     # fal 이 더 비싸서 승인이 필요하다
    assert r["failover_from"] == "seedance-2.5-ref" and r["model"] == "seedance-2.5-fal-ref"
    (payload,) = fal.payloads
    assert payload["prompt"].startswith("@Image1 의 공룡과 @Image2") and payload["task"] == "reference"
    assert len(payload["image_urls"]) == 2 and len(payload["audio_urls"]) == 1


async def test_estimate_with_reference_dir(make_app, sample):
    app = make_app()
    r = app.estimate(reference_dir=str(sample))
    assert {o["model"] for o in r["options"]} == {"seedance-2.5-ref", "seedance-2.5-fal-ref"}
    assert all(o["input"] == "reference" for o in r["options"])
    assert r["cheapest"] == "seedance-2.5-ref"
    assert list(r["references"]) == ["[Image1]", "[Image2]", "[Audio1]"]


async def test_generate_from_reference_dir_via_protocol(make_app, sample):
    server = create_server(make_app())
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        props = tools["generate_video"].input_schema["properties"]
        assert {"reference_image_paths", "reference_audio_paths", "reference_dir"} <= set(props)
        assert "[Image1]" in tools["generate_video"].description
        est = await c.call_tool("estimate_video", {"reference_dir": str(sample)})
        assert not est.is_error and "[Audio1]" in est.structured_content["references"]
        r = await c.call_tool("generate_video", {"prompt": P, "reference_dir": str(sample)})
        assert not r.is_error and r.structured_content["model"] == "seedance-2.5-ref"
        bad = await c.call_tool("generate_video", {"prompt": "[Image1]", "reference_audio_paths": [str(sample / "roar.wav")]})
        assert bad.is_error and "참조 이미지가 1장 이상" in bad.content[0].text


async def test_bad_reference_file_rejected_before_reserving(make_app, sample):
    (sample / "zz_broken.mp3").write_bytes(ID3 + b"\x00" * 100)
    app = make_app()
    with pytest.raises(PolicyError, match="길이를 읽지 못했습니다"):
        await app.generate(P, reference_dir=str(sample))
    assert app.ledger.jobs() == {}


async def test_too_many_reference_files_rejected_before_reading(make_app, tmp_path):
    app = make_app()
    paths = [str(tmp_path / f"{i}.png") for i in range(31)]      # 파일이 없어도 개수로 먼저 거른다
    with pytest.raises(PolicyError, match="30장까지"):
        await app.generate(P, reference_image_paths=paths)


# ── 리뷰 반영 회귀 테스트 ──

def test_corrupt_wav_is_policy_error(tmp_path):
    cases = {
        # fmt 청크가 선언보다 짧게 잘림 (예전에는 struct.error 로 터졌다)
        "trunc.wav": b"RIFF\x00\x00\x00\x00WAVEfmt " + (16).to_bytes(4, "little") + b"\x01\x00",
        # byte_rate 가 sample_rate × block_align 과 다름
        "rate.wav": wav_bytes(3.0)[:28] + (1).to_bytes(4, "little") + wav_bytes(3.0)[32:],
    }
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
        with pytest.raises(PolicyError, match="길이를 읽지 못했습니다"):
            load_audio(str(tmp_path / name), 10**7)


def test_mp3_with_garbage_between_frames_is_rejected(tmp_path):
    """깨진 바이트에서 멈춰 길이를 짧게 잡으면 30초 검사를 피해 간다."""
    path = tmp_path / "a.mp3"
    path.write_bytes(mp3_bytes(80) + b"\x00\x01" + MP3_FRAME * 2000)
    with pytest.raises(PolicyError, match="길이를 읽지 못했습니다"):
        load_audio(str(path), 10**8)


def test_reference_dir_natural_order(tmp_path):
    d = tmp_path / "n"
    d.mkdir()
    for name in ("10.png", "2.png", "1.png"):
        (d / name).write_bytes(PNG)
    images, _ = list_reference_dir(str(d))
    assert [Path(p).name for p in images] == ["1.png", "2.png", "10.png"]


def test_tag_edge_cases():
    assert rewrite_ref_tags("[Image01] mail a@image1.com, roar like @Audio1.", "bracket") == \
        "[Image01] mail a@image1.com, roar like [Audio1]."
    assert rewrite_ref_tags("[Image١]", "at") == "[Image١]"           # 유니코드 숫자는 표기가 아니다


def test_fingerprint_ignores_tag_syntax(settings, refs):
    imgs, auds = refs
    a = build_request(settings, P, ref_images=imgs, ref_audios=auds).fingerprint()
    b = build_request(settings, rewrite_ref_tags(P, "at"), ref_images=imgs, ref_audios=auds).fingerprint()
    assert a == b       # 표기만 바꾼 재시도는 같은 요청 (이중 과금 방지)


async def test_estimate_without_reference_models(make_app, settings, sample):
    app = make_app()
    data = settings.model_dump()
    data["models"] = [m for m in data["models"] if not m["alias"].endswith("-ref")]
    data["defaults"]["reference_model"] = None
    app.settings = Settings.model_validate(data)
    with pytest.raises(AppError, match="참조→영상 모델이 없습니다"):
        app.estimate(reference_dir=str(sample))


async def test_payload_error_before_request_is_not_charged(make_app, sample, monkeypatch):
    from capped_video_mcp import ledger as L
    from capped_video_mcp import service

    def broken(req, uploaded=None):
        raise ValueError("bad payload")

    monkeypatch.setattr(service, "build_payload", broken)
    mp = MockProvider()
    app = make_app(provider=mp)
    with pytest.raises(AppError, match="과금되지 않았습니다"):
        await app.generate(P, reference_dir=str(sample))
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED and not mp.payloads


async def test_expired_reservation_stops_remaining_uploads(make_app, sample, monkeypatch):
    import asyncio

    from capped_video_mcp import ledger as L
    from capped_video_mcp import service

    monkeypatch.setattr(service, "RESERVED_HEARTBEAT_SEC", 0.01)
    holder = {}

    class Slow(MockProvider):
        async def upload_file(self, media):
            if not self.uploads:      # 첫 업로드 중에 다른 프로세스가 예약을 회수
                (job_id,) = holder["app"].ledger.jobs()
                holder["app"].ledger.transition(job_id, {L.RESERVED}, status=L.UNKNOWN)
            await asyncio.sleep(0.05)
            return await super().upload_file(media)

    mp = Slow()
    app = holder["app"] = make_app(provider=mp)
    with pytest.raises(AppError, match="예약이 만료돼"):
        await app.generate(P, reference_dir=str(sample))
    assert len(mp.uploads) < 3 and not mp.payloads
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED and app.usage()["today_usd"] == 0
