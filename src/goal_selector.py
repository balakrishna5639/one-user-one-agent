"""Goal selector module for One User, One Agent.

Evaluates eligibility for the 12 candidate goals, enforces priority ordering,
handles goal window deadlines, and verifies goal attainment.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# Priority order when multiple goals are eligible:
# G01 > G03 > G02 > G04 > G06 > G05 > G12 > G07 > G08 > G09
PRIORITY_ORDER = [
    "G01",
    "G03",
    "G02",
    "G04",
    "G06",
    "G05",
    "G12",
    "G07",
    "G08",
    "G09",
]

GOAL_DEFINITIONS = {
    "G01": {
        "name": "first_play_24h",
        "window_hours": 24,
        "success_event": "play_started",
        "success_condition": "any title",
    },
    "G02": {
        "name": "first_full_episode_3d",
        "window_hours": 72,
        "success_event": "episode_completed",
        "success_condition": "any episode",
    },
    "G03": {
        "name": "second_day_return_2d",
        "window_hours": 48,
        "success_event": "app_open",  # Can be corrected dynamically to play_started
        "success_condition": "on a later IST calendar day than the goal start",
    },
    "G04": {
        "name": "trial_to_paid_7d",
        "window_hours": 168,
        "success_event": "purchase_completed",
        "success_condition": "is_trial = false",
    },
    "G05": {
        "name": "first_paid_7d",
        "window_hours": 168,
        "success_event": "purchase_completed",
        "success_condition": "is_trial = false",
    },
    "G06": {
        "name": "reactivate_lapsed_14d",
        "window_hours": 336,
        "success_event": "purchase_completed",
        "success_condition": "any paid plan",
    },
    "G07": {
        "name": "resume_series_3d",
        "window_hours": 72,
        "success_event": "play_started",
        "success_condition": "same content_slug as unfinished series",
    },
    "G08": {
        "name": "try_new_release_7d",
        "window_hours": 168,
        "success_event": "play_started",
        "success_condition": "title released in 30 days before goal start",
    },
    "G09": {
        "name": "weekly_habit_14d",
        "window_hours": 336,
        "success_event": "play_started",
        "success_condition": "on 3 distinct IST days",
    },
    "G10": {
        "name": "meaningful_engagement_7d",
        "window_hours": 168,
        "success_event": None,
        "success_condition": "engages meaningfully",
    },
    "G11": {
        "name": "tv_activation_14d",
        "window_hours": 336,
        "success_event": "tv_app_open",
        "success_condition": "any",
    },
    "G12": {
        "name": "return_after_absence_7d",
        "window_hours": 168,
        "success_event": "play_started",
        "success_condition": "any title",
    },
}


def parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def to_ist_date(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d")


class GoalSelector:
    """Evaluates viewer eligibility, selects candidate goals, and evaluates success."""

    def __init__(self, memory_instance: Optional[Any] = None, data_loader_instance: Optional[Any] = None):
        self.memory = memory_instance
        self.data_loader = data_loader_instance
        self.goal_defs = dict(GOAL_DEFINITIONS)

    def update_goal_rule(self, goal_id: str, new_success_event: str, new_success_condition: str) -> None:
        """Dynamically updates rule definition in memory."""
        if goal_id in self.goal_defs:
            self.goal_defs[goal_id]["success_event"] = new_success_event
            self.goal_defs[goal_id]["success_condition"] = new_success_condition

    def select(
        self,
        user_id: str,
        events: List[Dict[str, Any]],
        profile: Optional[Dict[str, Any]],
        trigger: Dict[str, Any],
        memory_instance: Optional[Any] = None,
        data_loader_instance: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        """Selects the single highest-priority eligible goal for a viewer.

        Returns:
            dict: {"id": goal_id, "deadline": iso_str, "name": str, ...} or None
        """
        mem = memory_instance or self.memory
        dl = data_loader_instance or self.data_loader

        trigger_time = parse_ts(trigger.get("received_at") or trigger.get("occurred_at"))
        if not trigger_time:
            trigger_time = datetime.now(UTC)

        # 1. Check existing active goal in memory
        if mem:
            active_goal = mem.get_goal(user_id)
            if active_goal and active_goal.get("status") == "active":
                deadline_dt = parse_ts(active_goal.get("deadline"))
                if deadline_dt and trigger_time <= deadline_dt:
                    # Still active and within deadline, keep current goal
                    return {
                        "id": active_goal["goal_id"],
                        "deadline": active_goal["deadline"],
                        "name": active_goal.get("metadata", {}).get("name") or self.goal_defs.get(active_goal["goal_id"], {}).get("name", ""),
                        "success_event": active_goal.get("success_event"),
                        "success_condition": active_goal.get("success_condition"),
                    }
                else:
                    # Deadline passed, mark previous goal inconclusive/failed
                    mem.update_goal_status(user_id, "inconclusive")

        # 2. Check each candidate goal in priority order
        eligible_goals = self.evaluate_all_eligibility(user_id, events, profile, trigger_time, dl)

        for gid in PRIORITY_ORDER:
            if gid in eligible_goals:
                details = self.goal_defs[gid]
                hours = details["window_hours"]
                deadline = (trigger_time + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                committed_at = trigger_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

                goal_record = {
                    "id": gid,
                    "name": details["name"],
                    "deadline": deadline,
                    "committed_at": committed_at,
                    "success_event": details["success_event"],
                    "success_condition": details["success_condition"],
                    "metadata": eligible_goals[gid].get("metadata", {})
                }

                # Save new active goal to memory
                if mem:
                    mem.set_goal(
                        user_id=user_id,
                        goal_id=gid,
                        deadline=deadline,
                        committed_at=committed_at,
                        success_event=details["success_event"],
                        success_condition=details["success_condition"],
                        status="active",
                        metadata=goal_record["metadata"]
                    )
                return goal_record

        return None

    def evaluate_all_eligibility(
        self,
        user_id: str,
        events: List[Dict[str, Any]],
        profile: Optional[Dict[str, Any]],
        now_dt: datetime,
        data_loader: Optional[Any] = None
    ) -> Dict[str, Dict[str, Any]]:
        """Evaluates eligibility for all goals against raw event history and profile."""
        eligible: Dict[str, Dict[str, Any]] = {}

        # Precompute summary statistics from event stream up to now_dt
        user_events = [e for e in events if parse_ts(e.get("received_at") or e.get("occurred_at")) <= now_dt]

        has_play_started = any(e.get("event") == "play_started" for e in user_events)
        has_episode_completed = any(e.get("event") == "episode_completed" for e in user_events)
        
        # Determine effective signup_at
        signup_dt = None
        for e in user_events:
            if e.get("event") == "signup_completed":
                signup_dt = parse_ts(e.get("received_at") or e.get("occurred_at"))
                break
        if not signup_dt and profile and profile.get("signup_at"):
            signup_dt = parse_ts(profile["signup_at"])
        if not signup_dt and user_events:
            signup_dt = parse_ts(user_events[0].get("received_at") or user_events[0].get("occurred_at"))

        # Determine effective subscription_status
        # Event stream purchase/subscription overrides profile
        subscription_status = profile.get("subscription_status") if profile else "never"
        for e in sorted(user_events, key=lambda x: x.get("received_at") or ""):
            ev = e.get("event")
            if ev in ("purchase_completed", "subscription_renewed"):
                props = e.get("properties") or {}
                if props.get("is_trial"):
                    subscription_status = "trial_active"
                else:
                    subscription_status = "active"
            elif ev == "subscription_lapsed":
                subscription_status = "lapsed"

        # G10 meaningful_engagement_7d: ALWAYS SKIP
        logger.info("G10 skipped: no measurable success signal in event stream")

        # G11 tv_activation_14d: ALWAYS SKIP
        logger.info("G11 skipped: success signal tv_app_open not in event stream")

        # G01 first_play_24h: signed_up_within 7 days AND no prior play_started
        if signup_dt and (now_dt - signup_dt) <= timedelta(days=7):
            if not has_play_started:
                eligible["G01"] = {"reason": "Signed up in last 7 days and never played"}

        # G02 first_full_episode_3d: no prior episode_completed
        if not has_episode_completed:
            eligible["G02"] = {"reason": "Has never completed an episode"}

        # G03 second_day_return_2d: signed_up_within 2 days
        if signup_dt and (now_dt - signup_dt) <= timedelta(days=2):
            eligible["G03"] = {"reason": "Signed up in last 2 days"}

        # G04 trial_to_paid_7d: subscription_status == trial_active
        if subscription_status == "trial_active":
            eligible["G04"] = {"reason": "Subscription status is trial_active"}

        # G05 first_paid_7d: subscription_status == never
        if subscription_status == "never":
            eligible["G05"] = {"reason": "Has never had a paid plan"}

        # G06 reactivate_lapsed_14d: subscription_status == lapsed
        if subscription_status == "lapsed":
            eligible["G06"] = {"reason": "Subscription is lapsed"}

        # G07 resume_series_3d:
        # Started a show/micro_drama, has unwatched episodes, and has not played for 3+ days
        dl = data_loader or self.data_loader
        unfinished_series = self._find_unfinished_series(user_events, now_dt, dl)
        if unfinished_series:
            eligible["G07"] = {
                "reason": f"Unfinished series {unfinished_series['slug']} last played 3+ days ago",
                "metadata": {"content_slug": unfinished_series["slug"]}
            }

        # G08 try_new_release_7d: subscription_status == active
        if subscription_status == "active":
            eligible["G08"] = {"reason": "Active subscriber"}

        # G09 weekly_habit_14d: subscription_status == active AND plays on at most 1 distinct IST day in last 14 days
        if subscription_status == "active":
            plays_14d = self._count_distinct_play_days(user_events, now_dt, days=14)
            if plays_14d <= 1:
                eligible["G09"] = {"reason": f"Active subscriber with plays on {plays_14d} days in last 14 days"}

        # G12 return_after_absence_7d: no app_open in the 14 days before the trigger
        app_opens_14d = self._count_app_opens(user_events, now_dt, days=14)
        if app_opens_14d == 0:
            eligible["G12"] = {"reason": "No app_open in the 14 days before trigger"}

        return eligible

    def _count_distinct_play_days(self, events: List[Dict[str, Any]], now_dt: datetime, days: int) -> int:
        cutoff = now_dt - timedelta(days=days)
        distinct_days: Set[str] = set()
        for e in events:
            if e.get("event") == "play_started":
                r_dt = parse_ts(e.get("received_at") or e.get("occurred_at"))
                if r_dt and cutoff <= r_dt <= now_dt:
                    distinct_days.add(to_ist_date(r_dt))
        return len(distinct_days)

    def _count_app_opens(self, events: List[Dict[str, Any]], now_dt: datetime, days: int) -> int:
        cutoff = now_dt - timedelta(days=days)
        count = 0
        for e in events:
            if e.get("event") == "app_open":
                r_dt = parse_ts(e.get("received_at") or e.get("occurred_at"))
                # Only check events strictly before the current trigger
                if r_dt and cutoff <= r_dt < (now_dt - timedelta(seconds=1)):
                    count += 1
        return count

    def _find_unfinished_series(
        self,
        events: List[Dict[str, Any]],
        now_dt: datetime,
        data_loader: Optional[Any]
    ) -> Optional[Dict[str, Any]]:
        """Finds any show/micro_drama with plays, unwatched episodes, and last play >= 3 days ago."""
        last_played: Dict[str, datetime] = {}
        completed_episodes: Dict[str, Set[str]] = {}

        for e in sorted(events, key=lambda x: x.get("received_at") or ""):
            ev = e.get("event")
            props = e.get("properties") or {}
            tid = props.get("title_id")
            epid = props.get("episode_id")
            slug = props.get("content_slug")

            if (tid or slug) and data_loader:
                t = data_loader.get_title(tid) if tid else None
                s = t["content_slug"] if t else slug
                c_type = t.get("content_type") if t else "show"
                if c_type in ("show", "micro_drama") and s:
                    r_dt = parse_ts(e.get("received_at") or e.get("occurred_at"))
                    if ev == "play_started" and r_dt:
                        last_played[s] = r_dt
                    elif ev == "episode_completed" and epid:
                        if s not in completed_episodes:
                            completed_episodes[s] = set()
                        completed_episodes[s].add(epid)

        three_days_ago = now_dt - timedelta(days=3)
        for slug, lp_time in last_played.items():
            if lp_time <= three_days_ago:
                if data_loader:
                    all_eps = data_loader.get_episodes(slug)
                    total_count = len(all_eps)
                    done_count = len(completed_episodes.get(slug, set()))
                    if done_count < total_count:
                        return {"slug": slug, "last_played": lp_time}
                else:
                    return {"slug": slug, "last_played": lp_time}
        return None

    def check_success(
        self,
        user_id: str,
        goal_id: str,
        event_type: str,
        event_props: Optional[Dict[str, Any]] = None,
        event_dt_str: Optional[str] = None,
        memory_instance: Optional[Any] = None
    ) -> bool:
        """Evaluates whether an incoming event fulfills the success condition for the user's active goal.

        Supports dynamic rule updates (such as G03 play_started correction).
        """
        mem = memory_instance or self.memory
        active_goal = mem.get_goal(user_id) if mem else None

        # Check success_event from active goal or fallback to goal definitions
        target_event = None
        if active_goal and active_goal.get("goal_id") == goal_id:
            target_event = active_goal.get("success_event")
        if not target_event and goal_id in self.goal_defs:
            target_event = self.goal_defs[goal_id].get("success_event")

        if event_type != target_event:
            return False

        # Additional condition checks per goal
        props = event_props or {}
        if goal_id in ("G04", "G05"):
            # is_trial must be False
            return props.get("is_trial") is False or props.get("is_trial") == "false"

        if goal_id == "G07":
            target_slug = active_goal.get("metadata", {}).get("content_slug") if active_goal else None
            if target_slug:
                event_slug = props.get("content_slug")
                return event_slug == target_slug

        return True


_default_selector: Optional[GoalSelector] = None


def get_default_goal_selector() -> GoalSelector:
    global _default_selector
    if _default_selector is None:
        from src.memory import get_default_memory
        from src.data_loader import get_default_loader
        _default_selector = GoalSelector(get_default_memory(), get_default_loader())
    return _default_selector


def select_goal(
    user_id: str,
    events: List[Dict[str, Any]],
    profile: Optional[Dict[str, Any]],
    trigger: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    return get_default_goal_selector().select(user_id, events, profile, trigger)


def check_success(
    user_id: str,
    goal_id: str,
    event_type: str,
    event_props: Optional[Dict[str, Any]] = None,
    event_dt_str: Optional[str] = None
) -> bool:
    return get_default_goal_selector().check_success(user_id, goal_id, event_type, event_props, event_dt_str)
