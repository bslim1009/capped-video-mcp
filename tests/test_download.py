import httpx
import pytest

from capped_video_mcp.download import DownloadError, download_video, host_allowed

JOB = "j_20261122_120000_abcdef"
HOSTS = ["replicate.delivery", "fal.media"]
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"x" * 100


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(request):
    return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=VIDEO)


async def test_success_and_no_auth_header(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return ok(request)

    path = await download_video(client(handler), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 10_000)
    assert path == tmp_path / f"{JOB}.mp4"
    assert path.read_bytes() == VIDEO
    assert "Authorization" not in seen[0].headers
    assert not list(tmp_path.glob("*.part"))


def test_host_allowed():
    assert host_allowed("https://replicate.delivery/x", HOSTS)
    assert host_allowed("https://v3.fal.media/x", HOSTS)  # 하위 도메인
    assert not host_allowed("http://replicate.delivery/x", HOSTS)  # http
    assert not host_allowed("https://replicate.delivery.evil.com/x", HOSTS)
    assert not host_allowed("https://evilfal.media/x", HOSTS)
    assert not host_allowed("file:///etc/passwd", HOSTS)


@pytest.mark.parametrize("url", ["https://evil.com/a.mp4", "http://replicate.delivery/a.mp4"])
async def test_rejects_disallowed(tmp_path, url):
    with pytest.raises(DownloadError, match="허용되지 않은"):
        await download_video(client(ok), url, tmp_path, JOB, HOSTS, 10_000)


async def test_redirect_to_other_host_rejected(tmp_path):
    def handler(request):
        if request.url.host == "replicate.delivery":
            return httpx.Response(302, headers={"Location": "https://evil.com/x.mp4"})
        return ok(request)

    with pytest.raises(DownloadError, match="허용되지 않은"):
        await download_video(client(handler), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 10_000)


async def test_redirect_within_allowed(tmp_path):
    def handler(request):
        if request.url.host == "replicate.delivery":
            return httpx.Response(302, headers={"Location": "https://v3.fal.media/x.mp4"})
        return ok(request)

    path = await download_video(client(handler), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 10_000)
    assert path.exists()


async def test_too_big_by_header(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "video/mp4", "Content-Length": "999999"}, content=VIDEO)

    with pytest.raises(DownloadError, match="너무 큽니다"):
        await download_video(client(handler), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 50)


async def test_too_big_by_stream(tmp_path):
    async def chunks():
        for i in range(0, len(VIDEO), 16):
            yield VIDEO[i : i + 16]

    def handler(request):
        # Content-Length 없이 스트리밍 → 받으면서 크기를 센다
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=chunks())

    with pytest.raises(DownloadError, match="너무 큽니다"):
        await download_video(client(handler), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 50)
    assert not list(tmp_path.iterdir())  # 임시 파일이 남지 않는다


async def test_wrong_content_type(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>")

    with pytest.raises(DownloadError, match="영상 형식"):
        await download_video(client(handler), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 10_000)


async def test_gone(tmp_path):
    with pytest.raises(DownloadError) as e:
        await download_video(client(lambda r: httpx.Response(404)), "https://replicate.delivery/a.mp4",
                             tmp_path, JOB, HOSTS, 10_000)
    assert e.value.gone


@pytest.mark.parametrize(
    "job_id", ["../../evil", "j_1", "j_20261122_120000_abcdef/../x", "j_20261122_120000_abcdef" + chr(10)]
)
async def test_invalid_job_id(tmp_path, job_id):
    with pytest.raises(DownloadError, match="작업 ID"):
        await download_video(client(ok), "https://replicate.delivery/a.mp4", tmp_path, job_id, HOSTS, 10_000)


async def test_octet_stream_with_mp4_signature_accepted(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "application/octet-stream"}, content=VIDEO)

    path = await download_video(client(handler), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 10_000)
    assert path.read_bytes() == VIDEO


async def test_octet_stream_without_signature_rejected(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "application/octet-stream"}, content=b"<html>" * 10)

    with pytest.raises(DownloadError, match="MP4"):
        await download_video(client(handler), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 10_000)
    assert not list(tmp_path.iterdir())


async def test_heartbeat_called(tmp_path, monkeypatch):
    from capped_video_mcp import download as d

    monkeypatch.setattr(d, "HEARTBEAT_SEC", 0.0)
    beats = []

    async def chunks():
        for i in range(0, len(VIDEO), 16):
            yield VIDEO[i : i + 16]

    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=chunks())

    await download_video(client(handler), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 10_000,
                         on_progress=lambda: beats.append(1))
    assert beats


async def test_concurrent_downloads_use_separate_part_files(tmp_path):
    import asyncio

    paths = await asyncio.gather(*[
        download_video(client(ok), "https://replicate.delivery/a.mp4", tmp_path, JOB, HOSTS, 10_000)
        for _ in range(3)
    ])
    assert all(p == tmp_path / f"{JOB}.mp4" for p in paths)
    assert (tmp_path / f"{JOB}.mp4").read_bytes() == VIDEO
