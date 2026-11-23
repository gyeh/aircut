"""Size-limited HTTP fetching for public podcast feeds and their enclosures."""
import gzip
import http.client
import io
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import tempfile
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit
import zlib


def _request(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
        raise ValueError("Enter a valid public HTTP or HTTPS URL.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid podcast host and port.") from exc
    if parsed.scheme not in ("http", "https") or not hostname or parsed.username is not None:
        raise ValueError("Use a public HTTP or HTTPS URL without embedded credentials.")
    if any(c.isspace() or c == "\\" for c in hostname) or port == 0:
        raise ValueError("Enter a valid podcast host and port.")
    try:
        host = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Enter a valid podcast host name.") from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("The podcast host could not be resolved.")
    for _, _, _, _, addr in addresses:
        ip = ipaddress.ip_address(addr[0])
        if not ip.is_global or ip.is_multicast or (ip.version == 6 and ip.ipv4_mapped and not ip.ipv4_mapped.is_global):
            raise ValueError("Podcast feeds and media must use a public internet host.")

    def connect_validated(address, timeout, source_address):
        # Pin the socket to the DNS answers checked above. TLS still verifies host.
        last_error = None
        for family, socktype, proto, _, addr in addresses:
            try:
                sock = socket.socket(family, socktype, proto)
                try:
                    sock.settimeout(timeout)
                    sock.connect(addr)
                    return sock
                except OSError:
                    sock.close()
                    raise
            except OSError as exc:
                last_error = exc
        raise last_error

    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=35, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=35)
    conn._create_connection = connect_validated
    target = urlunsplit(("", "", quote(parsed.path or "/", safe="/%:@!$&'()*+,;=~-._"), quote(parsed.query, safe="/?%:@!$&'()*+,;=~-._"), ""))
    try:
        conn.request("GET", target, headers={"User-Agent": "Aircut/1.0 (personal podcast reader)", "Accept-Encoding": "identity"})
        return conn, conn.getresponse()
    except Exception:
        conn.close()
        raise


def _fetch(url, target, limit):
    try:
        for _ in range(8):
            conn, reply = _request(url)
            try:
                if reply.status in (301, 302, 303, 307, 308) and reply.getheader("Location"):
                    url = urljoin(url, reply.getheader("Location"))
                    continue
                if reply.status != 200:
                    raise ValueError(f"Podcast host returned HTTP {reply.status}.")
                size_header = reply.getheader("Content-Length")
                size = None
                if size_header is not None:
                    length = size_header.strip()
                    if not (length.isascii() and length.isdecimal()):
                        raise ValueError("The podcast host sent an invalid content length.")
                    size = int(length)
                if size is not None and size > limit:
                    raise ValueError("Podcast download exceeds the configured size limit.")
                encoding = (reply.getheader("Content-Encoding") or "identity").strip().lower()
                if encoding not in ("identity", "gzip"):
                    raise ValueError("Unsupported encoding from podcast host.")
                reader = gzip.GzipFile(fileobj=reply) if encoding == "gzip" else reply
                count = 0
                while block := reader.read(128 * 1024):
                    count += len(block)
                    if count > limit:
                        raise ValueError("Podcast download exceeds the configured size limit.")
                    target.write(block)
                if not count or (size is not None and encoding == "identity" and count != size):
                    raise ValueError("The podcast host returned an incomplete download.")
                return urldefrag(url).url
            finally:
                reply.close()
                conn.close()
        raise ValueError("Too many podcast redirects.")
    except (OSError, EOFError, zlib.error, http.client.HTTPException) as exc:
        raise ValueError(f"Could not download from the podcast host ({type(exc).__name__}).") from exc


def fetch_feed(url, *, max_bytes):
    """Return decoded feed bytes and their final location for relative XML links."""
    buf = io.BytesIO()
    final_url = _fetch(url, buf, max_bytes)
    return buf.getvalue(), final_url


def fetch_bytes(url, *, max_bytes):
    return fetch_feed(url, max_bytes=max_bytes)[0]


def download_to(url, path, *, max_bytes):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        name = temp.name
        try:
            _fetch(url, temp, max_bytes)
            temp.close()
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
