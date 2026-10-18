"""Test for tool and model failures with safe fallback."""

import os
from src import agent
from src.data_loader import DataLoader
from src.memory import Memory
from src.model_client import FaultInjectingModelClient


def test_model_timeout_fallback():
    pack_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "one_user_one_agent_pack"))
    dl = DataLoader(":memory:")
    if os.path.exists(pack_path):
        dl.load_catalogue(os.path.join(pack_path, "data", "catalogue.csv"))
    mem = Memory(dl.conn)

    trigger = {
        "event_id": "test-trigger-001",
        "user_id": "U-TEST03",
        "event": "app_open",
        "occurred_at": "2026-10-01T10:00:00.000Z",
        "received_at": "2026-10-01T10:00:00.000Z",
        "app_dialect": "d1",
        "app_language": "hin",
        "platform": "android"
    }

    # Inject a timeout fault
    client = FaultInjectingModelClient(faults=[{
        "trigger_event_id": "test-trigger-001",
        "fault": "timeout",
        "seconds": 45
    }])

    # Agent should catch timeout and fall back
    decision = agent.process_trigger(
        trigger=trigger,
        data_loader=dl,
        memory=mem,
        model_client=client
    )

    # Must publish within 60 seconds total
    assert decision["decided_in_ms"] < 60000

    # Must not crash
    assert decision["status"] in ["PUBLISHED", "NO_ACTION"]

    # Must record the fallback
    assert "model_timeout" in decision.get("assumptions", [])
