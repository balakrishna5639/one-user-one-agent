"""Test for resolving 3-way dialect conflicts across app settings, profile, and deeplinks."""

import os
from src import agent
from src.data_loader import DataLoader
from src.memory import Memory


def test_conflicting_dialect_sources():
    pack_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "one_user_one_agent_pack"))
    dl = DataLoader(":memory:")
    if os.path.exists(pack_path):
        dl.load_catalogue(os.path.join(pack_path, "data", "catalogue.csv"))
    mem = Memory(dl.conn)

    # Set profile snapshot with preferred_dialect = d1
    profile = {
        "user_id": "U-TEST02",
        "preferred_dialect": "d1",
        "preferred_language": "hin",
        "subscription_status": "active"
    }
    cur = dl.conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO profiles (user_id, preferred_dialect, preferred_language, subscription_status)
        VALUES (?, ?, ?, ?)
        """,
        (profile["user_id"], profile["preferred_dialect"], profile["preferred_language"], profile["subscription_status"])
    )
    dl.conn.commit()

    def get_title_dialect(title_id: str):
        t = dl.get_title(title_id)
        return t["dialect"] if t else None

    # app_setting=d3, profile=d1, deeplink=d5
    trigger = {
        "event_id": "test-conflict-001",
        "user_id": "U-TEST02",
        "event": "app_open",
        "occurred_at": "2026-10-01T10:00:00.000Z",
        "received_at": "2026-10-01T10:00:00.000Z",
        "app_dialect": "d3",
        "app_language": "hin",
        "platform": "android",
        "properties": {"deeplink": "ott://c/cmp001/d5/title/T0001"}
    }

    decision = agent.process_trigger(trigger, data_loader=dl, memory=mem)

    # app_setting wins
    assert decision["dialect"]["used"] == "d3"
    assert decision["dialect"]["source"] == "app_setting"

    # All conflicts recorded
    assert "profile:d1" in decision["dialect"]["conflicts"]
    assert "deeplink:d5" in decision["dialect"]["conflicts"]

    # All content is in d3
    for title_id in decision["content"]:
        assert get_title_dialect(title_id) == "d3"
