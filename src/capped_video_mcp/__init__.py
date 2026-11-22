"""영상 생성 MCP 서버 (fal, Replicate)."""


def main() -> None:
    import sys

    # Windows 콘솔 기본 인코딩(cp949)에서 stderr 로그 한글이 깨지지 않게 한다. stdout(프로토콜)은 건드리지 않는다.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    from .log import setup_logging
    from .server import create_server
    from .service import build_app

    app = build_app()
    server = create_server(app)
    # SDK 가 stderr 로그 핸들러를 만든 뒤에 파일 핸들러와 가리기 필터를 붙인다.
    setup_logging(app.home, app.secrets.values())
    server.run("stdio")
