"""생성 입력용 로컬 미디어 파일(이미지·오디오) 읽기와 검증.

모든 오류는 PolicyError (과금 전). 경로는 에이전트가 준 값이므로
- 환경 변수(%FAL_KEY%, $FAL_KEY)는 풀지 않는다 (오류 메시지로 키가 새지 않게).
- UNC·장치 경로는 거부한다 (열기만 해도 외부 호스트에 연결하고 Windows 는 NTLM 인증을 보낸다).
- 오류 메시지에는 받은 값을 그대로 보여 준다.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from .policy import PolicyError

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
AUDIO_EXTS = frozenset({".mp3", ".wav"})
# 참조 오디오 파일 하나의 최소 길이 (fal: 파일당 1.8~30.2초). 합계 상한은 policy.AUDIO_TOTAL_MAX_SEC.
AUDIO_MIN_SEC = 1.8


@dataclass(frozen=True)
class MediaFile:
    path: str                                # 절대 경로. 기록·표시용
    mime: str
    sha256: str
    data: bytes = field(repr=False)          # 로그·기록에 남기지 않는다
    duration_sec: float | None = None        # 오디오 길이

    @property
    def name(self) -> str:
        return Path(self.path).name


# ── 형식 판별 (확장자가 아니라 파일 앞부분으로) ──

def _sniff_image(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _sniff_audio(head: bytes) -> str | None:
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    return None


# ── 오디오 길이 ──

def _wav_duration(data: bytes) -> float | None:
    """RIFF 청크를 따라 fmt 의 byte_rate 와 data 크기로 길이를 구한다."""
    pos, byte_rate, data_size = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if cid == b"fmt " and size >= 16 and body + 16 <= len(data):
            _, _, sample_rate, rate, block_align = struct.unpack_from("<HHIIH", data, body)
            # 헤더의 byte_rate 를 그대로 믿지 않는다 (sample_rate × block_align 과 같아야 한다).
            if not rate or rate != sample_rate * block_align:
                return None
            byte_rate = rate
        elif cid == b"data":
            data_size = min(size, len(data) - body)
        pos = body + size + (size & 1)
    if not byte_rate or data_size is None:
        return None
    return data_size / byte_rate


_MP3_BITRATES = {
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),   # MPEG-1 Layer III
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),       # MPEG-2/2.5 Layer III
}
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_duration(data: bytes) -> float | None:
    """MPEG Layer III 프레임 헤더를 차례로 따라가며 길이를 더한다 (VBR 도 맞다)."""
    pos = 0
    if data.startswith(b"ID3") and len(data) >= 10:
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        pos = 10 + size + (10 if data[5] & 0x10 else 0)
    total, frames = 0.0, 0
    while pos + 4 <= len(data):
        b1, b2 = data[pos + 1], data[pos + 2]
        if data[pos] != 0xFF or b1 & 0xE0 != 0xE0:
            break
        version, layer = (b1 >> 3) & 0b11, (b1 >> 1) & 0b11
        bi, si, pad = b2 >> 4, (b2 >> 2) & 0b11, (b2 >> 1) & 1
        if version == 1 or layer != 0b01 or bi in (0, 15) or si == 3:   # Layer III 만, 예약값 제외
            break
        rate = _MP3_RATES[version][si]
        bitrate = _MP3_BITRATES[1 if version == 3 else 2][bi] * 1000
        per = 1152 if version == 3 else 576
        length = per // 8 * bitrate // rate + pad
        if length < 4:
            break
        total += per / rate
        frames += 1
        pos += length
    if not frames:
        return None
    # 프레임 사이에 깨진 바이트가 있으면 길이를 짧게 잡게 된다 → 끝의 태그 말고 남은 게 있으면 손상으로 본다.
    rest = data[pos:]
    if len(rest) >= 4 and not rest.startswith((b"TAG", b"APETAGEX", b"LYRICS")):
        return None
    return total


# ── 읽기 ──

def _is_network_path(raw: str) -> bool:
    s = raw.replace("/", "\\")
    return s.startswith("\\\\") or PureWindowsPath(s).drive.startswith("\\\\")


def _local_path(raw_path: str, what: str) -> tuple[str, Path]:
    raw = (raw_path or "").strip()
    if not raw:
        raise PolicyError(f"{what} 경로가 비어 있습니다.")
    if _is_network_path(raw):
        raise PolicyError(f"네트워크 경로(UNC)는 쓸 수 없습니다. 로컬 경로를 주세요: {raw}")
    path = Path(os.path.expanduser(raw))
    if _is_network_path(str(path)):     # ~ 가 네트워크 홈 폴더로 풀린 경우
        raise PolicyError(f"네트워크 경로(UNC)는 쓸 수 없습니다. 로컬 경로를 주세요: {raw}")
    # 서버의 작업 폴더는 클라이언트마다 달라 상대 경로는 엉뚱한 파일을 가리킬 수 있다.
    if not path.is_absolute():
        raise PolicyError(f"{what} 경로는 절대 경로여야 합니다: {raw}")
    return raw, path


def _read(raw: str, path: Path, max_bytes: int, what: str) -> tuple[bytes, str]:
    try:
        if not path.is_file():
            raise PolicyError(f"{what} 파일이 없습니다: {raw}")
        size = path.stat().st_size
        if size > max_bytes:
            raise PolicyError(_too_big(what, raw, size, max_bytes))
        data = path.read_bytes()
        resolved = str(path.resolve())
    except OSError as e:
        raise PolicyError(f"{what} 파일을 읽지 못했습니다: {raw} ({e.strerror or type(e).__name__})") from e
    if not data:
        raise PolicyError(f"{what} 파일이 비어 있습니다: {raw}")
    if len(data) > max_bytes:   # 크기를 본 뒤 파일이 바뀐 경우
        raise PolicyError(_too_big(what, raw, len(data), max_bytes))
    return data, resolved


def _too_big(what: str, raw: str, size: int, max_bytes: int) -> str:
    return f"{what} 파일이 너무 큽니다 ({size / 1_048_576:.1f}MB). 최대 {max_bytes / 1_048_576:.1f}MB 입니다: {raw}"


def load_image(raw_path: str, max_bytes: int) -> MediaFile:
    raw, path = _local_path(raw_path, "이미지")
    data, resolved = _read(raw, path, max_bytes, "이미지")
    mime = _sniff_image(data[:16])
    if mime is None:
        raise PolicyError(f"지원하지 않는 이미지 형식입니다: {raw}. PNG, JPEG, WebP 만 쓸 수 있습니다.")
    return MediaFile(path=resolved, mime=mime, sha256=hashlib.sha256(data).hexdigest(), data=data)


def load_audio(raw_path: str, max_bytes: int) -> MediaFile:
    raw, path = _local_path(raw_path, "오디오")
    data, resolved = _read(raw, path, max_bytes, "오디오")
    mime = _sniff_audio(data[:16])
    if mime is None:
        raise PolicyError(f"지원하지 않는 오디오 형식입니다: {raw}. MP3, WAV 만 쓸 수 있습니다.")
    try:
        duration = _wav_duration(data) if mime == "audio/wav" else _mp3_duration(data)
    except (struct.error, IndexError, ZeroDivisionError):
        duration = None
    if duration is None:
        raise PolicyError(f"오디오 길이를 읽지 못했습니다 (손상됐거나 지원하지 않는 인코딩): {raw}")
    if duration < AUDIO_MIN_SEC:
        raise PolicyError(f"오디오가 너무 짧습니다 ({duration:.1f}초). 파일마다 {AUDIO_MIN_SEC}초 이상이어야 합니다: {raw}")
    return MediaFile(path=resolved, mime=mime, sha256=hashlib.sha256(data).hexdigest(), data=data,
                     duration_sec=round(duration, 3))


def _natural_key(name: str) -> list[int | str]:
    """사람이 기대하는 순서: 2.png 가 10.png 보다 앞."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def list_reference_dir(raw_dir: str) -> tuple[list[str], list[str]]:
    """폴더 안의 이미지·오디오 파일 경로 (하위 폴더 제외, 숨김 파일 제외, 파일 이름순. 숫자는 크기순)."""
    raw, path = _local_path(raw_dir, "참조 폴더")
    try:
        if not path.is_dir():
            raise PolicyError(f"폴더가 없습니다: {raw}")
        entries = sorted((p for p in path.iterdir() if p.is_file() and not p.name.startswith(".")),
                         key=lambda p: _natural_key(p.name))
    except OSError as e:
        raise PolicyError(f"폴더를 읽지 못했습니다: {raw} ({e.strerror or type(e).__name__})") from e
    images = [str(p) for p in entries if p.suffix.lower() in IMAGE_EXTS]
    audios = [str(p) for p in entries if p.suffix.lower() in AUDIO_EXTS]
    if not images and not audios:
        raise PolicyError(f"폴더에 쓸 수 있는 이미지(PNG·JPEG·WebP)나 오디오(MP3·WAV)가 없습니다: {raw}")
    return images, audios
