"""Impact measurement module for One User, One Agent.

Computes honest causal impact comparing the randomized agent arm
against the randomized holdout arm using simulation outcomes.
Re-evaluates the pilot report to identify and explain selection bias.
"""

from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple


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


def compute_wald_ci(p1: float, n1: int, p2: float, n2: int) -> Tuple[float, float]:
    """Computes 95% confidence interval for difference between two proportions."""
    if n1 == 0 or n2 == 0:
        return 0.0, 0.0
    diff = p1 - p2
    se = math.sqrt((p1 * (1.0 - p1) / n1) + (p2 * (1.0 - p2) / n2))
    margin = 1.96 * se
    return diff - margin, diff + margin


class ImpactEvaluator:
    """Evaluates causal uplift against randomized holdout arm."""

    def __init__(self, pack_path: str, actions_path: str, outcomes_path: str):
        self.pack_path = pack_path
        self.actions_path = actions_path
        self.outcomes_path = outcomes_path
        self.arms: Dict[str, str] = {}
        self._load_arms()

    def _load_arms(self) -> None:
        arms_file = os.path.join(self.pack_path, "replay", "arms.csv")
        if os.path.exists(arms_file):
            with open(arms_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    self.arms[r["user_id"]] = r["arm"]

    def evaluate(self, decisions_path: Optional[str] = None) -> Dict[str, Any]:
        """Runs the complete intention-to-treat evaluation."""
        outcome_events = read_jsonl(self.outcomes_path)
        decisions = read_jsonl(decisions_path) if decisions_path else []

        # Map decisions by user_id
        user_goals: Dict[str, str] = {}
        for d in decisions:
            uid = d.get("viewer_id")
            g = d.get("goal")
            if uid and g and g.get("id"):
                user_goals[uid] = g["id"]

        # Aggregate user actions in outcome window
        user_outcomes: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"played": False, "completed": False, "purchased": False, "opens": 0}
        )

        for e in outcome_events:
            uid = e.get("user_id")
            ev = e.get("event")
            if not uid:
                continue
            if ev == "play_started":
                user_outcomes[uid]["played"] = True
            elif ev == "episode_completed":
                user_outcomes[uid]["completed"] = True
            elif ev in ("purchase_completed", "subscription_renewed"):
                user_outcomes[uid]["purchased"] = True
            elif ev == "app_open":
                user_outcomes[uid]["opens"] += 1

        agent_users = [u for u, arm in self.arms.items() if arm == "agent"]
        holdout_users = [u for u, arm in self.arms.items() if arm == "holdout"]

        def check_user_success(u: str) -> bool:
            o = user_outcomes[u]
            gid = user_goals.get(u, "G01")
            if gid in ("G01", "G07", "G08", "G09", "G12"):
                return o["played"]
            elif gid == "G02":
                return o["completed"]
            elif gid in ("G04", "G05", "G06"):
                return o["purchased"]
            elif gid == "G03":
                return o["played"]  # Post-correction
            return o["played"] or o["completed"] or o["purchased"]

        agent_successes = sum(1 for u in agent_users if check_user_success(u))
        holdout_successes = sum(1 for u in holdout_users if check_user_success(u))

        n_agent = len(agent_users)
        n_holdout = len(holdout_users)

        agent_rate = (agent_successes / n_agent) if n_agent else 0.0
        holdout_rate = (holdout_successes / n_holdout) if n_holdout else 0.0
        diff = agent_rate - holdout_rate
        rel_lift = (diff / holdout_rate * 100.0) if holdout_rate > 0 else 0.0
        ci_low, ci_high = compute_wald_ci(agent_rate, n_agent, holdout_rate, n_holdout)

        # Breakdown by goal
        goal_breakdown: Dict[str, Dict[str, int]] = defaultdict(lambda: {"agent_n": 0, "agent_succ": 0, "holdout_n": 0, "holdout_succ": 0})
        for u in agent_users:
            gid = user_goals.get(u, "unassigned")
            goal_breakdown[gid]["agent_n"] += 1
            if check_user_success(u):
                goal_breakdown[gid]["agent_succ"] += 1
        for u in holdout_users:
            gid = user_goals.get(u, "unassigned")
            goal_breakdown[gid]["holdout_n"] += 1
            if check_user_success(u):
                goal_breakdown[gid]["holdout_succ"] += 1

        return {
            "agent_users": n_agent,
            "agent_successes": agent_successes,
            "agent_rate": agent_rate,
            "holdout_users": n_holdout,
            "holdout_successes": holdout_successes,
            "holdout_rate": holdout_rate,
            "diff_points": diff * 100.0,
            "relative_lift_pct": rel_lift,
            "ci_95_low_pts": ci_low * 100.0,
            "ci_95_high_pts": ci_high * 100.0,
            "goal_breakdown": dict(goal_breakdown)
        }

    def generate_report(self, decisions_path: Optional[str] = None) -> str:
        res = self.evaluate(decisions_path)

        lines = [
            "# Impact Report: Agent vs Randomized Holdout",
            "",
            "## Method",
            "",
            "This analysis uses **Intention-To-Treat (ITT)** by comparing all users in the randomized **agent arm** against all users in the randomized **holdout arm**.",
            "",
            "> [!IMPORTANT]",
            "> **Never compare served users vs all other users.** That comparison is fatally confounded by self-selection bias.",
            "",
            "## Results",
            "",
            "| Group | Users | Goal Success Rate | Successful Users |",
            "|---|---|---|---|",
            f"| **Agent arm** | {res['agent_users']} | **{res['agent_rate']*100:.2f}%** | {res['agent_successes']} |",
            f"| **Holdout arm** | {res['holdout_users']} | **{res['holdout_rate']*100:.2f}%** | {res['holdout_successes']} |",
            f"| **Lift** | - | **{res['diff_points']:+.2f} points** ({res['relative_lift_pct']:+.1f}%) | [95% CI: {res['ci_95_low_pts']:+.2f} to {res['ci_95_high_pts']:+.2f} pts] |",
            "",
            "## Why the Pilot Report Was Misleading",
            "",
            "The internal pilot summary report claimed a massive **+22.4 points uplift (40.1% vs 17.7%)** by comparing:",
            "- **Served by the agent:** 217 users who actively opened the app and received an intervention.",
            "- **All other users:** 3,674 users, which included holdout users *plus* treatment users who never opened the app or were inactive.",
            "",
            "### The Selection Bias Mechanism",
            "1. **Conditioning on Post-Treatment Activity:** Users who receive an intervention are by definition users who opened the app. Inactive users never receive an intervention.",
            "2. **Confounded Counterfactual:** Comparing active users against an inactive majority guarantees an illusion of huge uplift, even if the agent had zero causal impact.",
            "3. **Honest ITT Comparison:** When evaluated properly on randomized arms (treatment vs holdout), the true causal impact is measured without cherry-picking active subsets.",
            "",
            "## Breakdown by Candidate Goal",
            "",
            "| Goal ID | Agent Users | Agent Success | Holdout Users | Holdout Success |",
            "|---|---|---|---|---|"
        ]

        for gid, data in sorted(res["goal_breakdown"].items()):
            a_rate = (data["agent_succ"] / data["agent_n"] * 100) if data["agent_n"] else 0.0
            h_rate = (data["holdout_succ"] / data["holdout_n"] * 100) if data["holdout_n"] else 0.0
            lines.append(f"| {gid} | {data['agent_n']} | {a_rate:.1f}% ({data['agent_succ']}) | {data['holdout_n']} | {h_rate:.1f}% ({data['holdout_succ']}) |")

        lines.extend([
            "",
            "## Limitations & Threats to Validity",
            "- **Simulation Model Assumptions:** The simulator utilizes stochastic outcome sampling with fixed propensity baselines.",
            "- **Sample Size Constraints:** Replay window of 1 day with 2,769 triggers yields moderate statistical power for rare event goals (e.g. trial-to-paid conversion).",
            "- **Single Replay Day:** Seasonality and day-of-week effects are not captured in a single 24-hour trigger stream.",
            ""
        ])

        return "\n".join(lines)


def write_impact_report(
    pack_path: str,
    actions_path: str,
    outcomes_path: str,
    out_file: str,
    decisions_path: Optional[str] = None
) -> Dict[str, Any]:
    evaluator = ImpactEvaluator(pack_path, actions_path, outcomes_path)
    res = evaluator.evaluate(decisions_path)
    report_text = evaluator.generate_report(decisions_path)
    os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report_text)
    return res
