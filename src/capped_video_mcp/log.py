"""로그 설정. 비밀값과 인증 헤더를 가린다.

stdio MCP 서버는 stdout 이 통신 채널이므로 print() 를 쓰지 않는다.
로그는 stderr(SDK 가 설정)와 logs/ 파일에만 쓴다.
"""

from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .ledger import KST, _now

_AUTH_RE = re.compile(r"(?i)\b(Bearer|Key)\s+[A-Za-z0-9_\-.:]{8,}")
MASK = "***"


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, MASK)
    return _AUTH_RE.sub(lambda m: f"{m.group(1)} {MASK}", text)


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # 포맷 실패해도 로그는 남긴다
            msg = str(record.msg)
        # traceback 에도 키가 섞일 수 있으므로 미리 문자열로 만들어 가린 뒤 메시지에 붙인다.
        if record.exc_info:
            msg = msg + "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        if record.stack_info:
            msg = msg + "\n" + record.stack_info
            record.stack_info = None
        record.msg = redact(msg, self._secrets)
        record.args = None
        return True


def setup_logging(home: Path, secrets: list[str]) -> None:
    """루트 로거에 파일 핸들러를 붙이고, 모든 핸들러에 가리기 필터를 단다.

    MCPServer 를 만든 뒤에 부른다 (SDK 가 stderr 핸들러를 먼저 설정하므로).
    """
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    fh = RotatingFileHandler(logs / "capped-video-mcp.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    f = RedactFilter(secrets)
    for h in root.handlers:
        h.addFilter(f)
    # httpx 는 INFO 에서 요청 URL 을 찍는다. 서명된 URL 이 남지 않게 올린다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Audit:
    """logs/audit.jsonl 에 요청 하나당 한 줄을 남긴다."""

    PROMPT_CHARS = 50

    def __init__(self, path: Path, secrets: list[str]):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._secrets = [s for s in secrets if s]

    def write(self, event: str, **fields: Any) -> None:
        """감사 기록. 실패해도 예외를 던지지 않는다 (생성·수거 흐름을 깨지 않게)."""
        if "prompt" in fields and isinstance(fields["prompt"], str):
            p = fields["prompt"]
            fields["prompt"] = p[: self.PROMPT_CHARS] + ("…" if len(p) > self.PROMPT_CHARS else "")
        record = {"time": _now().astimezone(KST).isoformat(), "event": event, **fields}
        line = redact(json.dumps(record, ensure_ascii=False, default=str), self._secrets)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            logging.getLogger(__name__).error("감사 로그 기록 실패: %s", line)
