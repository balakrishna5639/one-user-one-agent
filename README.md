# One User, One Agent

An agentic system that owns one viewer's lifecycle on an OTT dialect streaming platform. For each viewer, it chooses one measurable goal, decides what to do (including doing nothing), acts within a strict time budget, and measures whether it made a difference.

## Setup

```bash
pip install -r requirements.txt
```

Only `pytest` is required. All core logic uses standard-library Python (3.9+).

## Run Replay & Simulation

```bash
python src/harness.py --pack ../one_user_one_agent_pack --out sample_run/
```

This single command:
1. Loads and deduplicates 30 days of viewer events, profiles, catalogue, and arms into SQLite.
2. Replays the 24-hour trigger stream alongside live events, title withdrawals, model faults, and rule corrections.
3. Enforces sub-60s latency SLAs, strict holdout isolation, and prompt injection defense.
4. Outputs `sample_run/decisions.jsonl` and `sample_run/actions.jsonl`.
5. Executes the 14-day outcome simulator (`simulate_outcomes.py`).
6. Computes honest causal lift against the randomized holdout arm in `sample_run/impact_report.md`.
7. Generates latency benchmarks in `sample_run/latency_report.json`.

## Run Automated Tests

```bash
python -m pytest tests/ -v
# or: pytest tests/ -v
```

Executes test suites covering:
- Late & offline-buffered triggers (`test_late_trigger.py`)
- 3-way dialect conflict resolution (`test_conflicting_source.py`)
- Model timeouts, malformed JSON, and graceful fallback (`test_tool_failure.py`)
- Mid-replay dynamic goal rule correction (`test_goal_correction.py`)
- Safety, quiet hours, frequency capping, holdout isolation, and prompt injection guards (`test_safety_and_policy.py`)

## Mock Mode & Zero Credentials

No external API keys or credentials are required. By default, `harness.py` runs with `MockModelClient` (deterministic rule-based decisions) and `FaultInjectingModelClient` (reproducing the faults specified in `replay/model_faults.jsonl`).
