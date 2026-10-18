"""Data loader module for One User, One Agent.

Loads and deduplicates the STAGE assignment data pack into SQLite,
builds indexes, and provides optimized query interfaces.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple


def parse_iso(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def format_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class DataLoader:
    """Manages SQLite storage and querying of the data pack."""

    def __init__(self, db: Optional[sqlite3.Connection | str] = None):
        if db is None or db == ":memory:":
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        elif isinstance(db, str):
            self.conn = sqlite3.connect(db, check_same_thread=False)
        else:
            self.conn = db
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA synchronous = OFF;
            PRAGMA journal_mode = MEMORY;

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                user_id TEXT,
                event_type TEXT,
                occurred_at TEXT,
                sent_at TEXT,
                received_at TEXT,
                session_id TEXT,
                app_dialect TEXT,
                app_language TEXT,
                platform TEXT,
                app_version TEXT,
                properties_json TEXT
            );

            CREATE TABLE IF NOT EXISTS profiles (
                user_id TEXT PRIMARY KEY,
                signup_at TEXT,
                subscription_status TEXT,
                plan_started_at TEXT,
                plan_ends_at TEXT,
                preferred_dialect TEXT,
                preferred_language TEXT,
                notifications_opt_in TEXT,
                whatsapp_opt_in TEXT,
                platform TEXT,
                updated_at TEXT,
                snapshot_at TEXT
            );

            CREATE TABLE IF NOT EXISTS catalogue (
                title_id TEXT PRIMARY KEY,
                content_slug TEXT,
                title TEXT,
                dialect TEXT,
                language TEXT,
                content_type TEXT,
                genres TEXT,
                release_date TEXT,
                available_from TEXT,
                available_until TEXT,
                is_premium INTEGER,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS episodes (
                episode_id TEXT PRIMARY KEY,
                content_slug TEXT,
                season INTEGER,
                episode INTEGER,
                minutes REAL
            );

            CREATE TABLE IF NOT EXISTS arms (
                user_id TEXT PRIMARY KEY,
                arm TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_events_user_received ON events(user_id, received_at);
            CREATE INDEX IF NOT EXISTS idx_catalogue_dialect_avail ON catalogue(dialect, available_until);
            CREATE INDEX IF NOT EXISTS idx_episodes_slug_ep ON episodes(content_slug, season, episode);
            """
        )
        self.conn.commit()

    def load_pack(self, pack_path: str) -> None:
        """Loads all data pack files from pack_path into SQLite."""
        self.load_catalogue(os.path.join(pack_path, "data", "catalogue.csv"))
        self.load_episodes(os.path.join(pack_path, "data", "episodes.csv"))
        self.load_profiles(os.path.join(pack_path, "data", "profiles_snapshot.csv"))
        self.load_arms(os.path.join(pack_path, "replay", "arms.csv"))
        self.load_events(os.path.join(pack_path, "data", "events.jsonl"))

    def load_catalogue(self, file_path: str) -> int:
        if not os.path.exists(file_path):
            return 0
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                is_prem = 1 if r.get("is_premium", "").lower() in ("true", "1") else 0
                rows.append((
                    r["title_id"],
                    r["content_slug"],
                    r["title"],
                    r["dialect"],
                    r["language"],
                    r["content_type"],
                    r["genres"],
                    r["release_date"],
                    r["available_from"],
                    r["available_until"] or None,
                    is_prem,
                    r.get("description", "")
                ))
        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT OR REPLACE INTO catalogue (
                title_id, content_slug, title, dialect, language, content_type,
                genres, release_date, available_from, available_until, is_premium, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows
        )
        self.conn.commit()
        return len(rows)

    def load_episodes(self, file_path: str) -> int:
        if not os.path.exists(file_path):
            return 0
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append((
                    r["episode_id"],
                    r["content_slug"],
                    int(r["season"]),
                    int(r["episode"]),
                    float(r["minutes"]) if r.get("minutes") else 0.0
                ))
        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT OR REPLACE INTO episodes (
                episode_id, content_slug, season, episode, minutes
            ) VALUES (?, ?, ?, ?, ?)
            """,
            rows
        )
        self.conn.commit()
        return len(rows)

    def load_profiles(self, file_path: str) -> int:
        """Loads profiles, resolving duplicates by taking the latest updated_at."""
        if not os.path.exists(file_path):
            return 0
        latest_profiles: Dict[str, Dict[str, Any]] = {}
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                uid = r["user_id"]
                updated_at = r.get("updated_at", "")
                existing = latest_profiles.get(uid)
                if existing is None or updated_at > existing.get("updated_at", ""):
                    latest_profiles[uid] = r

        rows = [
            (
                r["user_id"],
                r.get("signup_at"),
                r.get("subscription_status"),
                r.get("plan_started_at"),
                r.get("plan_ends_at"),
                r.get("preferred_dialect"),
                r.get("preferred_language"),
                r.get("notifications_opt_in"),
                r.get("whatsapp_opt_in"),
                r.get("platform"),
                r.get("updated_at"),
                r.get("snapshot_at")
            )
            for r in latest_profiles.values()
        ]
        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT OR REPLACE INTO profiles (
                user_id, signup_at, subscription_status, plan_started_at, plan_ends_at,
                preferred_dialect, preferred_language, notifications_opt_in, whatsapp_opt_in,
                platform, updated_at, snapshot_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows
        )
        self.conn.commit()
        return len(rows)

    def load_arms(self, file_path: str) -> int:
        if not os.path.exists(file_path):
            return 0
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append((r["user_id"], r["arm"]))
        cur = self.conn.cursor()
        cur.executemany(
            "INSERT OR REPLACE INTO arms (user_id, arm) VALUES (?, ?)",
            rows
        )
        self.conn.commit()
        return len(rows)

    def load_events(self, file_path: str) -> int:
        """Loads events.jsonl into SQLite, deduplicating on event_id."""
        if not os.path.exists(file_path):
            return 0
        batch = []
        batch_size = 5000
        count = 0
        cur = self.conn.cursor()

        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                batch.append((
                    e["event_id"],
                    e.get("user_id"),
                    e.get("event"),
                    e.get("occurred_at"),
                    e.get("sent_at"),
                    e.get("received_at"),
                    e.get("session_id"),
                    e.get("app_dialect"),
                    e.get("app_language"),
                    e.get("platform"),
                    e.get("app_version"),
                    json.dumps(e.get("properties") or {})
                ))
                count += 1
                if len(batch) >= batch_size:
                    cur.executemany(
                        """
                        INSERT OR IGNORE INTO events (
                            event_id, user_id, event_type, occurred_at, sent_at, received_at,
                            session_id, app_dialect, app_language, platform, app_version, properties_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch
                    )
                    self.conn.commit()
                    batch = []

            if batch:
                cur.executemany(
                    """
                    INSERT OR IGNORE INTO events (
                        event_id, user_id, event_type, occurred_at, sent_at, received_at,
                        session_id, app_dialect, app_language, platform, app_version, properties_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch
                )
                self.conn.commit()
        return count

    def insert_event(self, e: Dict[str, Any]) -> bool:
        """Inserts a single event. Returns True if inserted, False if duplicate."""
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO events (
                event_id, user_id, event_type, occurred_at, sent_at, received_at,
                session_id, app_dialect, app_language, platform, app_version, properties_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                e["event_id"],
                e.get("user_id"),
                e.get("event"),
                e.get("occurred_at"),
                e.get("sent_at"),
                e.get("received_at"),
                e.get("session_id"),
                e.get("app_dialect"),
                e.get("app_language"),
                e.get("platform"),
                e.get("app_version"),
                json.dumps(e.get("properties") or {})
            )
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_user_events(self, user_id: str, before_ts: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns all events for a user sorted chronologically by received_at."""
        cur = self.conn.cursor()
        if before_ts:
            cur.execute(
                """
                SELECT event_id, user_id, event_type as event, occurred_at, sent_at, received_at,
                       session_id, app_dialect, app_language, platform, app_version, properties_json
                FROM events
                WHERE user_id = ? AND received_at <= ?
                ORDER BY received_at ASC
                """,
                (user_id, before_ts)
            )
        else:
            cur.execute(
                """
                SELECT event_id, user_id, event_type as event, occurred_at, sent_at, received_at,
                       session_id, app_dialect, app_language, platform, app_version, properties_json
                FROM events
                WHERE user_id = ?
                ORDER BY received_at ASC
                """,
                (user_id,)
            )
        rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["properties"] = json.loads(d.pop("properties_json") or "{}")
            result.append(d)
        return result

    def get_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Returns viewer profile or None if missing."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT user_id, signup_at, subscription_status, plan_started_at, plan_ends_at,
                   preferred_dialect, preferred_language, notifications_opt_in, whatsapp_opt_in,
                   platform, updated_at, snapshot_at
            FROM profiles
            WHERE user_id = ?
            """,
            (user_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_catalogue(
        self,
        dialect: str,
        at_time: Optional[str] = None,
        exclude_withdrawn: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        """Returns available titles in the given dialect at at_time."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT title_id, content_slug, title, dialect, language, content_type,
                   genres, release_date, available_from, available_until, is_premium, description
            FROM catalogue
            WHERE dialect = ?
            ORDER BY release_date DESC
            """,
            (dialect,)
        )
        rows = cur.fetchall()
        result = []
        check_dt = parse_iso(at_time) if at_time else None
        withdrawn = exclude_withdrawn or set()

        for r in rows:
            d = dict(r)
            if d["title_id"] in withdrawn:
                continue
            if check_dt:
                from_dt = parse_iso(d["available_from"])
                until_dt = parse_iso(d["available_until"])
                if from_dt and from_dt > check_dt:
                    continue
                if until_dt and check_dt >= until_dt:
                    continue
            d["is_premium"] = bool(d["is_premium"])
            result.append(d)
        return result

    def get_title(self, title_id: str) -> Optional[Dict[str, Any]]:
        """Returns single title record by title_id."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT title_id, content_slug, title, dialect, language, content_type,
                   genres, release_date, available_from, available_until, is_premium, description
            FROM catalogue
            WHERE title_id = ?
            """,
            (title_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["is_premium"] = bool(d["is_premium"])
        return d

    def get_episodes(self, content_slug: str) -> List[Dict[str, Any]]:
        """Returns episodes for a content_slug ordered by season and episode number."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT episode_id, content_slug, season, episode, minutes
            FROM episodes
            WHERE content_slug = ?
            ORDER BY season ASC, episode ASC
            """,
            (content_slug,)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_arm(self, user_id: str) -> Optional[str]:
        """Returns 'agent' or 'holdout' or None."""
        cur = self.conn.cursor()
        cur.execute("SELECT arm FROM arms WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row["arm"] if row else None


# Module-level default singleton for easy importing
_default_loader: Optional[DataLoader] = None


def get_default_loader() -> DataLoader:
    global _default_loader
    if _default_loader is None:
        _default_loader = DataLoader()
    return _default_loader


def load_pack(pack_path: str, db: Optional[sqlite3.Connection | str] = None) -> DataLoader:
    loader = DataLoader(db)
    loader.load_pack(pack_path)
    return loader


def get_user_events(user_id: str, before_ts: Optional[str] = None) -> List[Dict[str, Any]]:
    return get_default_loader().get_user_events(user_id, before_ts)


def get_profile(user_id: str) -> Optional[Dict[str, Any]]:
    return get_default_loader().get_profile(user_id)


def get_catalogue(
    dialect: str,
    at_time: Optional[str] = None,
    exclude_withdrawn: Optional[Set[str]] = None
) -> List[Dict[str, Any]]:
    return get_default_loader().get_catalogue(dialect, at_time, exclude_withdrawn)


def get_arm(user_id: str) -> Optional[str]:
    return get_default_loader().get_arm(user_id)


def get_episodes(content_slug: str) -> List[Dict[str, Any]]:
    return get_default_loader().get_episodes(content_slug)


def get_title(title_id: str) -> Optional[Dict[str, Any]]:
    return get_default_loader().get_title(title_id)
