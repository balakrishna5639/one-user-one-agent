"""Memory module for One User, One Agent.

Provides durable per-viewer state storage in SQLite with provenance,
freshness scoring, source precedence rules, goal lifecycle tracking,
decision logging, and published actions history.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

SOURCE_FRESHNESS = {
    "app_setting": 1.0,
    "event_stream": 0.8,
    "profile_snapshot": 0.5,
    "default": 0.1,
}


def to_ist_date(ts_str: Optional[str]) -> str:
    """Converts ISO timestamp string to IST calendar date string (YYYY-MM-DD)."""
    if not ts_str:
        return datetime.now(IST).strftime("%Y-%m-%d")
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return dt.astimezone(IST).strftime("%Y-%m-%d")


class Memory:
    """Manages durable viewer state, goals, decisions, and actions."""

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
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS viewer_state (
                user_id TEXT,
                fact_key TEXT,
                fact_value TEXT,
                source TEXT,
                received_at TEXT,
                freshness_score REAL,
                PRIMARY KEY (user_id, fact_key)
            );

            CREATE TABLE IF NOT EXISTS active_goals (
                user_id TEXT PRIMARY KEY,
                goal_id TEXT,
                committed_at TEXT,
                deadline TEXT,
                status TEXT,
                success_event TEXT,
                success_condition TEXT,
                metadata_json TEXT
            );

            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY,
                user_id TEXT,
                trigger_event_id TEXT,
                decided_at TEXT,
                decision_json TEXT
            );

            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                decision_id TEXT,
                user_id TEXT,
                action_type TEXT,
                published_at TEXT,
                action_json TEXT,
                ist_date TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_actions_user_ist ON actions(user_id, ist_date);
            CREATE INDEX IF NOT EXISTS idx_decisions_trigger ON decisions(trigger_event_id);
            CREATE INDEX IF NOT EXISTS idx_goals_status ON active_goals(status);
            """
        )
        self.conn.commit()

    def set_fact(
        self,
        user_id: str,
        key: str,
        value: Any,
        source: str,
        received_at: str,
        force: bool = False
    ) -> bool:
        """Sets a viewer fact adhering to provenance and freshness rules.

        Precedence rule:
            New fact updates existing fact IF:
            - force is True
            - new source has strictly higher freshness_score
            - same freshness_score and new received_at >= old received_at
        """
        score = SOURCE_FRESHNESS.get(source, SOURCE_FRESHNESS["default"])
        existing = self.get_fact(user_id, key)

        should_update = False
        if force or existing is None:
            should_update = True
        else:
            existing_score = existing["freshness_score"]
            if score > existing_score:
                should_update = True
            elif score == existing_score:
                old_ts = existing["received_at"] or ""
                new_ts = received_at or ""
                if new_ts >= old_ts:
                    should_update = True

        if should_update:
            str_val = json.dumps(value) if not isinstance(value, str) else value
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO viewer_state (
                    user_id, fact_key, fact_value, source, received_at, freshness_score
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, key, str_val, source, received_at, score)
            )
            self.conn.commit()
            return True
        return False

    def get_fact(self, user_id: str, key: str) -> Optional[Dict[str, Any]]:
        """Returns fact dict {value, source, received_at, freshness_score} or None."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT fact_value, source, received_at, freshness_score
            FROM viewer_state
            WHERE user_id = ? AND fact_key = ?
            """,
            (user_id, key)
        )
        row = cur.fetchone()
        if not row:
            return None
        val_raw = row["fact_value"]
        try:
            val = json.loads(val_raw)
        except (json.JSONDecodeError, TypeError):
            val = val_raw
        return {
            "value": val,
            "source": row["source"],
            "received_at": row["received_at"],
            "freshness_score": row["freshness_score"],
        }

    def get_all_facts(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        """Returns all facts known for this user."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT fact_key, fact_value, source, received_at, freshness_score
            FROM viewer_state
            WHERE user_id = ?
            """,
            (user_id,)
        )
        facts = {}
        for row in cur.fetchall():
            val_raw = row["fact_value"]
            try:
                val = json.loads(val_raw)
            except (json.JSONDecodeError, TypeError):
                val = val_raw
            facts[row["fact_key"]] = {
                "value": val,
                "source": row["source"],
                "received_at": row["received_at"],
                "freshness_score": row["freshness_score"],
            }
        return facts

    def set_goal(
        self,
        user_id: str,
        goal_id: str,
        deadline: str,
        committed_at: Optional[str] = None,
        success_event: Optional[str] = None,
        success_condition: Optional[str] = None,
        status: str = "active",
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Sets or replaces the active goal for a viewer."""
        if committed_at is None:
            committed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        meta_json = json.dumps(metadata or {})
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO active_goals (
                user_id, goal_id, committed_at, deadline, status, success_event,
                success_condition, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                goal_id,
                committed_at,
                deadline,
                status,
                success_event,
                success_condition,
                meta_json
            )
        )
        self.conn.commit()

    def get_goal(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Returns the current goal record for a user or None."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT user_id, goal_id, committed_at, deadline, status,
                   success_event, success_condition, metadata_json
            FROM active_goals
            WHERE user_id = ?
            """,
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        return d

    def update_goal_status(self, user_id: str, status: str) -> bool:
        """Updates status of active goal (e.g. succeeded, failed, inconclusive)."""
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE active_goals SET status = ? WHERE user_id = ?",
            (status, user_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def update_open_goals_by_id(
        self,
        goal_id: str,
        new_success_event: str,
        new_success_condition: str
    ) -> int:
        """Retroactively updates success conditions for all open goals of a given goal_id."""
        cur = self.conn.cursor()
        cur.execute(
            """
            UPDATE active_goals
            SET success_event = ?, success_condition = ?
            WHERE goal_id = ? AND status = 'active'
            """,
            (new_success_event, new_success_condition, goal_id)
        )
        self.conn.commit()
        return cur.rowcount

    def save_decision(
        self,
        decision_id: str,
        user_id: str,
        decision_dict: Dict[str, Any],
        trigger_event_id: Optional[str] = None,
        decided_at: Optional[str] = None
    ) -> None:
        """Saves a decision record to SQLite."""
        if trigger_event_id is None:
            trigger_event_id = decision_dict.get("trigger", {}).get("event_id") or ""
        if decided_at is None:
            decided_at = decision_dict.get("trigger", {}).get("received_at") or ""
        dec_json = json.dumps(decision_dict, ensure_ascii=False)
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO decisions (
                decision_id, user_id, trigger_event_id, decided_at, decision_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (decision_id, user_id, trigger_event_id, decided_at, dec_json)
        )
        self.conn.commit()

    def get_decision(self, decision_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT decision_json FROM decisions WHERE decision_id = ?", (decision_id,))
        row = cur.fetchone()
        return json.loads(row["decision_json"]) if row else None

    def is_trigger_processed(self, trigger_event_id: str) -> bool:
        """Checks if a trigger_event_id was already processed (idempotency check)."""
        if not trigger_event_id:
            return False
        cur = self.conn.cursor()
        cur.execute(
            "SELECT 1 FROM decisions WHERE trigger_event_id = ? LIMIT 1",
            (trigger_event_id,)
        )
        return cur.fetchone() is not None

    def save_action(
        self,
        action_id: str,
        decision_id: str,
        user_id: str,
        action_dict: Dict[str, Any],
        action_type: Optional[str] = None,
        published_at: Optional[str] = None,
        ist_date: Optional[str] = None
    ) -> None:
        """Saves a published action to SQLite."""
        act_type = action_type or action_dict.get("type") or ""
        pub_at = published_at or action_dict.get("published_at") or ""
        i_date = ist_date or to_ist_date(pub_at or action_dict.get("send_at"))
        act_json = json.dumps(action_dict, ensure_ascii=False)
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO actions (
                action_id, decision_id, user_id, action_type, published_at, action_json, ist_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (action_id, decision_id, user_id, act_type, pub_at, act_json, i_date)
        )
        self.conn.commit()

    def get_actions_today(self, user_id: str, ist_date: str) -> List[Dict[str, Any]]:
        """Returns all published actions for user_id on given IST calendar date."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT action_id, decision_id, user_id, action_type, published_at, action_json, ist_date
            FROM actions
            WHERE user_id = ? AND ist_date = ?
            """,
            (user_id, ist_date)
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["action"] = json.loads(d.pop("action_json") or "{}")
            result.append(d)
        return result


_default_memory: Optional[Memory] = None


def get_default_memory() -> Memory:
    global _default_memory
    if _default_memory is None:
        _default_memory = Memory()
    return _default_memory


def set_fact(user_id: str, key: str, value: Any, source: str, received_at: str, force: bool = False) -> bool:
    return get_default_memory().set_fact(user_id, key, value, source, received_at, force)


def get_fact(user_id: str, key: str) -> Optional[Dict[str, Any]]:
    return get_default_memory().get_fact(user_id, key)


def set_goal(
    user_id: str,
    goal_id: str,
    deadline: str,
    committed_at: Optional[str] = None,
    success_event: Optional[str] = None,
    success_condition: Optional[str] = None,
    status: str = "active",
    metadata: Optional[Dict[str, Any]] = None
) -> None:
    get_default_memory().set_goal(
        user_id, goal_id, deadline, committed_at, success_event, success_condition, status, metadata
    )


def get_goal(user_id: str) -> Optional[Dict[str, Any]]:
    return get_default_memory().get_goal(user_id)


def update_goal_status(user_id: str, status: str) -> bool:
    return get_default_memory().update_goal_status(user_id, status)


def save_decision(decision_id: str, user_id: str, decision_dict: Dict[str, Any]) -> None:
    get_default_memory().save_decision(decision_id, user_id, decision_dict)


def save_action(action_id: str, decision_id: str, user_id: str, action_dict: Dict[str, Any]) -> None:
    get_default_memory().save_action(action_id, decision_id, user_id, action_dict)


def get_actions_today(user_id: str, ist_date: str) -> List[Dict[str, Any]]:
    return get_default_memory().get_actions_today(user_id, ist_date)
