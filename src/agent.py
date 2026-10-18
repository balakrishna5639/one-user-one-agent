"""Agent orchestration module for One User, One Agent.

Orchestrates the 7-step decision lifecycle for one viewer trigger:
1. Updates viewer state facts with provenance.
2. Assembles context and handles missing profiles.
3. Selects the single highest-priority eligible goal.
4. Generates an evidenced decision within resource budgets.
5. Verifies all decisions independently via zero-trust safety checks.
6. Enforces latency SLA (sub-60s) and persists decisions and actions.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from src.data_loader import DataLoader, get_default_loader
from src.decision_maker import DecisionMaker
from src.goal_selector import GoalSelector
from src.memory import Memory, get_default_memory
from src.model_client import MockModelClient, ModelClient
from src.verifier import Verifier

logger = logging.getLogger(__name__)


def process_trigger(
    trigger: Dict[str, Any],
    pack_path: Optional[str] = None,
    db_conn: Optional[Any] = None,
    data_loader: Optional[DataLoader] = None,
    memory: Optional[Memory] = None,
    model_client: Optional[ModelClient] = None,
    verifier: Optional[Verifier] = None,
    withdrawn_titles: Optional[Set[str]] = None,
    is_holdout: Optional[bool] = None,
    goal_selector: Optional[GoalSelector] = None
) -> Dict[str, Any]:
    """Processes a single viewer trigger and returns the finalized decision record."""
    t0 = time.time()
    user_id = trigger["user_id"]
    trigger_ts = trigger.get("received_at") or trigger.get("occurred_at")

    # Initialize dependencies if not supplied
    dl = data_loader or get_default_loader()
    mem = memory or get_default_memory()
    mc = model_client or MockModelClient()
    gs = goal_selector or GoalSelector(mem, dl)
    titles_withdrawn = withdrawn_titles or set()
    vf = verifier or Verifier(dl, mem, titles_withdrawn)

    # Step 1: Update viewer state from trigger (source=app_setting, freshness=1.0)
    app_dialect = trigger.get("app_dialect")
    app_language = trigger.get("app_language")
    if app_dialect:
        mem.set_fact(user_id, "app_dialect", app_dialect, "app_setting", trigger_ts)
    if app_language:
        mem.set_fact(user_id, "app_language", app_language, "app_setting", trigger_ts)

    # Step 2: Assemble viewer context
    events = dl.get_user_events(user_id, before_ts=trigger_ts)
    profile = dl.get_profile(user_id)
    if profile is None:
        logger.info(f"New user {user_id}, no profile snapshot yet, using event data only.")
    else:
        # Load profile facts into memory if not already present
        if profile.get("preferred_dialect"):
            mem.set_fact(user_id, "preferred_dialect", profile["preferred_dialect"], "profile_snapshot", profile.get("updated_at") or "")
        if profile.get("notifications_opt_in") is not None:
            mem.set_fact(user_id, "notifications_opt_in", profile["notifications_opt_in"], "profile_snapshot", profile.get("updated_at") or "")
        if profile.get("whatsapp_opt_in") is not None:
            mem.set_fact(user_id, "whatsapp_opt_in", profile["whatsapp_opt_in"], "profile_snapshot", profile.get("updated_at") or "")
        if profile.get("subscription_status"):
            mem.set_fact(user_id, "subscription_status", profile["subscription_status"], "profile_snapshot", profile.get("updated_at") or "")

    # Determine holdout arm
    if is_holdout is None:
        arm = dl.get_arm(user_id)
        is_holdout = (arm == "holdout")

    # Step 3: Select goal
    goal = None
    if not is_holdout:
        goal = gs.select(
            user_id=user_id,
            events=events,
            profile=profile,
            trigger=trigger,
            memory_instance=mem,
            data_loader_instance=dl
        )

    # Step 4: Make decision
    dm = DecisionMaker(dl, mem, mc, titles_withdrawn)
    decision, action = dm.decide(
        user_id=user_id,
        trigger=trigger,
        goal=goal,
        events=events,
        profile=profile,
        is_holdout=is_holdout
    )

    # Step 5: Verify independently
    vf.set_withdrawn_titles(titles_withdrawn)
    checks = vf.verify(decision, user_id, trigger, action)

    decision["checks"] = {
        "passed": checks["passed"],
        "failed": checks["failed"],
        "warnings": checks.get("warnings", [])
    }

    if checks["failed"]:
        decision["status"] = "BLOCKED"
        decision["action"] = None
        decision["no_action_reason"] = f"verification_failed: {'; '.join(checks['failed'])}"
        action = None
        decision.pop("action_payload", None)
    elif action is not None:
        if checks.get("sanitized_action"):
            action = checks["sanitized_action"]
            decision["action_payload"] = action
        decision["status"] = "PUBLISHED"
    else:
        # Deliberate abstention (e.g. holdout or insufficient evidence)
        if decision.get("status") != "NO_ACTION":
            decision["status"] = "NO_ACTION"

    # Step 6: Measure and record latency
    elapsed_ms = int((time.time() - t0) * 1000)
    decision["decided_in_ms"] = max(1, elapsed_ms)

    # Step 7: Persist decision and published action
    mem.save_decision(
        decision_id=decision["decision_id"],
        user_id=user_id,
        decision_dict=decision,
        trigger_event_id=trigger.get("event_id"),
        decided_at=trigger_ts
    )

    if decision["status"] == "PUBLISHED" and action:
        mem.save_action(
            action_id=action["action_id"],
            decision_id=decision["decision_id"],
            user_id=user_id,
            action_dict=action,
            action_type=action.get("type"),
            published_at=action.get("published_at")
        )

    return decision
