import concurrent.futures
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from podcast_library import Library, parse_feed


FEED = "https://audio.example/show/feed.xml"


def rss(*numbers):
    items = "".join(
        f"<item><guid>{n}</guid><title>Episode {n}</title>"
        f"<pubDate>2026-01-{n:02}T12:00:00Z</pubDate>"
        f'<enclosure url="audio/{n}.mp3" type="audio/mpeg"/></item>'
        for n in numbers
    )
    return f"<rss><channel><title>Example</title>{items}</channel></rss>".encode()


def make_audio(url, directory, title, progress):
    progress("Transcribing")
    path = directory / "clean.mp3"
    path.write_bytes(b"test audio")
    return dict(audio_path=str(path), transcript="A transcript", takeaways="Notes", summary="A summary",
                ads=[dict(start=10, end=15, reason="Sponsor")], duration=15, original_duration=20)


class FeedTest(unittest.TestCase):
    def test_atom_relative_audio_and_art(self):
        feed = b"""<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://media.example/a/">
        <title>Example</title><logo>art.jpg</logo><entry xml:base="episodes/">
        <id>tag:example,2026:e1</id><title>Talk</title><updated>2026-01-01T12:00:00-05:00</updated>
        <link rel="enclosure" href="video.mp4" type="video/mp4"/>
        <link rel="enclosure" href="talk.mp3" type="audio/mpeg"/><summary>&lt;p&gt;News&lt;/p&gt;</summary>
        </entry></feed>"""
        result = parse_feed(feed, FEED)
        self.assertEqual(result.image_url, "https://media.example/a/art.jpg")
        episode, = result.entries
        self.assertEqual(episode.audio_url, "https://media.example/a/episodes/talk.mp3")
        self.assertEqual(episode.description, "News")
        self.assertEqual(episode.published, "2026-01-01T17:00:00+00:00")

    def test_missing_audio_and_invalid_xml(self):
        invalid_feeds = [
            b"<rss>broken",
            b"<html></html>",
            b"<rss><channel><item><title>Article</title></item></channel></rss>",
            b'<!DOCTYPE rss [<!ENTITY value "some text">]><rss/>',
        ]
        for raw in invalid_feeds:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_feed(raw, FEED)


class LibraryTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.library = Library(Path(folder.name))
        self.addCleanup(self.library.stop)
        download = patch("podcast_library.fetch_feed", return_value=(rss(1, 3, 2, 3), FEED))
        self.fetch = download.start()
        self.addCleanup(download.stop)

    def test_reorder_baseline_retry_and_retention(self):
        lib = self.library
        feed = lib.subscribe(FEED, backfill=1)
        with self.assertRaises(ValueError):
            lib.subscribe(FEED, 1)
        (episode,) = lib.list_episodes()
        self.assertEqual(episode["title"], "Episode 3")
        self.fetch.return_value = rss(2, 4, 3, 1), FEED
        lib._poll_once(force=True)
        lib._poll_once(force=True)
        self.assertEqual([ep["title"] for ep in lib.list_episodes()], ["Episode 4", "Episode 3"])

        with patch("podcast_processing.process_episode", side_effect=RuntimeError("Temporary failure")):
            lib._work_next()
        details = lib.get_episode(episode["id"])
        self.assertEqual((details["status"], details["error"]), ("failed", "Temporary failure"))
        lib.retry(episode["id"])
        with patch("podcast_processing.process_episode", side_effect=make_audio):
            lib._work_next()
        lib.unsubscribe(feed["id"])
        ready, = lib.list_episodes()
        self.assertTrue(ready["has_audio"])
        self.assertEqual(ready["status"], "ready")
        details = lib.get_episode(ready["id"])
        self.assertEqual(details["ads"], [{"start": 10, "end": 15, "reason": "Sponsor"}])
        self.assertEqual(details["original_duration"], 20)
        self.assertNotIn("transcript", lib.list_ready()[0])
        self.assertTrue(Path(lib.list_ready()[0]["audio_path"]).is_file())
        self.assertEqual(lib.list_feeds(), [])
        self.assertEqual(lib.subscribe(FEED, 0)["id"], feed["id"])
        self.assertEqual(len(lib.list_episodes()), 1)

    def test_concurrent_subscriptions_have_one_commit(self):
        barrier = threading.Barrier(2)

        def download(*args, **kwargs):
            barrier.wait(4)
            return rss(1), FEED

        def subscribe():
            try:
                self.library.subscribe(FEED, 1)
            except ValueError:
                return False
            return True

        self.fetch.side_effect = download
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(subscribe) for _ in range(2)]
            self.assertEqual(sum(future.result(timeout=5) for future in results), 1)
        self.assertEqual(len(self.library.list_episodes()), 1)

    def test_player_preferences_and_episode_state_survive_migration_and_restart(self):
        lib = self.library
        feed = lib.subscribe(FEED, backfill=1)
        with patch("podcast_processing.process_episode", side_effect=make_audio):
            lib._work_next()
        (episode,) = lib.list_episodes()
        with lib._db(write=True) as con:
            con.execute("DROP TABLE player_settings")
            for column in ("is_saved", "playback_position", "playback_revision", "worker_active"):
                con.execute(f"ALTER TABLE episodes DROP COLUMN {column}")
        migrated = Library(lib.data_dir)
        self.assertEqual(migrated.player_settings(), {"skip_back_seconds": 15, "skip_forward_seconds": 30})
        current = migrated.get_episode(episode["id"])
        self.assertEqual((current["title"], current["status"]), (episode["title"], "ready"))
        self.assertIs(current["is_saved"], False)
        self.assertEqual(current["playback_position"], 0)
        migrated.set_saved(episode["id"], True)
        migrated.set_progress(episode["id"], 400)
        migrated.set_player_settings(1, 300)
        migrated.unsubscribe(feed["id"])
        reopened = Library(lib.data_dir)
        ready = reopened.list_episodes()[0]
        self.assertIs(ready["is_saved"], True)
        self.assertEqual(ready["playback_position"], ready["duration"])
        self.assertEqual(reopened.player_settings(), {"skip_back_seconds": 1, "skip_forward_seconds": 300})
        reopened.set_progress(ready["id"], 4.75)
        self.assertEqual(reopened.get_episode(ready["id"])["playback_position"], 4.75)

    def test_invalid_player_values_and_unplayable_episode(self):
        lib = self.library
        lib.subscribe(FEED, backfill=1)
        (episode,) = lib.list_episodes()
        for value in (0, -1, False, True, 301, 1.5, "20", None):
            with self.subTest(skip=value), self.assertRaises(ValueError):
                lib.set_player_settings(15, value)
        self.assertRaises(ValueError, lib.set_saved, episode["id"], 1)
        self.assertRaises(ValueError, lib.set_progress, episode["id"], 2)
        for value in (-1, False, "3", None, float("inf"), float("nan")):
            with self.subTest(position=value), self.assertRaisesRegex(ValueError, "nonnegative"):
                lib.set_progress(episode["id"], value)
        with lib._db() as con:
            skipped = con.execute("SELECT id FROM episodes WHERE status='skipped' LIMIT 1").fetchone()["id"]
        self.assertRaises(KeyError, lib.set_saved, skipped, True)
        self.assertRaises(KeyError, lib.set_progress, skipped, 3)

    def test_new_only_and_old_archive(self):
        self.fetch.return_value = rss(10), FEED
        self.library.subscribe(FEED, 0)
        self.fetch.return_value = rss(1, 10, 11), FEED
        self.library._poll_once(force=True)
        self.assertEqual([ep["title"] for ep in self.library.list_episodes()], ["Episode 11"])

    def test_delete_removes_files_and_notes_and_keeps_identity_across_feed_checks(self):
        lib = self.library
        self.fetch.return_value = rss(1, 2), FEED
        feed = lib.subscribe(FEED, 2)
        with patch("podcast_processing.process_episode", side_effect=make_audio):
            lib._work_next()
            lib._work_next()
        episode, other = lib.list_ready()
        target = episode["id"]
        self.assertGreater(target, other["id"])
        directory = Path(episode["audio_path"]).parent
        (directory / "transcript-cache").write_text("file to remove")
        sentinel = Path(other["audio_path"])
        (directory / "linked-file").symlink_to(sentinel)
        lib.set_saved(target, True)
        lib.set_progress(target, 7)
        self.assertEqual(lib.delete_episode(target), {"ok": True, "cleanup_pending": False})
        self.assertFalse(directory.exists())
        self.assertTrue(sentinel.is_file())
        self.assertEqual([ep["id"] for ep in lib.list_episodes()], [other["id"]])
        self.assertEqual([ep["id"] for ep in lib.list_ready()], [other["id"]])
        self.assertRaises(KeyError, lib.get_episode, target)
        self.assertRaises(KeyError, lib.set_progress, target, 0)
        self.assertRaises(KeyError, lib.set_saved, target, True)
        self.assertRaises(KeyError, lib.retry, target)
        self.assertEqual([f["id"] for f in lib.list_feeds()], [feed["id"]])
        with lib._db() as db:
            row = db.execute("SELECT transcript,summary,takeaways,ads,audio_path,is_saved,playback_position FROM episodes WHERE id=?", (target,)).fetchone()
            self.assertEqual(tuple(row), ("", "", "", "[]", "", 0, 0))
        lib = Library(lib.data_dir)
        self.assertFalse(lib.delete_episode(target)["cleanup_pending"])
        lib.unsubscribe(feed["id"])
        # Publisher changes the enclosure, then changes the GUID but keeps the new enclosure.
        moved = rss(1, 2, 3).replace(b'audio/2.mp3', b'audio/moved.mp3')
        self.fetch.return_value = moved, FEED
        lib.subscribe(FEED, 3)
        self.fetch.return_value = moved.replace(b"<guid>2</guid>", b"<guid>new-id</guid>"), FEED
        lib._poll_once(force=True)
        episodes = lib.list_episodes()
        self.assertEqual([ep["title"] for ep in episodes], ["Episode 3", "Episode 1"])
        self.assertGreater(episodes[0]["id"], target)
        # A different subscription can independently publish the same GUID and enclosure.
        different = lib.subscribe("https://different.example/show", 3)
        self.assertIn("Episode 2", [ep["title"] for ep in lib.list_episodes() if ep["feed_id"] == different["id"]])

    def test_delete_during_processing_waits_for_writer_before_removing_files(self):
        lib = self.library
        feed = lib.subscribe(FEED, 1)
        (episode,) = lib.list_episodes()
        episode_id = episode["id"]
        other_session = Library(lib.data_dir)
        finished = threading.Event()
        def finish_old_job(url, directory, title, progress):
            result = make_audio(url, directory, title, progress)
            # Even when the active row is requeued, its old writer still owns this directory.
            other_session.unsubscribe(feed["id"])
            other_session.subscribe(FEED, 1)
            self.assertTrue(other_session.delete_episode(episode_id)["cleanup_pending"])
            self.assertTrue(other_session.delete_episode(episode_id)["cleanup_pending"])
            self.assertEqual(other_session.list_episodes(), [])
            (directory / "last-checkpoint").write_text("late write")
            finished.set()
            return result
        with patch("podcast_processing.process_episode", side_effect=finish_old_job):
            self.assertTrue(lib._work_next())
        self.assertTrue(finished.is_set())
        self.assertEqual(lib.list_ready(), [])
        self.assertEqual(lib.list_episodes(), [])
        self.assertFalse((lib.data_dir / "episodes" / str(episode_id)).exists())
        self.assertFalse(lib.delete_episode(episode_id)["cleanup_pending"])
        self.assertFalse(lib._work_next())

    def test_delete_retries_filesystem_failure_after_restart(self):
        lib = self.library
        lib.subscribe(FEED, 1)
        with patch("podcast_processing.process_episode", side_effect=make_audio):
            lib._work_next()
        episode, = lib.list_ready()
        path = Path(episode["audio_path"])
        with patch("podcast_library.shutil.rmtree", side_effect=PermissionError("busy")), self.assertLogs("podcast_library"):
            self.assertTrue(lib.delete_episode(episode["id"])["cleanup_pending"])
        self.assertTrue(path.is_file())
        self.assertEqual(lib.list_ready(), [])
        reopened = Library(lib.data_dir)
        self.assertFalse(reopened._work_next())
        self.assertFalse(path.parent.exists())
        self.assertFalse(reopened.delete_episode(episode["id"])["cleanup_pending"])

    def test_relative_enclosure_uses_redirect_destination(self):
        self.fetch.return_value = rss(1), "https://publisher.example/moved/feed.xml"
        self.library.subscribe(FEED, 1)
        with self.library._db() as con:
            url, = con.execute("SELECT audio_url FROM episodes").fetchone()
        self.assertEqual(url, "https://publisher.example/moved/audio/1.mp3")
        self.assertEqual(self.library.list_feeds()[0]["url"], FEED)

    def test_unsubscribe_during_job_cannot_publish_old_result(self):
        lib = self.library
        feed = lib.subscribe(FEED, 1)

        def work(url, directory, title, progress):
            output = make_audio(url, directory, title, progress)
            lib.unsubscribe(feed["id"])
            lib.subscribe(FEED, 1)
            return output

        with patch("podcast_processing.process_episode", side_effect=work):
            lib._work_next()
        self.assertEqual([ep["status"] for ep in lib.list_episodes()], ["queued"])
        self.assertFalse(lib.list_ready())

    def test_interrupted_job_recovered_on_start(self):
        lib = self.library
        lib.subscribe(FEED, 1)
        with lib._db(write=True) as con:
            con.execute("UPDATE episodes SET status='processing' WHERE status='queued'")
        ran = threading.Event()

        def work(url, directory, title, progress):
            out = make_audio(url, directory, title, progress)
            ran.set()
            return out

        with patch("podcast_processing.process_episode", side_effect=work):
            lib.start()
            self.assertTrue(ran.wait(4))
            second = Library(lib.data_dir)
            with self.assertRaises(RuntimeError):
                second.start()
            lib.stop()
        self.assertEqual(len(lib.list_ready()), 1)


if __name__ == "__main__":
    unittest.main()
