"""
팀 공유용 HTTP 서버 — dashboard.html을 네트워크에 노출
사용법: python server.py
팀원: http://<이_PC의_IP>:8080/dashboard.html
"""
import http.server, socketserver, socket
from pathlib import Path

PORT      = 8080
DIRECTORY = Path(__file__).parent


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIRECTORY), **kwargs)

    def log_message(self, fmt, *args):
        pass  # 로그 억제


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


if __name__ == "__main__":
    ip = local_ip()
    print("=" * 50)
    print("리니지W 대만 모니터 — 팀 공유 서버")
    print(f"  내 PC  : http://localhost:{PORT}/dashboard.html")
    print(f"  팀원용 : http://{ip}:{PORT}/dashboard.html")
    print("종료: Ctrl+C")
    print("=" * 50)
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        httpd.serve_forever()
