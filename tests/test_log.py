import logging

from capped_video_mcp.log import RedactFilter, redact

from .conftest import FAKE_FAL, FAKE_REPLICATE


def test_redact_secrets_and_auth_headers():
    text = f"token={FAKE_REPLICATE} Authorization: Bearer abcdefgh12345678 Key {FAKE_FAL}"
    out = redact(text, [FAKE_REPLICATE, FAKE_FAL])
    assert FAKE_REPLICATE not in out and FAKE_FAL not in out
    assert "abcdefgh12345678" not in out


def test_filter_applies_to_formatted_args(caplog):
    logger = logging.getLogger("capped-video-test")
    f = RedactFilter([FAKE_REPLICATE])
    for h in logging.getLogger().handlers:
        h.addFilter(f)
    caplog.handler.addFilter(f)
    try:
        with caplog.at_level(logging.INFO):
            logger.info("response: %s", f"bad token {FAKE_REPLICATE}")
        assert FAKE_REPLICATE not in caplog.text
        assert "***" in caplog.text
    finally:
        caplog.handler.removeFilter(f)
        for h in logging.getLogger().handlers:
            h.removeFilter(f)


def test_traceback_is_redacted(caplog):
    logger = logging.getLogger("capped-video-test-exc")
    f = RedactFilter([FAKE_REPLICATE])
    caplog.handler.addFilter(f)
    try:
        try:
            raise RuntimeError(f"failed with {FAKE_REPLICATE}")
        except RuntimeError:
            logger.exception("boom")
        assert "RuntimeError" in caplog.text      # traceback 은 남는다
        assert FAKE_REPLICATE not in caplog.text  # 키는 가려진다
    finally:
        caplog.handler.removeFilter(f)
