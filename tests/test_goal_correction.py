"""Test for mid-replay dynamic goal rule correction (G03)."""

from src import goal_selector, harness, memory


def test_g03_rule_correction():
    # Open a G03 goal for a user
    memory.set_goal("U-TEST01", "G03", deadline="2026-10-03T00:00:00Z", success_event="app_open")

    # Verify initial success condition
    initial_goal = memory.get_goal("U-TEST01")
    assert initial_goal["success_event"] == "app_open"

    # Apply the control event correction
    correction = {
        "type": "goal_rule_corrected",
        "goal_id": "G03",
        "new_success": {
            "event": "play_started",
            "condition": "on a later IST calendar day within the window"
        }
    }
    harness.apply_control_event(correction)

    # Open G03 goal should now require play_started not app_open
    updated_goal = memory.get_goal("U-TEST01")
    assert updated_goal["success_event"] == "play_started"

    # An app_open should NOT count as success anymore
    result = goal_selector.check_success("U-TEST01", "G03", "app_open")
    assert result is False

    # A play_started SHOULD count as success
    result = goal_selector.check_success("U-TEST01", "G03", "play_started")
    assert result is True
