"""HTTP integration tests for the iPhone gateway and seekable podcast feed."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from aircut import Access, ITUNES, WebServer
from podcast_library import Library


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        audio = Path(self.temp.name) / "episodes" / "1" / "episode.mp3"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(bytes(range(256)))
        self.library = Library(Path(self.temp.name))
        with self.library._db(write=True) as con:
            con.execute("""INSERT INTO feeds(id,url,title,image_url,baseline,last_checked)
                           VALUES(1,'https://example.org/rss','Show','','','')""")
            con.execute("""INSERT INTO episodes
                        (id,feed_id,episode_key,audio_url,title,description,published,image_url,discovered,
                         status,duration,takeaways,summary,audio_path)
                        VALUES(1,1,'fixture','https://example.org/fixture.mp3','A & B','',
                         '2026-01-01T00:00:00+00:00','','','ready',24.5,'Some <notes>','More',?)""", (str(audio),))

        self.public = "https://aircut.example.org"
        self.access = Access(Path(self.temp.name), 8766, self.public)
        self.local = self.start_server(self.library, True)
        self.gateway = self.start_server(self.library, False)

    def start_server(self, lib, local):
        web = WebServer(0, lib, self.access, local, 8765)
        thread = threading.Thread(target=web.serve_forever, kwargs={"poll_interval":.01}, daemon=True)
        thread.start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)
        return web.server_port

    def req(self, port, path, method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        self.addCleanup(conn.close)
        conn.request(method, path, body, headers or {})
        r = conn.getresponse()
        return r.status, r.headers, r.read()

    def test_remote_pair_and_logout(self):
        status, _, payload = self.req(self.local, "/api/state")
        self.assertEqual(status, 200)
        url = urlsplit(json.loads(payload)["settings"]["remote_url"])
        self.assertEqual(self.req(self.gateway, "/api/state")[0], 401)
        self.assertEqual(self.req(self.gateway, "/api/pair", "POST", '{"key":"bad"}', {"Content-Type":"application/json"})[0], 401)
        status, headers, _ = self.req(self.gateway, "/api/pair", "POST", json.dumps({"key":url.fragment}),
                                      {"Content-Type":"application/json", "Origin":self.public})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        status, _, detail = self.req(self.gateway, "/api/episodes/1", headers={"Cookie":cookie})
        self.assertEqual(status, 200)
        self.assertNotIn("audio_path", json.loads(detail))
        self.assertEqual(self.req(self.gateway, "/api/tunnel", "DELETE", headers={"Cookie":cookie})[0], 403)
        status, headers, _ = self.req(self.gateway, "/api/logout", "POST", "{}", {"Cookie":cookie, "Content-Type":"application/json"})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_private_rss_and_media_seek(self):
        self.assertEqual(self.req(self.gateway, "/private/no/feed.xml")[0], 404)
        status, _, data = self.req(self.gateway, f"/private/{self.access.token('feed')}/feed.xml")
        self.assertEqual(status, 200)
        item = ET.fromstring(data).find("channel/item")
        self.assertEqual(item.findtext("title"), "A & B")
        enclosure = item.find("enclosure")
        self.assertEqual(enclosure.get("length"), "256")
        url = urlsplit(enclosure.get("url"))
        self.assertEqual(url.hostname, "aircut.example.org")
        self.assertEqual(self.req(self.gateway, url.path)[0], 401)
        path = url.path + "?" + url.query
        stat, headers, data = self.req(self.gateway, path, "HEAD")
        self.assertEqual((stat, headers["Content-Length"], data), (200, "256", b""))
        for spec, start, end in (("0-1",0,1), ("250-",250,255), ("-2",254,255), ("20-400",20,255)):
            stat, headers, data = self.req(self.gateway, path, headers={"Range":"bytes="+spec})
            self.assertEqual((stat, headers["Content-Range"]), (206, f"bytes {start}-{end}/256"))
            self.assertEqual(data, bytes(range(start, end+1)))
        stat, headers, _ = self.req(self.gateway, path, headers={"Range":"bytes=500-"})
        self.assertEqual((stat, headers["Content-Range"]), (416,"bytes */256"))

    def test_original_is_replaced_in_same_feed_item(self):
        feed = f"/private/{self.access.token('feed')}/feed.xml"
        for status in ("queued", "processing", "failed", "ready"):
            with self.library._db(write=True) as con:
                con.execute("UPDATE episodes SET status=? WHERE id=1", (status,))
            item, = ET.fromstring(self.req(self.gateway, feed)[2]).findall("channel/item")
            identity = item.findtext("guid"), item.findtext("pubDate")
            if status == "queued":
                original_identity = identity
            self.assertEqual(identity, original_identity)
            if status == "ready":
                self.assertEqual(item.findtext("title"), "A & B")
                self.assertEqual(urlsplit(item.find("enclosure").get("url")).path, "/audio/1.mp3")
            else:
                self.assertIn("Original with ads", item.findtext("title"))
                self.assertIn("Ads have not been removed", item.findtext("description"))
                self.assertEqual(item.find("enclosure").get("url"), "https://example.org/fixture.mp3")

    def test_private_rss_artwork(self):
        feed_path = f"/private/{self.access.token('feed')}/feed.xml"
        episode_art = "https://example.org/episode.jpg?size=large&v=2"
        show_art = "https://example.org/show.png"
        image_tag = f"{{{ITUNES}}}image"
        for episode_url, show_url, expected in ((episode_art, show_art, episode_art),
                                                ("", show_art, show_art), ("", "", None)):
            with self.subTest(episode_url=episode_url, show_url=show_url):
                with self.library._db(write=True) as con:
                    con.execute("UPDATE episodes SET image_url=? WHERE id=1", (episode_url,))
                    con.execute("UPDATE feeds SET image_url=? WHERE id=1", (show_url,))
                status, _, data = self.req(self.gateway, feed_path)
                self.assertEqual(status, 200)
                channel = ET.fromstring(data).find("channel")
                self.assertEqual(channel.find(image_tag).get("href"), self.public + "/static/cover.png")
                images = channel.find("item").findall(image_tag)
                self.assertEqual([image.get("href") for image in images], [expected] if expected else [])

    def test_player_changes_are_shared_with_paired_browser(self):
        headers = {"Content-Type": "application/json"}
        remote = dict(headers, Cookie="aircut_session=" + self.access.session())
        saved = "/api/episodes/1/save"
        progress = "/api/episodes/1/progress"
        settings = "/api/player/settings"
        def post(port, path, body, request_headers=headers):
            status, _, response = self.req(port, path, "POST", json.dumps(body), request_headers)
            return status, json.loads(response)
        self.assertEqual(post(self.gateway, saved, {"saved": True})[0], 401)
        self.assertEqual(post(self.local, saved, {"saved": True}), (200, {"is_saved": True}))
        self.assertEqual(post(self.gateway, progress, {"position": 8.5, "revision": 0}, remote),
                         (200, {"playback_position": 8.5, "playback_revision": 1}))
        status, response = post(self.local, progress, {"position": 3, "revision": 0})
        self.assertEqual((status, response["playback_position"], response["playback_revision"]), (409, 8.5, 1))
        skips = {"skip_back_seconds": 20, "skip_forward_seconds": 45}
        self.assertEqual(post(self.gateway, settings, skips, remote), (200, skips))
        status, _, data = self.req(self.local, "/api/state")
        state = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual({key: state["settings"][key] for key in skips}, skips)
        self.assertIs(state["episodes"][0]["is_saved"], True)
        self.assertEqual(state["episodes"][0]["playback_position"], 8.5)
        self.assertEqual(post(self.local, progress, {"position": 0, "revision": 1})[1]["playback_position"], 0)
        self.assertEqual(post(self.local, saved, {"saved": False}), (200, {"is_saved": False}))
        self.assertEqual(post(self.local, "/api/episodes/1001/save", {"saved": True})[0], 404)
        self.assertEqual(post(self.local, settings, {"skip_back_seconds": 30})[0], 400)

    def test_local_host_and_origin(self):
        self.assertEqual(self.req(self.local, "/api/state", headers={"Host":"unrelated.example"})[0], 403)
        self.assertEqual(self.req(self.local, "/api/state", headers={"X-Forwarded-For":"127.0.0.1"})[0], 403)
        self.assertEqual(self.req(self.local, "/api/refresh", "POST", "{}", {"Origin":"https://unrelated.example", "Content-Type":"application/json"})[0], 403)
        self.assertEqual(self.req(self.local, "/api/feeds", "POST", "url=foo", {"Content-Type":"text/plain"})[0], 400)
        self.assertEqual(self.req(self.gateway, "/api/state", headers={"Host":f"localhost:{self.local}"})[0], 401)

    def test_delete_episode_hides_it_from_all_clients_and_feed(self):
        status, _, payload = self.req(self.local, "/api/state")
        self.assertTrue(json.loads(payload)["settings"]["episode_deletion"])
        feed_path = f"/private/{self.access.token('feed')}/feed.xml"
        rss = ET.fromstring(self.req(self.gateway, feed_path)[2])
        media = urlsplit(rss.find("channel/item/enclosure").get("url"))
        url = "/api/episodes/1"
        session = {"Cookie": "aircut_session=" + self.access.session(), "Origin": self.public}
        self.assertEqual(self.req(self.gateway, url, "DELETE")[0], 401)
        self.assertEqual(self.req(self.gateway, url, "DELETE", headers=dict(session, Origin="https://invalid.example"))[0], 403)
        status, _, payload = self.req(self.gateway, url, "DELETE", headers=session)
        self.assertEqual((status, json.loads(payload)), (200, {"ok": True, "cleanup_pending": False}))
        self.assertEqual(self.req(self.local, url, "DELETE")[0], 200)
        self.assertEqual(self.req(self.local, "/api/episodes/999", "DELETE")[0], 404)
        self.assertEqual(self.req(self.gateway, url, headers=session)[0], 404)
        self.assertEqual(self.req(self.gateway, media.path + "?" + media.query)[0], 404)
        self.assertIsNone(ET.fromstring(self.req(self.gateway, feed_path)[2]).find("channel/item"))
        state = json.loads(self.req(self.local, "/api/state")[2])
        self.assertEqual(state["episodes"], [])
        self.assertEqual([f["title"] for f in state["feeds"]], ["Show"])


if __name__ == "__main__":
    unittest.main()
