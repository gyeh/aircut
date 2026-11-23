#!/usr/bin/env python3
"""Aircut — a personal podcast server with a separate authenticated iPhone gateway."""
import argparse
from datetime import datetime
from email.utils import format_datetime
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import signal
import stat
import threading
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

from podcast_access import Access, ROOT, configure_runtime
from podcast_library import Library, PlaybackConflict
from podcast_processing import processing_readiness

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)
STATIC = ROOT / "static"
PAIR_PAGE = b"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair with Aircut</title><link rel="stylesheet" href="/pair.css"><main><img src="/static/icon.png" width="68" height="68" alt="">
<h1>Pair with Aircut</h1><p>On your Mac, open iPhone access in Aircut. Open the private Safari link on this device to enter your listening room.</p>
<p role="status" id="status"></p></main><script src="/pair.js"></script></html>"""
PAIR_JS = b"""(async () => {
  const key = location.hash.slice(1);
  if (!key) return;
  history.replaceState(null, '', '/pair');
  const status = document.getElementById('status');
  status.textContent = 'Pairing this device...';
  try {
    const r = await fetch('/api/pair', {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
    if (r.ok) location.replace('/');
    else status.textContent = 'This link was not accepted. Copy a fresh Safari link from your Mac.';
  } catch { status.textContent = 'Could not reach your Mac. Open the pairing link again.'; }
})();"""
PAIR_CSS = b"""*{box-sizing:border-box}body{background:#f3f1e8;color:#22251f;font:18px/1.5 system-ui,sans-serif;margin:0;padding:8vw}main{max-width:480px;margin:9vh auto}img{border-radius:14px}h1{font:2.8rem/1.1 Georgia,serif;letter-spacing:-.05em}"""


def private_rss(library, access, base):
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    for tag, value in (("title", "Aircut"), ("link", base + "/"), ("description", "Your processed podcasts, with takeaways and summaries."),
                       ("language", "en"), (f"{{{ITUNES}}}author", "Aircut")):
        ET.SubElement(channel, tag).text = value
    ET.SubElement(channel, f"{{{ITUNES}}}image", href=base + "/static/cover.png")
    for episode in library.list_ready():
        path = Path(episode["audio_path"])
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = episode["title"]
        ET.SubElement(item, f"{{{ITUNES}}}author").text = episode["feed_title"]
        if episode["image_url"]:
            ET.SubElement(item, f"{{{ITUNES}}}image", href=episode["image_url"])
        guid = f"aircut-{access.token('guid')[:16]}-{episode['id']}"
        ET.SubElement(item, "guid", isPermaLink="false").text = guid
        ET.SubElement(item, "description").text = episode["takeaways"] + "\n\n" + episode["summary"]
        if episode["published"]:
            ET.SubElement(item, "pubDate").text = format_datetime(datetime.fromisoformat(episode["published"]))
        ET.SubElement(item, f"{{{ITUNES}}}duration").text = str(round(episode["duration"]))
        media = f"/audio/{episode['id']}.mp3"
        ET.SubElement(item, "enclosure", url=f"{base}{media}?key={access.token(media)}", type="audio/mpeg", length=str(info.st_size))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def byte_range(header, size):
    if header is None:
        return (0, size - 1)
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or not any(m.groups()) or max(len(s) for s in m.groups()) > 18:
        raise ValueError
    a, b = m.groups()
    start = int(a) if a else max(0, size - int(b))
    end = min(size - 1, int(b)) if a and b else size - 1
    if start > end or start >= size:
        raise ValueError
    return start, end


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, library, access, local, local_port):
        self.library = library
        self.access = access
        self.local = local
        self.local_url = f"http://127.0.0.1:{local_port}"
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "Aircut"
    sys_version = ""
    timeout = 30

    def log_message(self, *args):
        # Private RSS and enclosure paths are bearer credentials.
        pass

    def send_data(self, status, content, mime="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def end_headers(self):
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; media-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def reply(self, data, status=200, headers=None):
        self.send_data(status, json.dumps(data, ensure_ascii=False).encode(), headers=headers)

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Use an application/json request.")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= 16384:
            raise ValueError("Request is too large.")
        obj = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(obj, dict):
            raise ValueError("Expected a JSON object.")
        return obj

    def trusted_local(self):
        host = self.headers.get("Host", "").lower()
        port = self.server.server_port
        return (self.server.local and host in (f"127.0.0.1:{port}", f"localhost:{port}") and
                not any(name.lower().startswith(("forwarded", "x-forwarded-", "cf-")) for name in self.headers))

    def logged_in(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        value = cookie.get("aircut_session")
        return value is not None and self.server.access.session_valid(value.value)

    def dispatch(self):
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except KeyError:
            self.reply({"error": "This item could not be found."}, 404)
        except PlaybackConflict as exc:
            self.reply({"error": str(exc), "playback_position": exc.position, "playback_revision": exc.revision}, 409)
        except (ValueError, UnicodeError) as exc:
            self.reply({"error": str(exc) or "Invalid request."}, 400)
        except Exception:
            logging.exception("Error handling Aircut request")
            self.reply({"error": "The server could not complete this request."}, 500)

    do_GET = do_HEAD = do_POST = do_DELETE = dispatch

    def route(self):
        target = urlsplit(self.path)
        path = target.path
        reading = self.command in ("GET", "HEAD")
        local = self.trusted_local()
        if self.server.local and not local:
            return self.reply({"error": "Open the local dashboard using localhost or 127.0.0.1."}, 403)
        access, lib = self.server.access, self.server.library
        state = access.state(local, self.server.local_url)
        base = access.url or self.server.local_url
        if not reading:
            origin = self.headers.get("Origin")
            origins = {self.server.local_url, self.server.local_url.replace("127.0.0.1", "localhost")} if local else {access.url}
            if (origin and origin not in origins) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.reply({"error":"Request origin was not accepted."}, 403)

        if reading:
            if path in ("/pair", "/pair.js", "/pair.css"):
                content, mime = {"/pair": (PAIR_PAGE, "text/html; charset=utf-8"), "/pair.js": (PAIR_JS, "text/javascript"), "/pair.css": (PAIR_CSS, "text/css")}[path]
                return self.send_data(200, content, mime)
            if re.fullmatch(r"/static/[a-zA-Z0-9_.-]+", path):
                asset = STATIC / path.split("/")[-1]
                if asset.is_file():
                    return self.send_data(200, asset.read_bytes(), mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
                return self.reply({"error":"Asset not found."}, 404)
            if path.startswith("/private/"):
                if hmac.compare_digest(path.encode(), f"/private/{access.token('feed')}/feed.xml".encode()):
                    return self.send_data(200, private_rss(lib, access, base), "application/rss+xml; charset=utf-8")
                return self.reply({"error":"Feed not found."}, 404)
        if path == "/api/pair" and self.command == "POST":
            key = self.body().get("key")
            if not isinstance(key, str) or not hmac.compare_digest(key.encode(), access.token("pair").encode()):
                return self.reply({"error":"Invalid pairing link."}, 401)
            return self.reply({"ok":True}, headers={"Set-Cookie": f"aircut_session={access.session()}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=5184000"})
        audio = re.fullmatch(r"/audio/(\d+)\.mp3", path)
        media_key = parse_qs(target.query).get("key", [""])[0] if audio else ""
        if not (local or self.logged_in() or (reading and audio and hmac.compare_digest(media_key.encode(), access.token(path).encode()))):
            if reading and path == "/":
                return self.send_data(200, PAIR_PAGE, "text/html; charset=utf-8")
            return self.reply({"error":"Pair this device using the private Safari link on your Mac."}, 401)
        if reading and audio:
            return self.audio(lib.get_episode(int(audio[1])))
        if path == "/" and reading:
            return self.send_data(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state" and reading:
            ready, hint = processing_readiness()
            state.update(processing_ready=ready, setup_hint=hint, poll_minutes=lib.poll_minutes, episode_deletion=True)
            state.update(lib.player_settings())
            return self.reply({"feeds": lib.list_feeds(), "episodes": lib.list_episodes(), "settings": state})
        detail = re.fullmatch(r"/api/episodes/(\d+)", path)
        if detail and reading:
            episode = dict(lib.get_episode(int(detail[1])))
            episode.pop("audio_path", None)
            episode["audio_url"] = f"/audio/{detail[1]}.mp3" if episode["has_audio"] else ""
            return self.reply(episode)
        if detail and self.command == "DELETE":
            result = lib.delete_episode(int(detail[1]))
            return self.reply(result, 202 if result["cleanup_pending"] else 200)
        if self.command == "DELETE" and (feed := re.fullmatch(r"/api/feeds/(\d+)", path)):
            lib.unsubscribe(int(feed[1]))
            return self.reply({"ok":True})
        if self.command == "DELETE" and path == "/api/tunnel":
            if not local or access.fixed_url:
                return self.reply({"error":"Only a temporary tunnel on this Mac can be closed here."}, 403)
            access.stop()
            return self.reply({"ok":True})
        if self.command == "POST":
            body = self.body()
            if path == "/api/player/settings":
                return self.reply(lib.set_player_settings(body.get("skip_back_seconds"), body.get("skip_forward_seconds")))
            if save := re.fullmatch(r"/api/episodes/(\d+)/save", path):
                return self.reply(lib.set_saved(int(save[1]), body.get("saved")))
            if progress := re.fullmatch(r"/api/episodes/(\d+)/progress", path):
                return self.reply(lib.set_progress(int(progress[1]), body.get("position"), body.get("revision")))
            if path == "/api/feeds":
                url = body.get("url")
                if not isinstance(url, str):
                    raise ValueError("Enter a podcast feed URL.")
                return self.reply(lib.subscribe(url.strip(), body.get("backfill", 1)), 201)
            if path == "/api/refresh":
                lib.refresh()
                return self.reply({"ok":True}, 202)
            if retry := re.fullmatch(r"/api/episodes/(\d+)/retry", path):
                lib.retry(int(retry[1]))
                return self.reply({"ok":True}, 202)
            if path == "/api/tunnel":
                if not local:
                    return self.reply({"error":"Enable iPhone access from this Mac."}, 403)
                access.start_tunnel()
                return self.reply({"ok":True}, 202)
            if path == "/api/logout":
                return self.reply({"ok":True}, headers={"Set-Cookie":"aircut_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"})
        return self.reply({"error":"Page not found."}, 404)

    def audio(self, episode):
        if not episode["has_audio"] or not Path(episode["audio_path"]).is_file():
            return self.reply({"error":"The cleaned audio is not available yet."}, 404)
        try:
            stream = open(episode["audio_path"], "rb")
        except (FileNotFoundError, IsADirectoryError):
            return self.reply({"error":"The cleaned audio is not available yet."}, 404)
        with stream:
            size = os.fstat(stream.fileno()).st_size
            header = self.headers.get("Range")
            try:
                start, end = byte_range(header, size)
            except ValueError:
                return self.send_data(416, b"", "audio/mpeg", {"Content-Range": f"bytes */{size}"})
            self.send_response(206 if header else 200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(end - start + 1))
            if header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command != "HEAD":
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(remaining, 128 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="Local dashboard port (default 8765)")
    parser.add_argument("--gateway-port", type=int, default=8766, help="Authenticated loopback gateway (default 8766)")
    parser.add_argument("--data", type=Path, default=ROOT / ".aircut")
    parser.add_argument("--poll-minutes", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.poll_minutes <= 1440:
        parser.error("--poll-minutes must be between 1 and 1440")
    if not (0 < args.port < 65536 and 0 < args.gateway_port < 65536 and args.port != args.gateway_port):
        parser.error("Choose two different ports between 1 and 65535")
    os.umask(0o077)
    configure_runtime()
    data = args.data.expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    lib = Library(data, args.poll_minutes)
    access = Access(data, args.gateway_port, os.environ.get("PODCAST_PUBLIC_URL", ""))
    with WebServer(args.port, lib, access, True, args.port) as local, WebServer(args.gateway_port, lib, access, False, args.port) as gateway:
        stopped = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
        lib.start()
        for server in (local, gateway):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Aircut is running at {local.local_url}", flush=True)
        try:
            stopped.wait()
        finally:
            access.stop()
            local.shutdown()
            gateway.shutdown()
            lib.stop()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(f"Aircut: {exc}")
