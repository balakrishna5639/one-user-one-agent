"""Test for late and offline-buffered triggers."""

import os
from src import agent
from src.data_loader import DataLoader
from src.memory import Memory


def test_late_trigger():
    # Setup isolated memory and data loader
    pack_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "one_user_one_agent_pack"))
    dl = DataLoader(":memory:")
    if os.path.exists(pack_path):
        dl.load_catalogue(os.path.join(pack_path, "data", "catalogue.csv"))
    mem = Memory(dl.conn)

    # Create event where occurred_at is 3 days ago but received_at is now
    trigger = {
        "event_id": "test-late-001",
        "user_id": "U-TEST01",
        "event": "app_open",
        "occurred_at": "2026-09-28T10:00:00.000Z",  # 3 days ago
        "sent_at": "2026-09-28T10:00:00.000Z",
        "received_at": "2026-10-01T10:00:00.000Z",  # now
        "app_dialect": "d1",
        "app_language": "hin",
        "platform": "android",
        "deliver_at": "2026-10-01T10:00:00.000Z"
    }

    decision = agent.process_trigger(trigger, data_loader=dl, memory=mem)

    # event_age_s should be 3 days in seconds
    assert decision["trigger"]["event_age_s"] == 3 * 24 * 3600
    # Decision should still be made (not rejected)
    assert decision["status"] in ["PUBLISHED", "NO_ACTION"]
