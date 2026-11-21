"""本地 HTTP 服务：静态网页 + JSON API + 方块纹理。

只在 127.0.0.1 上监听，所有 /api/* 请求都要带一次性令牌；静态文件走白名单，
请求体有大小上限，路径参数做目录穿越校验。
"""

import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .assets import TextureLibrary
from .service import (MAX_BODY_BYTES, Session, is_valid_block_id,
                      pretty_block_name)

FRONTEND_DIR = Path(__file__).with_name("frontend")
STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' "
        "https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


class MiauServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, session: Session, textures: TextureLibrary,
                 token: str) -> None:
        super().__init__(address, Handler)
        self.session = session
        self.textures = textures
        self.token = token
        self.port = self.server_address[1]
        index = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        self.index_html = index.replace("__MIAU_TOKEN__", token)


class Handler(BaseHTTPRequestHandler):
    server_version = "MiauWeb/1.0"
    server: MiauServer

    # ── 基础工具 ─────────────────────────────────────────────
    def log_message(self, fmt, *args):  # noqa: A003 - 基类签名
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _authorized(self) -> bool:
        token = self.headers.get("X-Miau-Token") or ""
        return secrets.compare_digest(token, self.server.token)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("请求不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    # ── 路由 ─────────────────────────────────────────────────
    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        try:
            if not self._host_ok():
                self._error(HTTPStatus.FORBIDDEN, "非法的 Host")
                return
            parsed = urlparse(self.path)
            route = parsed.path
            if route in ("/", "/index.html"):
                body = self.server.index_html.encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8",
                           {"Cache-Control": "no-store"})
                return
            if route.startswith("/static/"):
                self._serve_static(route[len("/static/"):])
                return
            if not route.startswith("/api/"):
                self._error(HTTPStatus.NOT_FOUND, "没有这个页面")
                return
            if not self._authorized():
                self._error(HTTPStatus.FORBIDDEN, "令牌无效，请从启动窗口重新打开")
                return
            if route == "/api/state":
                self._json({"ok": True, "state": self.server.session.state()})
            elif route == "/api/preview":
                query = parse_qs(parsed.query)
                ticks = query.get("ticks", [None])[0]
                limit = int(ticks) if ticks not in (None, "") else None
                self._json({"ok": True,
                            "preview": self.server.session.preview(limit)})
            elif route == "/api/texture":
                query = parse_qs(parsed.query)
                block = (query.get("block", [""])[0] or "").strip()
                if not block or len(block) > 160:
                    self._error(HTTPStatus.BAD_REQUEST, "方块参数不合法")
                    return
                png = self.server.textures.texture_png(block)
                self._send(200, png, "image/png",
                           {"Cache-Control": "public, max-age=86400"})
            elif route == "/api/block-info":
                query = parse_qs(parsed.query)
                block = (query.get("block", [""])[0] or "").strip()
                if not block or len(block) > 160:
                    self._error(HTTPStatus.BAD_REQUEST, "方块参数不合法")
                    return
                valid = is_valid_block_id(block)
                self._json({"ok": True, "info": {
                    "block": block,
                    "valid": valid,
                    "hasTexture": bool(valid and self.server.textures.has_texture(block)),
                    "name": pretty_block_name(block) if valid else block,
                }})
            else:
                self._error(HTTPStatus.NOT_FOUND, "没有这个接口")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - 统一兜底，避免服务崩溃
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器错误：{exc}")

    def do_POST(self):  # noqa: N802
        try:
            if not self._host_ok():
                self._error(HTTPStatus.FORBIDDEN, "非法的 Host")
                return
            route = urlparse(self.path).path
            if not route.startswith("/api/"):
                self._error(HTTPStatus.NOT_FOUND, "没有这个接口")
                return
            if not self._authorized():
                self._error(HTTPStatus.FORBIDDEN, "令牌无效，请从启动窗口重新打开")
                return
            payload = self._read_json()
            session = self.server.session
            if route == "/api/song":
                self._json({"ok": True, "state": session.load_song(payload.get("path", ""))})
            elif route == "/api/pick":
                self._json({"ok": True, "path": _pick_dialog(payload.get("kind", "nbs"))})
            elif route == "/api/config":
                patch = payload.get("patch")
                if not isinstance(patch, dict):
                    raise ValueError("patch 必须是对象")
                self._json({"ok": True, "state": session.set_config(patch)})
            elif route == "/api/groups":
                self._json({"ok": True, "state": session.set_groups(payload.get("groups", []))})
            elif route == "/api/auto-group":
                self._json({"ok": True,
                            "state": session.auto_group(int(payload.get("mode", 1)))})
            elif route == "/api/offsets":
                self._json({"ok": True,
                            "state": session.set_offsets(payload.get("offsets", {}))})
            elif route == "/api/offsets/reset":
                self._json({"ok": True, "state": session.reset_offsets()})
            elif route == "/api/generate":
                result = session.generate(payload.get("path", ""), payload.get("ticks"))
                self._json({"ok": True, "result": result})
            elif route == "/api/open-folder":
                self._open_folder(payload.get("path", ""))
                self._json({"ok": True})
            else:
                self._error(HTTPStatus.NOT_FOUND, "没有这个接口")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器错误：{exc}")

    # ── 静态文件 ─────────────────────────────────────────────
    def _serve_static(self, name: str) -> None:
        clean = name.split("?")[0]
        if not clean or "/" in clean or "\\" in clean or clean.startswith("."):
            self._error(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        path = FRONTEND_DIR / clean
        if not path.is_file() or path.suffix.lower() not in STATIC_TYPES:
            self._error(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        body = path.read_bytes()
        self._send(200, body, STATIC_TYPES[path.suffix.lower()],
                   {"Cache-Control": "no-cache"})

    def _open_folder(self, raw: str) -> None:
        path = str(raw or "").strip()
        if not path:
            raise ValueError("路径为空")
        target = Path(path).expanduser()
        if target.is_file():
            target = target.parent
        if not target.is_dir():
            raise ValueError("文件夹不存在")
        if os.name == "nt":
            os.startfile(str(target))  # noqa: S606 - 本地桌面工具
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])  # noqa: S603
        else:
            subprocess.Popen(["xdg-open", str(target)])  # noqa: S603


def _pick_dialog(kind: str) -> str:
    """用系统文件对话框选文件（tkinter 子进程，避免阻塞服务线程）。"""
    if kind == "schem":
        dialog = (
            "p=filedialog.asksaveasfilename(parent=r, title='保存结构文件', "
            "defaultextension='.schem', filetypes=[('WorldEdit 结构','*.schem'),"
            "('所有文件','*.*')])"
        )
    else:
        dialog = (
            "p=filedialog.askopenfilename(parent=r, title='选择 NBS 歌曲', "
            "filetypes=[('Note Block Studio','*.nbs'),('所有文件','*.*')])"
        )
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r=tk.Tk(); r.withdraw(); r.attributes('-topmost', True); r.update()\n"
        f"{dialog}\n"
        "print(p or '')\n"
        "r.destroy()\n"
    )
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"无法打开文件选择框：{exc}") from exc
    return (proc.stdout or "").strip().splitlines()[-1].strip() if proc.stdout else ""


def _assets_setting() -> str:
    env = os.environ.get("MIAU_MC_ASSETS")
    if env:
        return env
    try:
        from ..config import default_config
        return str(default_config().get("mc_assets_dir") or "")
    except Exception:  # noqa: BLE001
        return ""


def create_server(port: int = 0, assets_dir: str = None):
    token = secrets.token_urlsafe(24)
    textures = TextureLibrary(assets_dir or _assets_setting())
    session = Session(textures)
    last_error = None
    for attempt in range(12):
        try:
            server = MiauServer(("127.0.0.1", port or 0), session, textures, token)
            return server
        except OSError as exc:
            last_error = exc
            if port:
                port += 1
            else:
                break
    raise OSError(f"无法启动本地服务：{last_error}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    no_browser = "--no-browser" in argv
    assets_dir = None
    port = 0
    for index, item in enumerate(argv):
        if item == "--mc-assets" and index + 1 < len(argv):
            assets_dir = argv[index + 1]
        if item == "--port" and index + 1 < len(argv):
            try:
                port = int(argv[index + 1])
            except ValueError:
                port = 0

    server = create_server(port, assets_dir)
    url = f"http://127.0.0.1:{server.port}/"
    status = server.textures.status()
    lines = [
        "",
        "  ███ MiauRedstoneMusic · 网页工作台",
        f"  地址   {url}",
        f"  纹理   {status['source'] or '未找到 Minecraft 资源（将使用纯色方块）'}",
        "  关闭   在这个窗口按 Ctrl+C",
        "",
    ]
    print("\n".join(lines))
    if not no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n  已关闭网页工作台。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
