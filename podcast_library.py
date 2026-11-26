"""Persistent podcast subscriptions, feed snapshots, and a single audio worker."""

from __future__ import annotations

from calendar import monthrange
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
from html.parser import HTMLParser
import json
import logging
import math
from pathlib import Path
import shutil
import sqlite3
import stat
import threading
import time
from urllib.parse import urldefrag, urljoin, urlsplit
import xml.etree.ElementTree as ET

from podcast_network import fetch_feed


LOG = logging.getLogger(__name__)
# Long-running daily shows can exceed 12 MiB of feed XML.
MAX_FEED_BYTES = 16 * 1024 * 1024
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _retention_cutoff(now, months):
    month_index = now.year * 12 + now.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    return now.replace(year=year, month=month, day=min(now.day, monthrange(year, month)[1]))


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _child(element, *names):
    return next((node for node in element if _local(node.tag) in names), None)


def _content(node):
    return "".join(node.itertext()).strip() if node is not None else ""


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pieces = []

    def handle_data(self, data):
        self.pieces.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.pieces.append(" ")


def _plain(value, limit):
    if "<" in value or "&" in value:
        parser = _PlainText()
        try:
            parser.feed(value)
            parser.close()
            value = "".join(parser.pieces)
        except ValueError:
            pass
    return " ".join(value.split())[:limit]


def _web_url(value, base=""):
    if not value.strip():
        return ""
    try:
        url = urldefrag(urljoin(base, value.strip())).url
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.hostname and parts.username is None and parts.password is None:
            return url
    except ValueError:
        pass
    return ""


def _published(value):
    for parse in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            dt = parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (ValueError, OverflowError, TypeError):
            continue
    return ""


def _art(element, base):
    for node in element:
        name = _local(node.tag)
        value = ""
        if name == "image":
            value = node.get("href") or node.get("url") or _content(_child(node, "url"))
        elif name in {"logo", "icon"}:
            value = _content(node)
        elif name == "thumbnail":
            value = node.get("url", "")
        image = _web_url(value, urljoin(base, node.get(XML_BASE, "")))
        if image:
            return image
    return ""


def _enclosure(entry, base):
    candidates = []
    for node in entry.iter():
        name = _local(node.tag)
        media_content = name == "content" and node.tag.startswith("{http://search.yahoo.com/mrss/")
        if not (name == "enclosure" or (name == "link" and node.get("rel", "").lower() == "enclosure") or media_content):
            continue
        mime = node.get("type", "").lower().split(";", 1)[0].strip()
        medium = node.get("medium", "").lower()
        audio = mime.startswith("audio/") or mime == "application/ogg" or medium == "audio"
        if not audio and (mime not in {"", "application/octet-stream", "binary/octet-stream"} or medium):
            continue
        url = _web_url(node.get("href") or node.get("url") or "", urljoin(base, node.get(XML_BASE, "")))
        if url:
            candidates.append((audio, url))
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else ""


@dataclass(frozen=True)
class Entry:
    key: str
    audio_url: str
    title: str
    description: str
    published: str
    image_url: str


@dataclass(frozen=True)
class Snapshot:
    title: str
    image_url: str
    entries: list[Entry]


def parse_feed(raw, url):
    # Feed formats do not need DTDs. Reject declarations before expanding entities.
    if b"<!DOCTYPE" in raw.replace(b"\x00", b"").upper():
        raise ValueError("The feed contains an unsupported XML document type declaration.")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError, LookupError) as exc:
        raise ValueError("The address did not return a valid RSS or Atom XML feed.") from exc
    root_name = _local(root.tag)
    base = urljoin(url, root.get(XML_BASE, ""))
    if root_name == "feed":
        channel = root
        nodes = [node for node in root if _local(node.tag) == "entry"]
    elif root_name in {"rss", "rdf"}:
        channel = _child(root, "channel")
        if channel is None:
            raise ValueError("The RSS feed is missing its channel.")
        base = urljoin(base, channel.get(XML_BASE, ""))
        nodes = [node for node in (root if root_name == "rdf" else channel) if _local(node.tag) == "item"]
    else:
        raise ValueError("Enter the URL of a podcast RSS or Atom feed.")
    title = _plain(_content(_child(channel, "title")), 1000) or urlsplit(url).hostname or "Podcast"
    entries = []
    keys, urls = set(), set()
    for node in nodes:
        entry_base = urljoin(base, node.get(XML_BASE, ""))
        audio = _enclosure(node, entry_base)
        if not audio:
            continue
        guid = _content(_child(node, "guid", "id"))
        key = "id:" + guid if guid else "url:" + audio
        if key in keys or audio in urls:
            continue
        keys.add(key)
        urls.add(audio)
        published = _content(_child(node, "pubdate", "published", "date")) or _content(_child(node, "updated"))
        description = _content(_child(node, "description", "summary", "encoded", "subtitle"))
        entries.append(Entry(key, audio, _plain(_content(_child(node, "title")), 1000) or "Untitled episode",
                             _plain(description, 20_000), _published(published), _art(node, entry_base)))
    if nodes and not entries:
        raise ValueError("The feed contains entries, but none has a supported audio enclosure.")
    return Snapshot(title, _art(channel, base), entries)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, image_url TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
    baseline TEXT NOT NULL, last_checked TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY, feed_id INTEGER NOT NULL REFERENCES feeds(id),
    episode_key TEXT NOT NULL, audio_url TEXT NOT NULL, title TEXT NOT NULL,
    description TEXT NOT NULL, published TEXT NOT NULL, image_url TEXT NOT NULL, discovered TEXT NOT NULL,
    status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '', takeaways TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
    ads TEXT NOT NULL DEFAULT '[]', duration REAL NOT NULL DEFAULT 0, original_duration REAL NOT NULL DEFAULT 0,
    audio_path TEXT NOT NULL DEFAULT '', is_saved INTEGER NOT NULL DEFAULT 0,
    playback_position REAL NOT NULL DEFAULT 0, playback_revision INTEGER NOT NULL DEFAULT 0,
    worker_active INTEGER NOT NULL DEFAULT 0,
    UNIQUE(feed_id, episode_key)
);
CREATE INDEX IF NOT EXISTS episode_queue ON episodes(status,id);
CREATE TABLE IF NOT EXISTS deleted_episode_identities (
    feed_id INTEGER NOT NULL REFERENCES feeds(id), identity TEXT NOT NULL,
    PRIMARY KEY(feed_id,identity)
);
CREATE TABLE IF NOT EXISTS player_settings (
    id INTEGER PRIMARY KEY CHECK (id=1),
    skip_back_seconds INTEGER NOT NULL CHECK (skip_back_seconds BETWEEN 1 AND 300),
    skip_forward_seconds INTEGER NOT NULL CHECK (skip_forward_seconds BETWEEN 1 AND 300)
);
INSERT OR IGNORE INTO player_settings(id,skip_back_seconds,skip_forward_seconds) VALUES(1,15,30);
"""
_FEED_FIELDS = "id,url,title,image_url,last_checked,last_error"
_EPISODE_FIELDS = """e.id,e.feed_id,f.title AS feed_title,e.title,e.description,e.published,
    COALESCE(NULLIF(e.image_url,''),f.image_url) AS image_url,
    e.status,e.stage,e.error,e.duration,e.original_duration,e.is_saved,e.playback_position,e.playback_revision,
    (e.status='ready' AND e.audio_path!='') AS has_audio"""


def _episode_dict(row):
    return dict(row, has_audio=bool(row["has_audio"]), is_saved=bool(row["is_saved"]))


def episode_ad_totals(ads, original_duration):
    """Count overlapping cut time once and each named advertiser once per episode."""
    seconds = previous_end = 0.0
    brands = {}
    for ad in sorted(ads, key=lambda ad: ad["start"]):
        end = min(ad["end"], original_duration) if original_duration > 0 else ad["end"]
        seconds += max(0, end - max(ad["start"], previous_end))
        previous_end = max(previous_end, end)
        for name in ad.get("advertisers", []):
            name = name.strip()
            if name:
                brands.setdefault(name.casefold(), name)
    return seconds, brands


class PlaybackConflict(Exception):
    def __init__(self, position, revision):
        super().__init__("A newer playback position has already been saved.")
        self.position = position
        self.revision = revision


class _Interrupted(Exception):
    pass


class Library:
    def __init__(self, data_dir: Path, poll_minutes: int = 30, retention_months: int = 0):
        if poll_minutes <= 0:
            raise ValueError("The feed polling interval must be positive.")
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.poll_minutes = poll_minutes
        if type(retention_months) is not int or not 0 <= retention_months <= 120:
            raise ValueError("Episode retention must be between 0 and 120 months.")
        self.retention_months = retention_months
        self.db_path = self.data_dir / "library.sqlite3"
        self._stop = threading.Event()
        self._poll_wake = threading.Event()
        self._work_wake = threading.Event()
        self._threads = []
        self._owner = None
        with self._db() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)
        with self._db(write=True) as con:
            columns = {r["name"] for r in con.execute("PRAGMA table_info(episodes)")}
            for name, sql_type in (("original_duration", "REAL"), ("is_saved", "INTEGER"),
                                   ("playback_position", "REAL"), ("playback_revision", "INTEGER"),
                                   ("worker_active", "INTEGER")):
                if name not in columns:
                    con.execute(f"ALTER TABLE episodes ADD COLUMN {name} {sql_type} NOT NULL DEFAULT 0")

    @contextmanager
    def _db(self, write=False):
        con = sqlite3.connect(self.db_path, timeout=15)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            with con:
                if write:
                    con.execute("BEGIN IMMEDIATE")
                yield con
        finally:
            con.close()

    def start(self):
        if any(thread.is_alive() for thread in self._threads):
            if self._stop.is_set():
                raise RuntimeError("The previous workers are still stopping.")
            return
        if self._owner is None:
            owner = (self.data_dir / "worker.lock").open("a")
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                owner.close()
                raise RuntimeError("Another podcast server already owns this library.") from exc
            self._owner = owner
        with self._db(write=True) as con:
            con.execute("UPDATE episodes SET status='queued',stage='Waiting to resume' WHERE status='processing'")
            con.execute("UPDATE episodes SET worker_active=0 WHERE worker_active=1")
        self._stop.clear()
        workers = [("feed-poller", self._poll_loop), ("episode-worker", self._work_loop)]
        if self.retention_months:
            self._expire_episodes()
            workers.append(("episode-retention", self._retention_loop))
        self._threads = [threading.Thread(name=name, target=run, daemon=True) for name, run in workers]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self._stop.set()
        self._poll_wake.set()
        self._work_wake.set()
        for thread in self._threads:
            thread.join(timeout=1)
        if self._owner is not None and all(not thread.is_alive() for thread in self._threads):
            self._owner.close()
            self._owner = None

    def list_feeds(self):
        with self._db() as con:
            return [dict(row) for row in con.execute(f"SELECT {_FEED_FIELDS} FROM feeds WHERE active=1 ORDER BY title COLLATE NOCASE,id")]

    def get_active_feed(self, feed_id):
        with self._db() as con:
            row = con.execute(f"SELECT {_FEED_FIELDS} FROM feeds WHERE id=? AND active=1", (feed_id,)).fetchone()
        if row is None:
            raise KeyError("Subscription not found.")
        return dict(row)

    def player_settings(self):
        with self._db() as con:
            return dict(con.execute("SELECT skip_back_seconds,skip_forward_seconds FROM player_settings WHERE id=1").fetchone())

    def set_player_settings(self, back, forward):
        if any(type(value) is not int or not 1 <= value <= 300 for value in (back, forward)):
            raise ValueError("Choose a whole number from 1 to 300 seconds for each skip button.")
        with self._db(write=True) as con:
            con.execute("UPDATE player_settings SET skip_back_seconds=?,skip_forward_seconds=? WHERE id=1", (back, forward))
        return {"skip_back_seconds": back, "skip_forward_seconds": forward}

    def set_saved(self, episode_id, saved):
        if type(saved) is not bool:
            raise ValueError("The saved flag must be true or false.")
        with self._db(write=True) as con:
            changed = con.execute("""UPDATE episodes SET is_saved=? WHERE id=?
                                  AND status NOT IN ('skipped','deleting','deleted')""", (saved, episode_id))
            if not changed.rowcount:
                raise KeyError("Episode not found.")
        return {"is_saved": saved}

    def set_progress(self, episode_id, position, revision=None):
        if (type(position) not in (int, float) or position < 0 or
                (type(position) is float and not math.isfinite(position))):
            raise ValueError("Playback position must be a finite, nonnegative number of seconds.")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("Playback revision must be a nonnegative integer.")
        with self._db(write=True) as con:
            row = con.execute("""SELECT status,audio_path,duration,playback_position,playback_revision FROM episodes
                                 WHERE id=? AND status NOT IN ('skipped','deleting','deleted')""", (episode_id,)).fetchone()
            if row is None:
                raise KeyError("Episode not found.")
            if row["status"] != "ready" or row["duration"] <= 0 or not Path(row["audio_path"]).is_file():
                raise ValueError("The episode's cleaned audio is unavailable.")
            position = float(min(position, row["duration"]))
            current_revision = row["playback_revision"]
            if position != row["playback_position"]:
                if revision is not None and revision != current_revision:
                    raise PlaybackConflict(row["playback_position"], current_revision)
                current_revision += 1
                con.execute("UPDATE episodes SET playback_position=?,playback_revision=? WHERE id=?",
                            (position, current_revision, episode_id))
        return {"playback_position": position, "playback_revision": current_revision}

    def list_episodes(self):
        with self._db() as con:
            rows = con.execute(f"""SELECT {_EPISODE_FIELDS}
                FROM episodes e JOIN feeds f ON f.id=e.feed_id WHERE e.status NOT IN ('skipped','deleting','deleted')
                ORDER BY COALESCE(NULLIF(e.published,''),e.discovered) DESC,e.id DESC""")
            return [_episode_dict(row) for row in rows]

    def get_episode(self, episode_id):
        with self._db() as con:
            row = con.execute(f"""SELECT {_EPISODE_FIELDS},e.transcript,e.takeaways,e.summary,e.ads,e.audio_path
                FROM episodes e JOIN feeds f ON f.id=e.feed_id WHERE e.id=?
                AND e.status NOT IN ('skipped','deleting','deleted')""",
                              (episode_id,)).fetchone()
        if row is None:
            raise KeyError("Episode not found.")
        return dict(_episode_dict(row), ads=json.loads(row["ads"]))

    def list_ready(self):
        return self._list_for_feed(("ready",))

    def ad_stats(self):
        shows = {}
        advertisers = {}
        with self._db() as con:
            rows = con.execute("""SELECT e.feed_id, f.title, e.original_duration, e.ads
                FROM episodes e JOIN feeds f ON e.feed_id=f.id WHERE e.status='ready' ORDER BY e.id""")
            for row in rows:
                duration = row["original_duration"]
                removed, brands = episode_ad_totals(json.loads(row["ads"]), duration)
                show = shows.setdefault(row["feed_id"], {
                    "id": row["feed_id"], "title": row["title"], "episodes": 0, "removed_seconds": 0.0,
                    "original_seconds": 0.0, "timed_removed_seconds": 0.0, "missing_duration": 0,
                })
                show["episodes"] += 1
                show["removed_seconds"] += removed
                if duration > 0:
                    show["original_seconds"] += duration
                    show["timed_removed_seconds"] += removed
                else:
                    show["missing_duration"] += 1
                for key, name in brands.items():
                    advertiser = advertisers.setdefault(key, {"name": name, "episodes": 0})
                    advertiser["episodes"] += 1

        for show in shows.values():
            original = show["original_seconds"]
            removed = show.pop("timed_removed_seconds")
            show["percent"] = 100 * removed / original if original else None
        podcasts = sorted(shows.values(), key=lambda s: (
            s["percent"] is None, -(s["percent"] or 0), s["title"].casefold(), s["id"]))
        return {
            "episodes": sum(s["episodes"] for s in podcasts),
            "removed_seconds": sum(s["removed_seconds"] for s in podcasts),
            "podcasts": podcasts,
            "advertisers": sorted(advertisers.values(), key=lambda a: (-a["episodes"], a["name"].casefold())),
        }

    def list_for_feed(self, feed_id=None):
        return self._list_for_feed(("queued", "processing", "failed", "ready"), feed_id)

    def _list_for_feed(self, statuses, feed_id=None):
        placeholders = ",".join("?" for _ in statuses)
        feed_filter = "" if feed_id is None else "AND e.feed_id=?"
        params = statuses if feed_id is None else (*statuses, feed_id)
        with self._db() as con:
            rows = con.execute(f"""SELECT {_EPISODE_FIELDS},e.audio_url,e.audio_path,e.takeaways,e.summary
                FROM episodes e JOIN feeds f ON f.id=e.feed_id WHERE e.status IN ({placeholders}) {feed_filter}
                ORDER BY COALESCE(NULLIF(e.published,''),e.discovered) DESC,e.id DESC""", params)
            return [_episode_dict(row) for row in rows]

    def delete_episode(self, episode_id):
        with self._db(write=True) as con:
            row = con.execute("""SELECT id,feed_id,episode_key,audio_url,status FROM episodes
                              WHERE id=? AND status!='skipped'""", (episode_id,)).fetchone()
            if row is None:
                raise KeyError("Episode not found.")
            if row["status"] not in ("deleting", "deleted"):
                self._mark_deleted(con, row)
        self._work_wake.set()
        return {"ok": True, "cleanup_pending": not self._clean_deleted_episode(row["id"])}

    @staticmethod
    def _mark_deleted(con, row):
        identities = {row["episode_key"], "url:" + row["audio_url"]}
        con.executemany("INSERT OR IGNORE INTO deleted_episode_identities VALUES(?,?)",
                        [(row["feed_id"], identity) for identity in identities])
        # Retaining the row reserves its numeric ID for previously issued audio links.
        con.execute("""UPDATE episodes SET status='deleting',stage='',error='',title='',description='',
                    published='',image_url='',discovered='',transcript='',summary='',takeaways='',ads='[]',
                    duration=0,original_duration=0,audio_path='',is_saved=0,playback_position=0,
                    playback_revision=0 WHERE id=?""", (row["id"],))

    def _expire_episodes(self, now=None):
        if not self.retention_months:
            return 0
        cutoff = _retention_cutoff(now or datetime.now(timezone.utc), self.retention_months)
        with self._db(write=True) as con:
            rows = list(con.execute(
                """SELECT id,feed_id,episode_key,audio_url FROM episodes
                   WHERE status NOT IN ('skipped','processing','deleting','deleted')
                   AND worker_active=0 AND is_saved=0
                   AND julianday(published) < julianday(?)""", (cutoff.isoformat(),)))
            for row in rows:
                self._mark_deleted(con, row)
        if rows:
            LOG.info("Expired %d Aircut episodes older than %d months.", len(rows), self.retention_months)
            self._work_wake.set()
        return len(rows)

    def _retention_loop(self):
        while not self._stop.wait(900):
            try:
                self._expire_episodes()
            except Exception:
                LOG.exception("Episode retention interrupted")

    def _clean_deleted_episode(self, episode_id):
        # Serialize cleanup with the worker's claim/release and duplicate requests.
        # Only this episode's server-owned directory is eligible for removal.
        try:
            with self._db(write=True) as con:
                row = con.execute("SELECT status,worker_active FROM episodes WHERE id=?", (episode_id,)).fetchone()
                if row["status"] != "deleting":
                    return True
                if row["worker_active"]:
                    return False
                parent = self.data_dir / "episodes"
                if parent.is_symlink():
                    raise OSError("The episodes directory is a symbolic link.")
                directory = parent / str(episode_id)
                try:
                    mode = directory.lstat().st_mode
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISDIR(mode):
                        shutil.rmtree(directory)
                    else:
                        directory.unlink()
                con.execute("UPDATE episodes SET status='deleted' WHERE id=?", (episode_id,))
            return True
        except OSError:
            LOG.warning("Episode %s file cleanup will be retried.", episode_id)
            return False

    def _fetch(self, url):
        raw, final_url = fetch_feed(url, max_bytes=MAX_FEED_BYTES)
        return parse_feed(raw, final_url)

    def subscribe(self, url, backfill=1):
        if not isinstance(url, str) or not (url := _web_url(url)):
            raise ValueError("Enter an HTTP or HTTPS podcast feed URL.")
        if type(backfill) is not int or not 0 <= backfill <= 10:
            raise ValueError("Choose between zero and ten recent episodes.")
        with self._db() as con:
            if con.execute("SELECT id FROM feeds WHERE url=? AND active=1", (url,)).fetchone():
                raise ValueError("You already subscribe to this feed.")
        snapshot = self._fetch(url)
        now = _now()
        baseline = max((x.published for x in snapshot.entries if x.published <= now), default="")
        with self._db(write=True) as con:
            feed = con.execute("SELECT id,active FROM feeds WHERE url=?", (url,)).fetchone()
            if feed and feed["active"]:
                raise ValueError("You already subscribe to this feed.")
            if feed:
                feed_id = feed["id"]
                con.execute("""UPDATE feeds SET active=1,version=version+1,title=?,image_url=?,baseline=?,
                            last_checked=?,last_error='' WHERE id=?""",
                            (snapshot.title,snapshot.image_url,baseline,now,feed_id))
            else:
                feed_id = con.execute("""INSERT INTO feeds(url,title,image_url,baseline,last_checked)
                    VALUES(?,?,?,?,?)""", (url,snapshot.title,snapshot.image_url,baseline,now)).lastrowid
            self._ingest(con, feed_id, snapshot, baseline, backfill, now)
            result = con.execute(f"SELECT {_FEED_FIELDS} FROM feeds WHERE id=?", (feed_id,)).fetchone()
        self._work_wake.set()
        return dict(result)

    def _ingest(self, con, feed_id, snapshot, baseline, backfill, now):
        # Initial subscription records the entire snapshot. Reordering old GUIDs never queues them.
        # Without dates, RSS/Atom document order determines the most recent entries.
        cutoff = (_retention_cutoff(datetime.fromisoformat(now), self.retention_months).isoformat()
                  if self.retention_months else "")
        def eligible(entry):
            return not cutoff or not entry.published or entry.published >= cutoff

        selected = set()
        if backfill is not None:
            ranked = sorted((entry for entry in snapshot.entries if eligible(entry)),
                            key=lambda entry: entry.published, reverse=True)
            selected = {entry.key for entry in ranked[:backfill]}
        existing = list(con.execute("SELECT id,episode_key,audio_url,status FROM episodes WHERE feed_id=?", (feed_id,)))
        by_key = {row["episode_key"]: row for row in existing}
        by_url = {row["audio_url"]: row for row in existing}
        deleted = {row[0] for row in con.execute("SELECT identity FROM deleted_episode_identities WHERE feed_id=?", (feed_id,))}
        for entry in snapshot.entries:
            identities = {entry.key, "url:" + entry.audio_url}
            if not deleted.isdisjoint(identities):
                # Keep recognizing an episode when its publisher changes one identifier at a time.
                con.executemany("INSERT OR IGNORE INTO deleted_episode_identities VALUES(?,?)",
                                [(feed_id, identity) for identity in identities - deleted])
                deleted.update(identities)
                continue
            previous = by_key.get(entry.key) or by_url.get(entry.audio_url)
            metadata = (entry.audio_url,entry.title,entry.description,entry.published,entry.image_url)
            if previous:
                con.execute("""UPDATE episodes SET audio_url=?,title=?,description=?,published=?,image_url=?
                    WHERE id=?""", (*metadata,previous["id"]))
                if entry.key in selected and previous["status"] == "skipped":
                    con.execute("UPDATE episodes SET status='queued',stage='Queued',error='' WHERE id=?", (previous["id"],))
                continue
            if backfill is None:
                queue = eligible(entry) and (not entry.published or entry.published >= baseline)
            else:
                queue = entry.key in selected
            con.execute("""INSERT INTO episodes(feed_id,episode_key,audio_url,title,description,published,image_url,
                        discovered,status,stage) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (feed_id,entry.key,*metadata,now,"queued" if queue else "skipped","Queued" if queue else ""))

    def unsubscribe(self, feed_id):
        with self._db(write=True) as con:
            changed = con.execute("UPDATE feeds SET active=0,version=version+1 WHERE id=? AND active=1", (feed_id,))
            if not changed.rowcount:
                raise KeyError("Subscription not found.")
            con.execute("""UPDATE episodes SET status='skipped',stage='',error='' WHERE feed_id=?
                        AND status IN ('queued','processing','failed')""", (feed_id,))

    def refresh(self):
        self._poll_wake.set()

    def retry(self, episode_id):
        with self._db(write=True) as con:
            row = con.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if row is None or row["status"] in ("skipped", "deleting", "deleted"):
                raise KeyError("Episode not found.")
            if row["status"] != "failed":
                raise ValueError("Only a failed episode can be retried.")
            con.execute("UPDATE episodes SET status='queued',stage='Queued',error='' WHERE id=?", (episode_id,))
        self._work_wake.set()
        return self.get_episode(episode_id)

    def _poll_once(self, force=False):
        with self._db() as con:
            feeds = list(con.execute("SELECT id,url,version,baseline,last_checked FROM feeds WHERE active=1"))
        for feed in feeds:
            if self._stop.is_set():
                return
            if not force and time.time() - datetime.fromisoformat(feed["last_checked"]).timestamp() < self.poll_minutes * 60:
                continue
            try:
                snapshot = self._fetch(feed["url"])
            except Exception as exc:
                with self._db(write=True) as con:
                    con.execute("UPDATE feeds SET last_error=?,last_checked=? WHERE id=? AND active=1 AND version=?",
                                (_error_text(exc), _now(), feed["id"],feed["version"]))
                continue
            with self._db(write=True) as con:
                now = _now()
                updated = con.execute("""UPDATE feeds SET title=?,image_url=?,last_error='',last_checked=?
                            WHERE id=? AND active=1 AND version=?""",
                            (snapshot.title,snapshot.image_url,now,feed["id"],feed["version"]))
                if updated.rowcount:
                    self._ingest(con, feed["id"], snapshot, feed["baseline"], None, now)
            self._work_wake.set()

    def _poll_loop(self):
        while not self._stop.is_set():
            force = self._poll_wake.is_set()
            self._poll_wake.clear()
            try:
                self._poll_once(force)
            except Exception:
                LOG.exception("Feed polling interrupted")
            self._poll_wake.wait(min(60, self.poll_minutes * 60))

    def _work_loop(self):
        while not self._stop.is_set():
            self._work_wake.clear()
            try:
                if self._work_next():
                    continue
            except Exception:
                LOG.exception("Episode queue interrupted")
            self._work_wake.wait(15)

    def _work_next(self):
        with self._db() as con:
            pending = list(con.execute("SELECT id FROM episodes WHERE status='deleting' AND worker_active=0"))
        for row in pending:
            self._clean_deleted_episode(row["id"])
        with self._db(write=True) as con:
            job = con.execute("""SELECT e.id,e.title,e.audio_url,e.feed_id,f.version FROM episodes e
                JOIN feeds f ON f.id=e.feed_id WHERE e.status='queued' AND f.active=1
                ORDER BY e.id LIMIT 1""").fetchone()
            if job is None:
                return False
            con.execute("UPDATE episodes SET status='processing',stage='Starting',worker_active=1 WHERE id=?", (job["id"],))

        # A subscription version prevents an old job from publishing after unsubscribe/resubscribe.
        condition = """WHERE id=? AND status='processing' AND EXISTS
                       (SELECT 1 FROM feeds WHERE id=? AND active=1 AND version=?)"""
        identity = (job["id"],job["feed_id"],job["version"])

        def progress(stage):
            if self._stop.is_set():
                raise _Interrupted()
            with self._db(write=True) as con:
                if not con.execute("UPDATE episodes SET stage=? " + condition, (str(stage)[:400],*identity)).rowcount:
                    raise _Interrupted()

        try:
            from podcast_processing import process_episode
            directory = self.data_dir / "episodes" / str(job["id"])
            directory.mkdir(parents=True, exist_ok=True)
            result = process_episode(job["audio_url"], directory, job["title"], progress)
            audio_path = Path(result["audio_path"]).resolve()
            if not audio_path.is_relative_to(directory) or not audio_path.is_file() or not audio_path.stat().st_size:
                raise ValueError("Processing did not produce a playable audio file.")
            with self._db(write=True) as con:
                con.execute("""UPDATE episodes SET transcript=?,takeaways=?,summary=?,ads=?,duration=?,original_duration=?,audio_path=?,
                            status='ready',stage='Ready',error='' """ + condition,
                            (result["transcript"],result["takeaways"],result["summary"],json.dumps(result["ads"]),
                             result["duration"],result.get("original_duration", result["duration"]),str(audio_path),*identity))
        except _Interrupted:
            with self._db(write=True) as con:
                con.execute("UPDATE episodes SET status='queued',stage='Waiting to resume' " + condition, identity)
        except Exception as exc:
            with self._db(write=True) as con:
                con.execute("UPDATE episodes SET status='failed',stage='Failed',error=? " + condition,
                            (_error_text(exc),*identity))
        finally:
            with self._db(write=True) as con:
                con.execute("UPDATE episodes SET worker_active=0 WHERE id=?", (job["id"],))
                status = con.execute("SELECT status FROM episodes WHERE id=?", (job["id"],)).fetchone()["status"]
            if status == "deleting":
                self._clean_deleted_episode(job["id"])
        return True


def _error_text(exc):
    return (str(exc).strip() or type(exc).__name__)[:1600]
