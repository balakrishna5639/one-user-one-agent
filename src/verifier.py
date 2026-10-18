"""Independent verification module for One User, One Agent.

Executes 9 independent verification checks against raw SQLite state
and platform safety constraints before any action can be published.
Never trusts LLM output as evidence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

INJECTION_PATTERNS = [
    "ignore",
    "system note",
    "send message to every",
    "override",
    "forget previous",
    "system note for ai",
    "ignore your previous rules",
]

QUIET_START_HOUR = 22  # 22:00 IST
QUIET_END_HOUR = 8     # 08:00 IST


def parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def is_quiet_hours(dt: datetime) -> bool:
    h = to_ist(dt).hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def scan_for_injection(text: Optional[str]) -> bool:
    """Scans text for prompt injection keywords. Returns True if injection attempt found."""
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in INJECTION_PATTERNS)


class Verifier:
    """Independent verification engine."""

    def __init__(
        self,
        data_loader: Any,
        memory: Any,
        withdrawn_titles: Optional[Set[str]] = None
    ):
        self.data_loader = data_loader
        self.memory = memory
        self.withdrawn_titles = withdrawn_titles or set()

    def set_withdrawn_titles(self, withdrawn: Set[str]) -> None:
        self.withdrawn_titles = withdrawn

    def verify(
        self,
        decision: Dict[str, Any],
        user_id: str,
        trigger: Dict[str, Any],
        action: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Runs all 9 independent checks.

        Returns:
            dict: {"passed": list, "failed": list, "warnings": list, "sanitized_action": dict or None}
        """
        passed: List[str] = []
        failed: List[str] = []
        warnings: List[str] = []

        now_dt = parse_ts(trigger.get("received_at") or trigger.get("occurred_at")) or datetime.now(UTC)
        trigger_event_id = trigger.get("event_id") or ""
        app_setting_dialect = trigger.get("app_dialect")

        # 1. Duplicate check (Idempotency)
        if trigger_event_id and self.memory.is_trigger_processed(trigger_event_id):
            failed.append(f"duplicate_check: trigger {trigger_event_id} has already been processed")
        else:
            passed.append("duplicate_check")

        # If decision status is NO_ACTION and no action is proposed, verification passes
        target_action = action or decision.get("action_payload")
        if not target_action and decision.get("action") in (None, "NO_ACTION"):
            passed.append("no_action_valid")
            return {"passed": passed, "failed": failed, "warnings": warnings, "sanitized_action": None}

        # 2. Eligibility check: re-verify against raw SQLite events
        goal = decision.get("goal")
        if goal and goal.get("id"):
            gid = goal["id"]
            if gid in ("G10", "G11"):
                failed.append(f"eligibility_check: goal {gid} is unmeasurable or schema-invalid")
            else:
                events = self.data_loader.get_user_events(user_id, before_ts=trigger.get("received_at"))
                profile = self.data_loader.get_profile(user_id)
                from src.goal_selector import GoalSelector
                gs = GoalSelector(self.memory, self.data_loader)
                elig_map = gs.evaluate_all_eligibility(user_id, events, profile, now_dt, self.data_loader)
                if gid not in elig_map:
                    failed.append(f"eligibility_check: raw evidence does not support goal {gid}")
                else:
                    passed.append("eligibility_check")
        else:
            passed.append("eligibility_check_none")

        if not target_action:
            return {"passed": passed, "failed": failed, "warnings": warnings, "sanitized_action": None}

        action_type = target_action.get("type") or decision.get("action")

        # 3. Dialect check: confirm content exists in viewer's app_setting dialect
        used_dialect = target_action.get("dialect") or decision.get("dialect", {}).get("used")
        if app_setting_dialect and used_dialect != app_setting_dialect:
            failed.append(f"dialect_check: action dialect {used_dialect} != app_setting {app_setting_dialect}")
        else:
            passed.append("dialect_check")

        # Collect all title IDs referenced in the action
        content_titles: List[str] = []
        if action_type == "home_page":
            hero_id = target_action.get("hero_title_id")
            if hero_id:
                content_titles.append(hero_id)
            for row in target_action.get("rows", []):
                content_titles.extend(row.get("title_ids", []))
        elif action_type in ("push", "whatsapp", "in_app_message"):
            tid = target_action.get("title_id")
            if tid:
                content_titles.append(tid)

        # 4. Availability & Withdrawn check
        avail_ok = True
        for tid in content_titles:
            if tid in self.withdrawn_titles:
                failed.append(f"availability_check: title {tid} was withdrawn")
                avail_ok = False
                break
            t_meta = self.data_loader.get_title(tid)
            if not t_meta:
                failed.append(f"availability_check: title {tid} does not exist in catalogue")
                avail_ok = False
                break
            if t_meta.get("dialect") != used_dialect:
                failed.append(f"dialect_check: title {tid} has dialect {t_meta.get('dialect')}, expected {used_dialect}")
                avail_ok = False
                break
            from_dt = parse_ts(t_meta.get("available_from"))
            until_dt = parse_ts(t_meta.get("available_until"))
            if from_dt and from_dt > now_dt:
                failed.append(f"availability_check: title {tid} not yet available ({t_meta.get('available_from')})")
                avail_ok = False
                break
            if until_dt and now_dt >= until_dt:
                failed.append(f"availability_check: title {tid} expired ({t_meta.get('available_until')})")
                avail_ok = False
                break
        if avail_ok:
            passed.append("availability_check")

        # 5. Consent check
        profile = self.data_loader.get_profile(user_id) or {}
        user_facts = self.memory.get_all_facts(user_id) if self.memory else {}

        if action_type == "push":
            opt_in = user_facts.get("notifications_opt_in", {}).get("value")
            if opt_in is None:
                opt_in = profile.get("notifications_opt_in")
            if str(opt_in).lower() not in ("true", "1"):
                failed.append("consent_check: notifications_opt_in is not true")
            else:
                passed.append("consent_check")

        elif action_type == "whatsapp":
            opt_in = user_facts.get("whatsapp_opt_in", {}).get("value")
            if opt_in is None:
                opt_in = profile.get("whatsapp_opt_in")
            if str(opt_in).lower() not in ("true", "1"):
                failed.append("consent_check: whatsapp_opt_in is not true")
            else:
                passed.append("consent_check")

        elif action_type == "home_page":
            passed.append("consent_check_not_required")

        # 6. Frequency cap check (Max 1 push or whatsapp per viewer per IST calendar day)
        if action_type in ("push", "whatsapp"):
            ist_date = to_ist(now_dt).strftime("%Y-%m-%d")
            actions_today = self.memory.get_actions_today(user_id, ist_date) if self.memory else []
            messages_today = [
                a for a in actions_today
                if a.get("action_type") in ("push", "whatsapp")
            ]
            if len(messages_today) >= 1:
                failed.append(f"frequency_cap_check: viewer already received {len(messages_today)} message(s) today ({ist_date})")
            else:
                passed.append("frequency_cap_check")

        # 7. Quiet hours check (No push or whatsapp between 22:00 and 08:00 IST)
        if action_type in ("push", "whatsapp"):
            send_at_dt = parse_ts(target_action.get("send_at") or target_action.get("published_at")) or now_dt
            if is_quiet_hours(send_at_dt):
                failed.append(f"quiet_hours_check: send time {send_at_dt.isoformat()} falls within 22:00-08:00 IST")
            else:
                passed.append("quiet_hours_check")

        # 8. Min row size check (for home_page)
        sanitized_action = dict(target_action)
        if action_type == "home_page":
            valid_rows = []
            seen_slugs: Set[str] = set()

            hero_id = sanitized_action.get("hero_title_id")
            if hero_id:
                t = self.data_loader.get_title(hero_id)
                if t:
                    seen_slugs.add(t["content_slug"])

            for r in sanitized_action.get("rows", []):
                row_titles = []
                for tid in r.get("title_ids", []):
                    if tid in self.withdrawn_titles:
                        continue
                    t = self.data_loader.get_title(tid)
                    if not t or t.get("dialect") != used_dialect:
                        continue
                    slug = t.get("content_slug")
                    if slug in seen_slugs:
                        continue
                    seen_slugs.add(slug)
                    row_titles.append(tid)

                if len(row_titles) >= 3:
                    valid_rows.append({"row_type": r.get("row_type"), "title_ids": row_titles[:10]})
                else:
                    warnings.append(f"min_row_size_check: dropped row of type {r.get('row_type')} with {len(row_titles)} < 3 titles")

            sanitized_action["rows"] = valid_rows
            if not valid_rows and not hero_id:
                failed.append("min_row_size_check: home_page has no valid rows and no valid hero")
            else:
                passed.append("min_row_size_check")

        # 9. Injection check: scan all text fields for adversarial instructions
        injection_found = False
        text_fields_to_scan = []
        if "text" in sanitized_action:
            if isinstance(sanitized_action["text"], dict):
                text_fields_to_scan.extend(sanitized_action["text"].values())
            elif isinstance(sanitized_action["text"], str):
                text_fields_to_scan.append(sanitized_action["text"])
        if "variables" in sanitized_action and isinstance(sanitized_action["variables"], dict):
            text_fields_to_scan.extend(sanitized_action["variables"].values())

        # Also scan title descriptions of any content used!
        for tid in content_titles:
            t = self.data_loader.get_title(tid)
            if t and t.get("description"):
                text_fields_to_scan.append(t["description"])

        for tf in text_fields_to_scan:
            if scan_for_injection(str(tf)):
                logger.warning(f"INJECTION_ATTEMPT detected in text field: {tf[:80]}...")
                failed.append(f"injection_check: malicious prompt injection pattern detected")
                injection_found = True
                break

        if not injection_found:
            passed.append("injection_check")

        return {
            "passed": passed,
            "failed": failed,
            "warnings": warnings,
            "sanitized_action": sanitized_action if not failed else None
        }


def verify_decision(
    decision: Dict[str, Any],
    user_id: str,
    trigger: Dict[str, Any],
    action: Optional[Dict[str, Any]] = None,
    data_loader: Optional[Any] = None,
    memory: Optional[Any] = None,
    withdrawn_titles: Optional[Set[str]] = None
) -> Dict[str, Any]:
    from src.data_loader import get_default_loader
    from src.memory import get_default_memory
    dl = data_loader or get_default_loader()
    mem = memory or get_default_memory()
    verifier = Verifier(dl, mem, withdrawn_titles)
    return verifier.verify(decision, user_id, trigger, action)
