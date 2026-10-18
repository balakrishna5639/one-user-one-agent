"""Model client module for One User, One Agent.

Provides LLM client abstractions including deterministic MockModelClient
and FaultInjectingModelClient for simulating unsupported claims,
malformed JSON outputs, and network timeouts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class ModelClient:
    """Base interface for model clients."""

    def assess_goal(self, user_context: Dict[str, Any], trigger_event_id: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def choose_content(
        self,
        user_context: Dict[str, Any],
        dialect: str,
        goal: Optional[Dict[str, Any]],
        available_titles: List[Dict[str, Any]],
        trigger_event_id: Optional[str] = None
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def generate_message(
        self,
        user_context: Dict[str, Any],
        title: Optional[Dict[str, Any]],
        channel: str,
        trigger_event_id: Optional[str] = None
    ) -> Dict[str, Any]:
        raise NotImplementedError


class MockModelClient(ModelClient):
    """Deterministic, rule-based model client operating with zero credentials."""

    def __init__(self, **kwargs: Any):
        self.call_count = 0

    def assess_goal(self, user_context: Dict[str, Any], trigger_event_id: Optional[str] = None) -> Dict[str, Any]:
        self.call_count += 1
        # Extract basic context
        profile = user_context.get("profile") or {}
        sub_status = profile.get("subscription_status") or "never"
        events = user_context.get("events") or []

        has_played = any(e.get("event") == "play_started" for e in events)
        has_completed = any(e.get("event") == "episode_completed" for e in events)

        if not has_played:
            return {
                "goal_id": "G01",
                "eligible": True,
                "confidence": 0.85,
                "reason": "New viewer without watch history; prime candidate for first play."
            }
        elif not has_completed:
            return {
                "goal_id": "G02",
                "eligible": True,
                "confidence": 0.80,
                "reason": "Viewer has started watching but has never finished a full episode."
            }
        elif sub_status == "trial_active":
            return {
                "goal_id": "G04",
                "eligible": True,
                "confidence": 0.75,
                "reason": "Active trial user reaching conversion window."
            }
        elif sub_status == "never":
            return {
                "goal_id": "G05",
                "eligible": True,
                "confidence": 0.70,
                "reason": "Engaged viewer on free tier eligible for first subscription."
            }
        elif sub_status == "active":
            return {
                "goal_id": "G08",
                "eligible": True,
                "confidence": 0.65,
                "reason": "Active subscriber ready for catalogue discovery."
            }
        return {
            "goal_id": "G09",
            "eligible": True,
            "confidence": 0.60,
            "reason": "General viewer eligible for weekly habit formation."
        }

    def choose_content(
        self,
        user_context: Dict[str, Any],
        dialect: str,
        goal: Optional[Dict[str, Any]],
        available_titles: List[Dict[str, Any]],
        trigger_event_id: Optional[str] = None
    ) -> Dict[str, Any]:
        self.call_count += 1
        # Filter titles strictly matching dialect
        matching = [t for t in available_titles if t.get("dialect") == dialect]
        matching_ids = [t["title_id"] for t in matching[:10]]
        return {
            "title_ids": matching_ids,
            "confidence": 0.78,
            "reason": f"Ranked {len(matching_ids)} titles matching dialect {dialect} and goal."
        }

    def generate_message(
        self,
        user_context: Dict[str, Any],
        title: Optional[Dict[str, Any]],
        channel: str,
        trigger_event_id: Optional[str] = None
    ) -> Dict[str, Any]:
        self.call_count += 1
        t_name = title.get("title") if title else "Popular Show"
        if channel == "push":
            return {
                "title": f"Dekhiye {t_name}",
                "body": f"Naya episode aur kahani abhi uplabdh hai. Shuru kijiye."
            }
        elif channel == "whatsapp":
            return {
                "template_id": "wa_recommend_title_v1",
                "variables": {
                    "title_name": t_name,
                    "dialect_name": title.get("dialect", "apni bhasha") if title else "apni bhasha"
                }
            }
        return {
            "title": f"Watch {t_name}",
            "body": "Stream now on STAGE."
        }


class FaultInjectingModelClient(MockModelClient):
    """Model client that injects simulated faults specified in model_faults.jsonl."""

    def __init__(
        self,
        faults_path: Optional[str] = None,
        faults: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any
    ):
        super().__init__(**kwargs)
        self.faults_map: Dict[str, Dict[str, Any]] = {}

        if faults:
            for f in faults:
                if "trigger_event_id" in f:
                    self.faults_map[f["trigger_event_id"]] = f
        elif faults_path and os.path.exists(faults_path):
            with open(faults_path, "r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()
                    if line:
                        entry = json.loads(line)
                        if "trigger_event_id" in entry:
                            self.faults_map[entry["trigger_event_id"]] = entry

    def assess_goal(
        self,
        user_context: Dict[str, Any],
        trigger_event_id: Optional[str] = None
    ) -> Union[Dict[str, Any], str]:
        self.call_count += 1
        fault_entry = self.faults_map.get(trigger_event_id) if trigger_event_id else None

        if fault_entry:
            fault_type = fault_entry.get("fault")
            if fault_type == "timeout":
                seconds = fault_entry.get("seconds", 45)
                # Sleep a nominal duration to simulate latency without freezing tests
                time.sleep(min(float(seconds), 0.05))
                raise TimeoutError(f"Model call timed out after {seconds} seconds")

            elif fault_type == "malformed_output":
                # Returns malformed string that triggers json.JSONDecodeError
                raw_out = fault_entry.get("model_output", '{"goal_id": "G02", "eligible": tr')
                return raw_out

            elif fault_type == "unsupported_claim":
                # Returns output claiming eligibility without event backing
                out = dict(fault_entry.get("model_output", {}))
                out["injected"] = True
                return out

        return super().assess_goal(user_context, trigger_event_id)
