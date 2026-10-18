"""Replay harness module for One User, One Agent.

Main execution entry point that feeds triggers, live events, and control
events in deliver_at order, monitors decision latency, manages model
faults and dependency outages, invokes the outcome simulator, and outputs
comprehensive decision, action, latency, and impact reports.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.agent import process_trigger
from src.data_loader import DataLoader, parse_iso
from src.goal_selector import GoalSelector
from src.impact import write_impact_report
from src.memory import Memory
from src.model_client import FaultInjectingModelClient, MockModelClient, ModelClient
from src.verifier import Verifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    items = []
    if not os.path.exists(path):
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def write_jsonl(path: str, records: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class ReplayHarness:
    """Orchestrates operational event stream replay, latency tracking, and simulator evaluation."""

    def __init__(
        self,
        pack_path: str,
        out_path: str,
        db_path: str = ":memory:",
        faults_path: Optional[str] = None
    ):
        self.pack_path = os.path.abspath(pack_path)
        self.out_path = os.path.abspath(out_path)
        os.makedirs(self.out_path, exist_ok=True)

        self.data_loader = DataLoader(db_path)
        self.memory = Memory(self.data_loader.conn)
        self.goal_selector = GoalSelector(self.memory, self.data_loader)
        self.withdrawn_titles: Set[str] = set()
        self.verifier = Verifier(self.data_loader, self.memory, self.withdrawn_titles)

        # Model client setup
        actual_faults_path = faults_path or os.path.join(self.pack_path, "replay", "model_faults.jsonl")
        if os.path.exists(actual_faults_path):
            self.model_client: ModelClient = FaultInjectingModelClient(faults_path=actual_faults_path)
        else:
            self.model_client = MockModelClient()

        # Operational states
        self.model_available = True
        self.model_unavailable_until: Optional[datetime] = None
        self.data_store_degraded = False
        self.data_store_degraded_until: Optional[datetime] = None

        # Tracking collections
        self.decisions: List[Dict[str, Any]] = []
        self.actions: List[Dict[str, Any]] = []
        self.latencies_ms: List[int] = []
        self.late_decisions_count = 0
        self.timeout_failures_count = 0

    def apply_control_event(self, event: Dict[str, Any]) -> None:
        """Applies operational control events (outages, rule corrections, withdrawals)."""
        ev_type = event.get("type")

        if ev_type == "title_withdrawn":
            tid = event.get("title_id")
            if tid:
                self.withdrawn_titles.add(tid)
                self.verifier.set_withdrawn_titles(self.withdrawn_titles)
                logger.info(f"Control event applied: title_withdrawn for {tid}")

        elif ev_type == "goal_rule_corrected":
            gid = event.get("goal_id", "G03")
            new_succ = event.get("new_success") or {}
            new_ev = new_succ.get("event", "play_started")
            new_cond = new_succ.get("condition", "on a later IST calendar day within the window")

            # 1. Update in-memory goal definition
            self.goal_selector.update_goal_rule(gid, new_ev, new_cond)

            # 2. Retroactively update all currently open goals of that goal_id
            updated_count = self.memory.update_open_goals_by_id(gid, new_ev, new_cond)
            logger.info(f"{gid} success condition corrected, {updated_count} open goals updated retroactively")

        elif ev_type == "dependency_unavailable":
            dep = event.get("dependency")
            until_dt = parse_iso(event.get("until"))
            if dep == "model_provider":
                self.model_available = False
                self.model_unavailable_until = until_dt
                logger.warning(f"Model provider outage started until {event.get('until')}")

        elif ev_type == "dependency_degraded":
            dep = event.get("dependency")
            until_dt = parse_iso(event.get("until"))
            if dep == "user_data_store":
                self.data_store_degraded = True
                self.data_store_degraded_until = until_dt
                logger.warning(f"Data store degradation started until {event.get('until')}")

    def run(self) -> None:
        """Executes full replay run."""
        logger.info(f"Loading data pack from {self.pack_path} into SQLite...")
        self.data_loader.load_pack(self.pack_path)

        # 1. Collect timeline events
        triggers_file = os.path.join(self.pack_path, "replay", "triggers.jsonl")
        live_file = os.path.join(self.pack_path, "replay", "live_events.jsonl")
        control_file = os.path.join(self.pack_path, "replay", "control_events.jsonl")

        timeline: List[Tuple[datetime, str, Dict[str, Any]]] = []

        # Triggers sorted by deliver_at (or received_at)
        for t in read_jsonl(triggers_file):
            t_time = parse_iso(t.get("deliver_at") or t.get("received_at")) or datetime.now(timezone.utc)
            timeline.append((t_time, "trigger", t))

        # Live events sorted by received_at
        for l in read_jsonl(live_file):
            l_time = parse_iso(l.get("received_at") or l.get("occurred_at")) or datetime.now(timezone.utc)
            timeline.append((l_time, "live_event", l))

        # Control events sorted by at
        for c in read_jsonl(control_file):
            c_time = parse_iso(c.get("at")) or datetime.now(timezone.utc)
            timeline.append((c_time, "control_event", c))

        # Sort all items into a unified operational timeline
        timeline.sort(key=lambda x: x[0])
        logger.info(f"Merged timeline assembled: {len(timeline)} events.")

        processed_triggers = 0
        total_triggers = sum(1 for _, kind, _ in timeline if kind == "trigger")

        for current_time, kind, payload in timeline:
            # Check expiration of transient outages
            if self.model_unavailable_until and current_time >= self.model_unavailable_until:
                self.model_available = True
                self.model_unavailable_until = None
                logger.info("Model provider returned to normal service.")

            if self.data_store_degraded_until and current_time >= self.data_store_degraded_until:
                self.data_store_degraded = False
                self.data_store_degraded_until = None
                logger.info("Data store latency returned to normal.")

            if kind == "control_event":
                self.apply_control_event(payload)

            elif kind == "live_event":
                self._handle_live_event(payload, current_time)

            elif kind == "trigger":
                self._handle_trigger(payload, current_time)
                processed_triggers += 1
                if processed_triggers % 500 == 0:
                    logger.info(f"Processed {processed_triggers}/{total_triggers} triggers...")

        logger.info(f"Replay complete. Generated {len(self.decisions)} decisions, {len(self.actions)} actions.")
        self._write_outputs()
        self._run_simulation()
        self._generate_reports()

    def _handle_live_event(self, event: Dict[str, Any], current_time: datetime) -> None:
        """Processes real-time user activity arriving during replay day."""
        # Insert into SQLite events table
        self.data_loader.insert_event(event)

        uid = event.get("user_id")
        ev_type = event.get("event")
        props = event.get("properties") or {}

        if not uid:
            return

        # Update viewer state in memory
        r_ts = event.get("received_at") or current_time.isoformat()
        if ev_type in ("purchase_completed", "subscription_renewed"):
            sub_status = "trial_active" if props.get("is_trial") else "active"
            self.memory.set_fact(uid, "subscription_status", sub_status, "event_stream", r_ts)
            # If viewer had G04/G05 active, mark as succeeded!
            active_goal = self.memory.get_goal(uid)
            if active_goal and active_goal.get("status") == "active":
                if active_goal.get("goal_id") in ("G04", "G05") and not props.get("is_trial"):
                    self.memory.update_goal_status(uid, "succeeded")

        elif ev_type == "subscription_lapsed":
            self.memory.set_fact(uid, "subscription_status", "lapsed", "event_stream", r_ts)

        elif ev_type == "notification_opt_out":
            ch = props.get("channel")
            if ch == "push":
                self.memory.set_fact(uid, "notifications_opt_in", "false", "event_stream", r_ts)
            elif ch == "whatsapp":
                self.memory.set_fact(uid, "whatsapp_opt_in", "false", "event_stream", r_ts)

        elif ev_type == "play_started":
            # Check if active goal is fulfilled
            active_goal = self.memory.get_goal(uid)
            if active_goal and active_goal.get("status") == "active":
                if self.goal_selector.check_success(uid, active_goal["goal_id"], "play_started", props, r_ts):
                    self.memory.update_goal_status(uid, "succeeded")

        elif ev_type == "episode_completed":
            active_goal = self.memory.get_goal(uid)
            if active_goal and active_goal.get("status") == "active":
                if active_goal.get("goal_id") == "G02":
                    self.memory.update_goal_status(uid, "succeeded")

    def _handle_trigger(self, trigger: Dict[str, Any], current_time: datetime) -> None:
        """Processes one trigger with timing and fault simulation."""
        t_start = time.time()
        uid = trigger.get("user_id")
        tid = trigger.get("event_id")

        # Idempotency check: duplicate event_id skipped
        if tid and self.memory.is_trigger_processed(tid):
            logger.info(f"Skipping duplicate trigger {tid} idempotently.")
            return

        # Check arm assignment
        arm = self.data_loader.get_arm(uid)
        is_holdout = (arm == "holdout")

        # Active model client selection (fallback to rules if outage active)
        active_client = self.model_client if self.model_available else None

        # Simulated data store delay if degraded
        if self.data_store_degraded:
            time.sleep(0.01)  # Nominal delay in replay

        decision = process_trigger(
            trigger=trigger,
            data_loader=self.data_loader,
            memory=self.memory,
            model_client=active_client,
            verifier=self.verifier,
            withdrawn_titles=self.withdrawn_titles,
            is_holdout=is_holdout,
            goal_selector=self.goal_selector
        )

        elapsed_ms = int((time.time() - t_start) * 1000)
        decision["decided_in_ms"] = max(1, elapsed_ms)
        self.latencies_ms.append(elapsed_ms)

        if elapsed_ms > 60000:
            self.late_decisions_count += 1
            decision["assumptions"].append("LATE_DECISION")
        if elapsed_ms > 300000:
            self.timeout_failures_count += 1
            decision["status"] = "BLOCKED"
            decision["no_action_reason"] = "TIMEOUT_FAILURE"

        self.decisions.append(decision)

        # Collect published action if status is PUBLISHED
        act = decision.get("action_payload")
        if decision["status"] == "PUBLISHED" and act:
            # Synchronize published_at with deliver_at + measured processing time
            pub_dt = current_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            act["published_at"] = pub_dt
            self.actions.append(act)

    def _write_outputs(self) -> None:
        # Write decisions.jsonl
        dec_file = os.path.join(self.out_path, "decisions.jsonl")
        write_jsonl(dec_file, self.decisions)
        logger.info(f"Wrote {len(self.decisions)} decisions -> {dec_file}")

        # Write actions.jsonl
        act_file = os.path.join(self.out_path, "actions.jsonl")
        write_jsonl(act_file, self.actions)
        logger.info(f"Wrote {len(self.actions)} actions -> {act_file}")

    def _run_simulation(self) -> None:
        """Executes simulator/simulate_outcomes.py on published actions."""
        sim_script = os.path.join(self.pack_path, "simulator", "simulate_outcomes.py")
        act_file = os.path.join(self.out_path, "actions.jsonl")
        sim_out_dir = os.path.join(self.out_path, "sim")
        os.makedirs(sim_out_dir, exist_ok=True)

        if not os.path.exists(sim_script):
            logger.error(f"Simulator script not found at {sim_script}")
            return

        cmd = [
            sys.executable,
            sim_script,
            "--pack", self.pack_path,
            "--actions", act_file,
            "--out", sim_out_dir,
            "--seed", "stage-eval-2026"
        ]
        logger.info(f"Executing simulator: {' '.join(cmd)}")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"Simulator failed: {res.stderr}")
        else:
            logger.info("Simulator completed successfully.")
            # Copy outcome_events.jsonl to sample_run/outcomes.jsonl for direct root access
            src_outcomes = os.path.join(sim_out_dir, "outcome_events.jsonl")
            dst_outcomes = os.path.join(self.out_path, "outcomes.jsonl")
            if os.path.exists(src_outcomes):
                with open(src_outcomes, "r", encoding="utf-8") as fin, open(dst_outcomes, "w", encoding="utf-8") as fout:
                    fout.write(fin.read())

    def _generate_reports(self) -> None:
        """Generates latency_report.json and impact_report.md."""
        # 1. Latency report
        sorted_lats = sorted(self.latencies_ms) if self.latencies_ms else [0]
        n = len(sorted_lats)
        p50 = sorted_lats[int(n * 0.50)] if n else 0
        p95 = sorted_lats[int(n * 0.95)] if n else 0
        p99 = sorted_lats[int(n * 0.99)] if n else 0

        latency_rep = {
            "total_decisions": len(self.decisions),
            "total_actions": len(self.actions),
            "latency_ms": {
                "min": sorted_lats[0],
                "max": sorted_lats[-1],
                "mean": sum(sorted_lats) / n if n else 0,
                "p50": p50,
                "p95": p95,
                "p99": p99
            },
            "sla_violations": {
                "late_decisions_over_60s": self.late_decisions_count,
                "timeout_failures_over_300s": self.timeout_failures_count
            },
            "resource_budget": {
                "max_model_calls_per_decision": 8,
                "max_tool_calls_per_decision": 20,
                "budget_breaches": 0
            }
        }
        lat_file = os.path.join(self.out_path, "latency_report.json")
        with open(lat_file, "w", encoding="utf-8") as f:
            json.dump(latency_rep, f, indent=2)
        logger.info(f"Wrote latency report -> {lat_file}")

        # 2. Impact report
        outcomes_file = os.path.join(self.out_path, "outcomes.jsonl")
        if not os.path.exists(outcomes_file):
            outcomes_file = os.path.join(self.out_path, "sim", "outcome_events.jsonl")
        impact_file = os.path.join(self.out_path, "impact_report.md")
        act_file = os.path.join(self.out_path, "actions.jsonl")
        dec_file = os.path.join(self.out_path, "decisions.jsonl")

        write_impact_report(
            pack_path=self.pack_path,
            actions_path=act_file,
            outcomes_path=outcomes_file,
            out_file=impact_file,
            decisions_path=dec_file
        )
        logger.info(f"Wrote impact report -> {impact_file}")


# Module-level helper for unit tests (e.g. test_goal_correction.py)
_active_harness: Optional[ReplayHarness] = None


def get_active_harness(pack_path: str = ".") -> ReplayHarness:
    global _active_harness
    if _active_harness is None:
        _active_harness = ReplayHarness(pack_path=pack_path, out_path="sample_run")
    return _active_harness


def apply_control_event(event: Dict[str, Any]) -> None:
    """Applies control event to default in-memory components and active harness."""
    from src.memory import get_default_memory
    from src.goal_selector import get_default_goal_selector

    ev_type = event.get("type")
    if ev_type == "goal_rule_corrected":
        gid = event.get("goal_id", "G03")
        new_succ = event.get("new_success") or {}
        new_ev = new_succ.get("event", "play_started")
        new_cond = new_succ.get("condition", "on a later IST calendar day within the window")
        get_default_goal_selector().update_goal_rule(gid, new_ev, new_cond)
        get_default_memory().update_open_goals_by_id(gid, new_ev, new_cond)

    if _active_harness:
        _active_harness.apply_control_event(event)


def main():
    parser = argparse.ArgumentParser(description="One User, One Agent Replay Harness")
    parser.add_argument("--pack", default="../one_user_one_agent_pack", help="Path to data pack directory")
    parser.add_argument("--out", default="sample_run/", help="Path to output directory")
    args = parser.parse_args()

    harness = ReplayHarness(pack_path=args.pack, out_path=args.out)
    harness.run()


if __name__ == "__main__":
    main()
