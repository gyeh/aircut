"""Verify the existing macOS Tailscale Serve ingress for private episode pages."""
import json
import re
import subprocess


_TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
_EXTENSION = "io.tailscale.ipn.macsys.network-extension"
_EXTENSION_PATH = re.compile(
    r"/Library/SystemExtensions/[0-9A-Fa-f-]{36}/"
    + re.escape(f"{_EXTENSION}.systemextension/Contents/MacOS/{_EXTENSION}")
)


def _output(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=3)


def verified_serve_origin(gateway_port, source_ip=None):
    """Allow the node's private HTTPS root proxy only, with no other forwarding rules."""
    try:
        status = json.loads(_output(_TAILSCALE, "status", "--json"))
        config = json.loads(_output(_TAILSCALE, "serve", "status", "--json"))
        node = status["Self"]
        host = node["DNSName"].removesuffix(".")
        suffix = status["CurrentTailnet"]["MagicDNSSuffix"]
        if (status["BackendState"] != "Running" or node["Online"] is not True
                or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+\.ts\.net", host)
                or not suffix or not host.endswith("." + suffix)):
            return ""
        host_port = host + ":443"
        target = f"http://127.0.0.1:{gateway_port}"
        if (set(config) - {"Web", "TCP", "AllowFunnel"}
                or any(value is not False for value in config.get("AllowFunnel", {}).values())
                or config["TCP"] != {"443": {"HTTPS": True}}
                or config["Web"] != {host_port: {"Handlers": {"/": {"Proxy": target}}}}):
            return ""
        if source_ip is not None:
            nodes = [node, *status.get("Peer", {}).values()]
            if not any(peer.get("DNSName", "").removesuffix(".").endswith("." + suffix)
                       and source_ip in peer.get("TailscaleIPs", []) for peer in nodes):
                return ""
        return "https://" + host
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError):
        return ""


def _serve_owns_connection(peer, gateway_port):
    # Verify the client side of this exact live TCP connection. Other local
    # proxies and direct callers can supply identical HTTP headers.
    if peer[0] != "127.0.0.1":
        return False
    try:
        processes = _output("/bin/ps", "-axo", "uid=,pid=,comm=")
        for line in processes.splitlines():
            fields = line.split(None, 2)
            if len(fields) == 3 and fields[0] == "0" and _EXTENSION_PATH.fullmatch(fields[2]):
                flows = _output("/usr/bin/nettop", "-m", "tcp", "-n", "-L", "1",
                                "-p", fields[1], "-J", "state")
                connection = f"tcp4 127.0.0.1:{peer[1]}<->127.0.0.1:{gateway_port},Established,"
                if connection in flows.splitlines():
                    return True
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return False


def private_page_allowed(headers, peer, gateway_port):
    sources = headers.get_all("X-Forwarded-For", [])
    if (len(sources) != 1 or not headers.get("Tailscale-User-Login")
            or "Tailscale-Funnel-Request" in headers or not _serve_owns_connection(peer, gateway_port)):
        return False
    origin = verified_serve_origin(gateway_port, sources[0])
    return bool(origin and headers.get_all("X-Forwarded-Proto") == ["https"]
                and headers.get_all("X-Forwarded-Host") in ([origin[8:]], [origin[8:] + ":443"]))
