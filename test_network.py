import gzip
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from podcast_network import download_to, fetch_bytes, fetch_feed


class Reply(io.BytesIO):
    def __init__(self, data, status=200, **headers):
        super().__init__(data)
        self.status = status
        self.getheader = headers.get


class NetworkTest(unittest.TestCase):
    def test_feed_returns_redirect_destination_and_decodes_gzip(self):
        original = "https://audio.example/feeds/test"
        final = "https://publisher.example/moved/test.xml"
        xml = b"<rss/>"
        hops = [
            (Mock(), Reply(b"", 302, Location=final)),
            (Mock(), Reply(gzip.compress(xml), **{"Content-Encoding": "gzip"})),
        ]
        with patch("podcast_network._request", side_effect=hops) as request:
            self.assertEqual(fetch_feed(original, max_bytes=1000), (xml, final))
        self.assertEqual([c.args[0] for c in request.call_args_list], [original, final])
        for connection, reply in hops:
            connection.close.assert_called_once()
            self.assertTrue(reply.closed)

    def test_gzip_limit_applies_to_decoded_bytes(self):
        reply = Reply(gzip.compress(b"a" * 10000), **{"Content-Encoding": "gzip"})
        with patch("podcast_network._request", return_value=(Mock(), reply)):
            with self.assertRaisesRegex(ValueError, "size limit"):
                fetch_bytes("https://audio.example/feed", max_bytes=500)

    def test_interrupted_download_preserves_existing_file(self):
        with TemporaryDirectory() as temp:
            dest = Path(temp) / "episode.mp3"
            dest.write_bytes(b"original")
            reply = Reply(b"partial", **{"Content-Length": "30"})
            with patch("podcast_network._request", return_value=(Mock(), reply)):
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    download_to("https://audio.example/episode.mp3", dest, max_bytes=500)
            self.assertEqual(dest.read_bytes(), b"original")
            self.assertEqual(list(Path(temp).iterdir()), [dest])

    def test_invalid_url_fails_before_dns(self):
        with patch("podcast_network.socket.getaddrinfo") as resolve:
            for url in ["https://", "https://audio.example:0/feed", "https://audio.example:no/feed",
                        "https://bad host.example/feed", "https://["]:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    fetch_bytes(url, max_bytes=100)
            resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
