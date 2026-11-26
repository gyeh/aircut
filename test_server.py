"""HTTP integration tests for the iPhone gateway and seekable podcast feed."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from aircut import Access, ITUNES, WebServer
from podcast_library import Library
from podcast_tailnet import verified_serve_origin


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
        self.access.gateway_port = self.gateway
        notes = patch("aircut.verified_serve_origin", return_value="")
        self.notes_origin = notes.start()
        self.addCleanup(notes.stop)

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
        self.assertEqual(item.findtext("title"), "🍊 A & B")
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
                self.assertEqual(item.findtext("title"), "🍊 A & B")
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

    def test_show_feeds_and_text_directory(self):
        art = "https://example.org/show.jpg"
        with self.library._db(write=True) as con:
            con.execute("UPDATE feeds SET image_url=? WHERE id=1", (art,))
            con.execute("""INSERT INTO feeds(id,url,title,image_url,baseline,last_checked)
                           VALUES(2,'https://example.org/other','Other & Co.','','','')""")
        state = json.loads(self.req(self.local, "/api/state")[2])
        combined = state["settings"]["rss_url"]
        other, show = (feed["rss_url"] for feed in state["feeds"])
        self.assertEqual(self.req(self.gateway, "/feeds.txt")[0], 401)
        paired = {"Cookie": "aircut_session=" + self.access.session()}
        for port, headers in ((self.local, {}), (self.gateway, paired)):
            status, response, data = self.req(port, "/feeds.txt", headers=headers)
            self.assertEqual((status, response.get_content_type()), (200, "text/plain"))
            self.assertEqual(data.decode().splitlines(), [combined, other, show])

        def channel(url):
            self.assertTrue(url.startswith(self.public + "/"))
            status, headers, data = self.req(self.gateway, urlsplit(url).path)
            self.assertEqual((status, headers.get_content_type()), (200, "application/rss+xml"))
            return ET.fromstring(data).find("channel")

        self.assertEqual(channel(other).findtext("title"), "Other & Co.")
        self.assertIsNone(channel(other).find("item"))
        single = channel(show)
        self.assertEqual(single.findtext("title"), "Show")
        self.assertEqual(single.find(f"{{{ITUNES}}}image").get("href"), art)
        self.assertEqual(ET.tostring(single.find("item")), ET.tostring(channel(combined).find("item")))
        self.assertEqual(self.req(self.gateway, urlsplit(show).path.replace("/1.xml", "/2.xml"))[0], 404)

        self.library.unsubscribe(1)
        self.assertEqual(self.req(self.gateway, urlsplit(show).path)[0], 404)
        self.assertEqual(self.req(self.local, "/feeds.txt")[2].decode().splitlines(), [combined, other])
        self.assertEqual(channel(combined).findtext("item/title"), "🍊 A & B")

    def test_notes_from_published_feeds(self):
        origin = "https://node.example.ts.net"
        for path in (self.access.feed_path(), self.access.feed_path(1)):
            self.notes_origin.return_value = ""
            item = ET.fromstring(self.req(self.gateway, path)[2]).find("channel/item")
            self.assertIsNone(item.find("link"))
            self.assertNotIn("/notes", item.findtext("description"))
            self.notes_origin.return_value = origin
            linked = ET.fromstring(self.req(self.gateway, path)[2]).find("channel/item")
            self.assertEqual(linked.findtext("guid"), item.findtext("guid"))
            self.assertEqual(linked.find("enclosure").attrib, item.find("enclosure").attrib)
            self.assertEqual(linked.findtext("title"), "🍊 A & B")
            self.assertEqual(linked.findtext("link"), origin + "/episodes/1/notes")
            self.assertIn(f'href="{linked.findtext("link")}"', linked.findtext("description"))
            self.assertIn("&lt;notes&gt;", linked.findtext("description"))
        path = urlsplit(linked.findtext("link")).path
        with self.library._db(write=True) as con:
            ads = [{"start": 2.5, "end": 10.9, "reason": "Offer from <Sponsor>", "advertisers": ["A&B"]},
                   {"start": 3600, "end": 3661, "reason": "Paid promotion", "advertisers": ["a&b", "Other"]}]
            con.execute("UPDATE episodes SET transcript='[00:04] <sample>', original_duration=3700, ads=? WHERE id=1",
                        (json.dumps(ads),))
        headers = {"X-Forwarded-Host": origin[8:], "X-Forwarded-Proto": "https",
                   "Tailscale-User-Login": "reader", "X-Forwarded-For": "100.64.0.1"}
        self.assertEqual(self.req(self.local, path)[0], 403)
        with patch("podcast_tailnet._serve_owns_connection", return_value=False):
            self.assertEqual(self.req(self.gateway, path, headers=headers)[0], 403)
        with patch("podcast_tailnet._serve_owns_connection", return_value=True), \
                patch("podcast_tailnet.verified_serve_origin", return_value=origin) as verify:
            status, response, html = self.req(self.gateway, path, headers=headers)
            self.assertEqual((status, response.get_content_type()), (200, "text/html"))
            for content in (b"More", b"Some &lt;notes&gt;", b"[00:04] &lt;sample&gt;", b"viewport"):
                self.assertIn(content, html)
            notes, removed = html.decode().split('<details class="notes removed-ads"><summary>Removed ads')
            self.assertIn("<details><summary>Transcript</summary>", notes)
            self.assertIn("00:01:09 (1.9% of the original 01:01:40)", removed)
            self.assertIn("A&amp;B, Other", removed)
            detail = json.loads(self.req(self.local, "/api/episodes/1")[2])
            self.assertIn(detail["removed_ads_html"], html.decode())
            self.assertIn("00:00:02–00:00:10", removed)
            self.assertIn("Offer from &lt;Sponsor&gt;", removed)
            self.assertIn("01:00:00–01:01:01", removed)
            self.assertIn("Paid promotion", removed)
            with self.library._db(write=True) as con:
                con.execute("UPDATE episodes SET ads='[]' WHERE id=1")
            self.assertIn(b"No ad breaks were removed.", self.req(self.gateway, path, headers=headers)[2])
            with self.library._db(write=True) as con:
                con.execute("UPDATE episodes SET status='queued' WHERE id=1")
            self.assertNotIn(b"<summary>Removed ads", self.req(self.gateway, path, headers=headers)[2])
            self.assertEqual(self.req(self.gateway, path, headers=dict(headers, **{"Tailscale-Funnel-Request":"?1"}))[0], 403)
            self.assertEqual(self.req(self.gateway, "/episodes/999/notes", headers=headers)[0], 404)
            verify.return_value = ""
            self.assertEqual(self.req(self.gateway, path, "HEAD", headers=headers)[0], 403)

    def test_briefing_is_formatted_once_on_reading_surfaces(self):
        summary = ("## The Big Idea\n\nA <claim>.\n\n## Deep Dives by Theme\n\n### Who pays?\n\nTwo groups."
                   "\n\n## Nuance, Pushback & Open Questions\n\nCosts vary.\n\n## Notable Quotes & References"
                   "\n\n> A <quote>.\n\n## Lasting Takeaways\n\n- Compare costs.")
        with self.library._db(write=True) as con:
            con.execute("UPDATE episodes SET summary=?,takeaways=? WHERE id=1", (summary, "- Compare costs."))
        detail = json.loads(self.req(self.local, "/api/episodes/1")[2])
        html = detail["briefing_html"]
        for part in ("<h2>The Big Idea</h2>", "<h3>Who pays?</h3>", "<p>A &lt;claim&gt;.</p>",
                     "<blockquote>A &lt;quote&gt;.</blockquote>", "<li>Compare costs.</li>"):
            self.assertIn(part, html)
        with patch("aircut.private_page_allowed", return_value=True):
            page = self.req(self.gateway, "/episodes/1/notes")[2].decode()
            self.assertIn(html, page)
            self.assertEqual(page.count("Compare costs."), 1)
        for origin in ("", "https://node.example.ts.net"):
            self.notes_origin.return_value = origin
            for path in (self.access.feed_path(), self.access.feed_path(1)):
                description = ET.fromstring(self.req(self.gateway, path)[2]).findtext("channel/item/description")
                self.assertIn(html, description)
                self.assertEqual(description.count("Compare costs."), 1)

    def test_ad_stats_weights_runtime_and_counts_distinct_episodes(self):
        self.assertEqual(self.req(self.gateway, "/api/ad-stats")[0], 401)
        with self.library._db(write=True) as con:
            ads = [{"start": 10, "end": 30, "advertisers": ["ACME", "Other"]},
                   {"start": 25, "end": 35, "advertisers": [" acme "]}]
            con.execute("UPDATE episodes SET original_duration=100,ads=? WHERE id=1", (json.dumps(ads),))
            con.execute("""INSERT INTO feeds(id,url,title,image_url,baseline,last_checked)
                        VALUES(2,'https://example.org/two','Two','','','')""")
            for key, feed_id, duration, status, cuts in (
                    ("two", 1, 150, "ready", []), ("three", 2, 50, "ready", [{"start": 35, "end": 60, "advertisers": ["acme"]}]),
                    ("four", 2, 0, "ready", [{"start": 0, "end": 5}]), ("pending", 1, 400, "queued", ads)):
                con.execute("""INSERT INTO episodes(feed_id,episode_key,audio_url,title,description,published,
                            image_url,discovered,status,original_duration,ads)
                            VALUES(?,?,'','','','','','',?,?,?)""", (feed_id, key, status, duration, json.dumps(cuts)))
        paired = {"Cookie": "aircut_session=" + self.access.session()}
        status, _, data = self.req(self.gateway, "/api/ad-stats", headers=paired)
        self.assertEqual(status, 200)
        stats = json.loads(data)
        self.assertEqual((stats["episodes"], stats["removed_seconds"]), (4, 45))
        self.assertEqual([(s["title"], s["percent"]) for s in stats["podcasts"]], [("Two", 30), ("Show", 10)])
        self.assertEqual(stats["podcasts"][0]["missing_duration"], 1)
        self.assertEqual(stats["advertisers"], [{"name":"ACME","episodes":2}, {"name":"Other","episodes":1}])
        with self.library._db(write=True) as con:
            con.execute("UPDATE episodes SET status='deleted'")
        self.assertEqual(json.loads(self.req(self.local, "/api/ad-stats")[2]),
                         {"episodes": 0, "removed_seconds": 0, "podcasts": [], "advertisers": []})

    def test_empty_directory_uses_local_combined_url(self):
        self.library.unsubscribe(1)
        self.access.url = ""
        urls = self.req(self.local, "/feeds.txt")[2].decode().splitlines()
        self.assertEqual(urls, [f"http://127.0.0.1:8765/private/{self.access.token('feed')}/feed.xml"])

    def test_private_serve_requires_verified_tailnet_and_exclusive_route(self):
        host = "node.example.ts.net"
        node = {"DNSName": host + ".", "Online": True, "TailscaleIPs": ["100.64.0.1"]}
        status = {"Self": node, "BackendState": "Running", "CurrentTailnet": {"MagicDNSSuffix": "example.ts.net"}}
        config = {"TCP": {"443": {"HTTPS": True}}, "Web": {host + ":443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}}}
        for conf, port, source, allowed in (
                (config, 8766, node["TailscaleIPs"][0], True), (config, 8765, None, False),
                (config, 8766, "100.64.0.2", False), ({}, 8766, None, False),
                (dict(config, AllowFunnel={host + ":443": True}), 8766, None, False),
                (dict(config, Foreground={"other": {}}), 8766, None, False)):
            with patch("podcast_tailnet._output", side_effect=[json.dumps(status), json.dumps(conf)]):
                self.assertEqual(bool(verified_serve_origin(port, source)), allowed)
        with patch("podcast_tailnet._output", side_effect=OSError):
            self.assertEqual(verified_serve_origin(8766), "")

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

    def test_automatic_retention_removes_episode_from_both_feeds_and_audio(self):
        lib = self.library
        lib.retention_months = 2
        paths = [self.access.feed_path(), self.access.feed_path(1)]
        for path in paths:
            self.assertIsNotNone(ET.fromstring(self.req(self.gateway, path)[2]).find("channel/item"))
        self.assertEqual(self.req(self.local, "/audio/1.mp3")[0], 200)
        self.addCleanup(lib.stop)
        with patch.object(lib, "_poll_once"):
            lib.start()
            self.assertEqual(self.req(self.local, "/api/episodes/1")[0], 404)
            self.assertEqual(self.req(self.local, "/audio/1.mp3")[0], 404)
            for path in paths:
                self.assertIsNone(ET.fromstring(self.req(self.gateway, path)[2]).find("channel/item"))
            state = json.loads(self.req(self.local, "/api/state")[2])
            self.assertEqual(state["settings"]["retention_months"], 2)
            self.assertEqual(state["episodes"], [])
            self.assertEqual([f["title"] for f in state["feeds"]], ["Show"])
            lib.stop()

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
