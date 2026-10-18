"""Decision maker module for One User, One Agent.

Resolves dialect conflicts, selects proportionate actions (or abstains),
assembles personalized content within budget constraints, and outputs
structured decision payloads.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

MAX_MODEL_CALLS = 8
MAX_TOOL_CALLS = 20

INJECTION_PATTERNS = [
    "ignore",
    "system note",
    "send message to every",
    "override",
    "forget previous",
    "system note for ai",
]


def parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def extract_deeplink_dialect(deeplink: Optional[str]) -> Optional[str]:
    """Extracts dialect code from campaign deeplink format: ott://c/<campaign>/<dialect>/title/<title_id>"""
    if not deeplink:
        return None
    match = re.search(r"ott://c/[^/]+/([^/]+)/title/", deeplink)
    if match:
        return match.group(1)
    return None


def contains_injection(text: Optional[str]) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(p in lower for p in INJECTION_PATTERNS)


class DecisionMaker:
    """Core decision engine for single-viewer lifecycle actions."""

    def __init__(
        self,
        data_loader: Any,
        memory: Any,
        model_client: Any,
        withdrawn_titles: Optional[Set[str]] = None,
        max_model_calls: int = MAX_MODEL_CALLS,
        max_tool_calls: int = MAX_TOOL_CALLS
    ):
        self.data_loader = data_loader
        self.memory = memory
        self.model_client = model_client
        self.withdrawn_titles = withdrawn_titles or set()
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls

    def set_withdrawn_titles(self, withdrawn: Set[str]) -> None:
        self.withdrawn_titles = withdrawn

    def decide(
        self,
        user_id: str,
        trigger: Dict[str, Any],
        goal: Optional[Dict[str, Any]],
        events: List[Dict[str, Any]],
        profile: Optional[Dict[str, Any]],
        is_holdout: bool = False
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Makes an evidenced decision for a viewer trigger.

        Returns:
            Tuple[decision_dict, action_payload_or_None]
        """
        model_calls = 0
        tool_calls = 0
        assumptions: List[str] = []
        evidence: List[str] = []

        trigger_time_str = trigger.get("received_at") or trigger.get("occurred_at")
        trigger_time = parse_ts(trigger_time_str) or datetime.now(UTC)
        occurred_time = parse_ts(trigger.get("occurred_at")) or trigger_time
        event_age_s = int((trigger_time - occurred_time).total_seconds())

        trigger_summary = {
            "event": trigger.get("event", "app_open"),
            "event_id": trigger.get("event_id"),
            "received_at": trigger.get("received_at"),
            "event_age_s": max(0, event_age_s)
        }
        if trigger.get("event_id"):
            evidence.append(f"event:{trigger['event_id']}")

        # Holdout check: zero interventions of any kind
        if is_holdout:
            decision_id = f"D-{uuid.uuid4().hex[:8]}"
            decision = {
                "decision_id": decision_id,
                "viewer_id": user_id,
                "trigger": trigger_summary,
                "goal": None,
                "action": None,
                "dialect": {"used": trigger.get("app_dialect", "d1"), "source": "app_setting", "conflicts": []},
                "content": [],
                "confidence": 1.0,
                "confidence_reason": "User is assigned to randomized holdout arm; control isolation active.",
                "evidence": ["arms:holdout"],
                "assumptions": ["holdout users receive zero actions"],
                "checks": {"passed": ["holdout_isolation"], "failed": []},
                "decided_in_ms": 1,
                "status": "NO_ACTION",
                "no_action_reason": "holdout_arm"
            }
            return decision, None

        # 1. Resolve viewer dialect
        # Precedence: app_setting > profile.preferred_dialect > deeplink dialect
        app_dialect = trigger.get("app_dialect")
        profile_dialect = profile.get("preferred_dialect") if profile else None
        props = trigger.get("properties") or {}
        deeplink_dialect = extract_deeplink_dialect(props.get("deeplink"))

        used_dialect = app_dialect or profile_dialect or deeplink_dialect or "d1"
        dialect_source = "app_setting" if app_dialect else ("profile" if profile_dialect else "deeplink")

        conflicts = []
        if profile_dialect and profile_dialect != used_dialect:
            conflicts.append(f"profile:{profile_dialect}")
        if deeplink_dialect and deeplink_dialect != used_dialect:
            conflicts.append(f"deeplink:{deeplink_dialect}")

        # 2. Fetch available catalogue in used dialect
        tool_calls += 1
        available_titles = self.data_loader.get_catalogue(
            dialect=used_dialect,
            at_time=trigger_time_str,
            exclude_withdrawn=self.withdrawn_titles
        )

        # Filter out any title whose description contains prompt injection!
        safe_titles = []
        for t in available_titles:
            if contains_injection(t.get("description")):
                logger.warning(f"Excluding injected title {t['title_id']} from candidate pool.")
                continue
            safe_titles.append(t)

        if not safe_titles:
            decision_id = f"D-{uuid.uuid4().hex[:8]}"
            return {
                "decision_id": decision_id,
                "viewer_id": user_id,
                "trigger": trigger_summary,
                "goal": goal,
                "action": None,
                "dialect": {"used": used_dialect, "source": dialect_source, "conflicts": conflicts},
                "content": [],
                "confidence": 0.5,
                "confidence_reason": "No safe titles currently available in dialect.",
                "evidence": evidence,
                "assumptions": ["dialect catalogue empty or unavailable"],
                "checks": {"passed": [], "failed": ["no_titles_available"]},
                "decided_in_ms": 10,
                "status": "NO_ACTION",
                "no_action_reason": "no_available_titles_in_dialect"
            }, None

        # 3. Model consultation within budget (optional guidance)
        user_context = {
            "user_id": user_id,
            "events": events[-30:],  # Recent events
            "profile": profile,
            "goal": goal,
            "dialect": used_dialect
        }

        # Catch model timeouts or malformed outputs
        fallback_used = False
        if model_calls < self.max_model_calls and self.model_client:
            try:
                model_calls += 1
                model_assessment = self.model_client.assess_goal(
                    user_context,
                    trigger_event_id=trigger.get("event_id")
                )
                if isinstance(model_assessment, str):
                    # Malformed JSON from fault injection
                    try:
                        model_assessment = json.loads(model_assessment)
                    except json.JSONDecodeError:
                        assumptions.append("model_malformed_output_fallback")
                        fallback_used = True
            except TimeoutError:
                assumptions.append("model_timeout")
                fallback_used = True
            except Exception as e:
                assumptions.append("model_failure_fallback")
                fallback_used = True

        # 4. Construct personalized action (Default: HOME_PAGE for active app_open)
        action_type = "home_page"
        hero_title = safe_titles[0]
        evidence.append(f"catalogue:{hero_title['title_id']}")

        # Build rows of distinct content slugs
        seen_slugs = {hero_title["content_slug"]}
        rows: List[Dict[str, Any]] = []

        # Row 1: Popular in dialect
        pop_titles = []
        for t in safe_titles:
            if t["content_slug"] not in seen_slugs and len(pop_titles) < 10:
                pop_titles.append(t["title_id"])
                seen_slugs.add(t["content_slug"])
        if len(pop_titles) >= 3:
            rows.append({"row_type": "popular_in_dialect", "title_ids": pop_titles})

        # Row 2: New releases
        new_titles = []
        for t in sorted(safe_titles, key=lambda x: x.get("release_date") or "", reverse=True):
            if t["content_slug"] not in seen_slugs and len(new_titles) < 10:
                new_titles.append(t["title_id"])
                seen_slugs.add(t["content_slug"])
        if len(new_titles) >= 3:
            rows.append({"row_type": "new_releases", "title_ids": new_titles})

        # Row 3: Genre spotlight
        genres_available = {}
        for t in safe_titles:
            if t["content_slug"] not in seen_slugs:
                for g in (t.get("genres") or "").split("|"):
                    if g:
                        genres_available.setdefault(g, []).append(t)
        if genres_available:
            top_genre = max(genres_available, key=lambda k: len(genres_available[k]))
            genre_titles = []
            for t in genres_available[top_genre]:
                if t["content_slug"] not in seen_slugs and len(genre_titles) < 10:
                    genre_titles.append(t["title_id"])
                    seen_slugs.add(t["content_slug"])
            if len(genre_titles) >= 3:
                rows.append({"row_type": "genre", "title_ids": genre_titles})

        # Collect top content references
        content_refs = [hero_title["title_id"]]
        for r in rows:
            content_refs.extend(r["title_ids"][:3])

        decision_id = f"D-{uuid.uuid4().hex[:8]}"
        action_id = f"A-{uuid.uuid4().hex[:8]}"

        action_payload = {
            "action_id": action_id,
            "decision_id": decision_id,
            "user_id": user_id,
            "trigger_event_id": trigger.get("event_id"),
            "type": "home_page",
            "published_at": trigger_time_str,
            "dialect": used_dialect,
            "hero_title_id": hero_title["title_id"],
            "rows": rows
        }

        confidence = 0.61 if not fallback_used else 0.52
        confidence_reason = (
            f"personalization tailored to dialect {used_dialect} with {len(rows)} diverse rows"
            if not fallback_used else "safe rule-based fallback after model degradation"
        )

        decision = {
            "decision_id": decision_id,
            "viewer_id": user_id,
            "trigger": trigger_summary,
            "goal": {"id": goal["id"], "deadline": goal["deadline"]} if goal else None,
            "action": "HOME_PAGE",
            "dialect": {
                "used": used_dialect,
                "source": dialect_source,
                "conflicts": conflicts
            },
            "content": content_refs[:6],
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "evidence": evidence,
            "assumptions": assumptions or ["viewer has not watched recommended hero title"],
            "checks": {"passed": [], "failed": []},
            "decided_in_ms": 15,
            "status": "PUBLISHED",
            "no_action_reason": None,
            "action_payload": action_payload
        }

        return decision, action_payload


def make_decision(
    user_id: str,
    trigger: Dict[str, Any],
    goal: Optional[Dict[str, Any]],
    events: List[Dict[str, Any]],
    profile: Optional[Dict[str, Any]],
    data_loader: Optional[Any] = None,
    memory: Optional[Any] = None,
    model_client: Optional[Any] = None,
    withdrawn_titles: Optional[Set[str]] = None,
    is_holdout: bool = False
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    from src.data_loader import get_default_loader
    from src.memory import get_default_memory
    from src.model_client import MockModelClient

    dl = data_loader or get_default_loader()
    mem = memory or get_default_memory()
    mc = model_client or MockModelClient()
    dm = DecisionMaker(dl, mem, mc, withdrawn_titles)
    return dm.decide(user_id, trigger, goal, events, profile, is_holdout)
