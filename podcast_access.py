"""Local configuration, private links, and the optional HTTPS tunnel."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def configure_runtime():
    config = ROOT / ".env"
    if config.is_file():
        for n, raw in enumerate(config.read_text().splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ValueError(f"Invalid setting on line {n} in .env. Use NAME=value.")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)
    mlx = ROOT / ".runtime/mlx/bin/python"
    cache = Path.home() / ".cache/huggingface/hub/models--mlx-community--whisper-small-mlx/snapshots"
    if not (os.environ.get("WHISPER_CPP_MODEL") or os.environ.get("PODCAST_MLX_PYTHON") or os.environ.get("PODCAST_MLX_MODEL")) and mlx.exists():
        for model in sorted(cache.glob("*")):
            if (model / "config.json").is_file() and (model / "weights.npz").is_file():
                os.environ["PODCAST_MLX_MODEL"] = str(model)
                os.environ["PODCAST_MLX_PYTHON"] = str(mlx)
                break


class Access:
    def __init__(self, data_dir, gateway_port, public_url=""):
        self.gateway_port = gateway_port
        self.fixed_url = public_url.rstrip("/")
        if self.fixed_url:
            parsed = urlsplit(self.fixed_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.path or parsed.query or parsed.fragment:
                raise ValueError("PODCAST_PUBLIC_URL must be a bare HTTPS origin.")
        self.lock = threading.Lock()
        self.url = self.fixed_url
        self.error = ""
        self.process = None
        self.tunnel_bin = shutil.which("cloudflared") or str(ROOT / ".runtime/cloudflared")
        secret = data_dir / "access.key"
        if not secret.exists():
            with secret.open("xb") as f:
                os.chmod(secret, 0o600)
                f.write(secrets.token_bytes(32))
        self.secret = secret.read_bytes()
        if len(self.secret) != 32:
            raise ValueError("The access key is incomplete. Restore access.key or remove it to rotate all links.")

    def token(self, purpose):
        return hmac.new(self.secret, purpose.encode(), hashlib.sha256).hexdigest()

    def session(self):
        value = f"{int(time.time()) + 60 * 86400}.{secrets.token_hex(12)}"
        return value + "." + self.token("session/" + value)

    def session_valid(self, cookie):
        if not re.fullmatch(r"\d{1,12}\.[a-f0-9]{24}\.[a-f0-9]{64}", cookie):
            return False
        expires, _, _ = cookie.split(".")
        value, sig = cookie.rsplit(".", 1)
        return int(expires) > time.time() and hmac.compare_digest(sig, self.token("session/" + value))

    def state(self, local, local_url):
        with self.lock:
            base = self.url or local_url
            return {
                "rss_url": f"{base}/private/{self.token('feed')}/feed.xml",
                "remote_url": (self.url + ("/pair#" + self.token("pair") if local else "/")) if self.url else "",
                "can_start_tunnel": local and not self.fixed_url and os.access(self.tunnel_bin, os.X_OK),
                "tunnel_starting": self.process is not None and not self.url,
                "tunnel_error": self.error,
                "temporary_tunnel": bool(self.url and not self.fixed_url),
                "local": local,
            }

    def start_tunnel(self):
        with self.lock:
            if self.url or self.process:
                return
            if not os.access(self.tunnel_bin, os.X_OK):
                raise ValueError("Install cloudflared or set PODCAST_PUBLIC_URL to your HTTPS gateway.")
            self.error = ""
            self.process = subprocess.Popen(
                [self.tunnel_bin, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url",
                 f"http://127.0.0.1:{self.gateway_port}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            )
            threading.Thread(target=self._watch, args=(self.process,), daemon=True).start()

    def _watch(self, process):
        candidate = ""
        connected = False
        def startup_timeout():
            with self.lock:
                if self.process is process and not self.url:
                    self.error = "The HTTPS tunnel could not connect within 90 seconds. Check internet access, then try again."
                    process.terminate()
        timer = threading.Timer(90, startup_timeout)
        timer.daemon = True
        timer.start()
        try:
            for line in process.stdout:
                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com\b", line)
                if match:
                    candidate = match.group()
                if "Registered tunnel connection" in line:
                    connected = True
                if candidate and connected:
                    with self.lock:
                        if self.process is process:
                            self.url = candidate
                    timer.cancel()
            process.wait()
        finally:
            timer.cancel()
            process.stdout.close()
            with self.lock:
                if self.process is process:
                    self.process = None
                    self.url = self.fixed_url
                    if not self.error:
                        self.error = "The temporary tunnel closed. Enable iPhone access again or configure a stable HTTPS gateway."

    def stop(self):
        with self.lock:
            process, self.process = self.process, None
            self.url = self.fixed_url
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
