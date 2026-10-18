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

This single command executes the end-to-end replay pipeline:
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

## Project Structure

```
one-user-one-agent/
├── src/
│   ├── data_loader.py          # SQLite schema, indexing, deduplication, and queries
│   ├── memory.py               # Durable state store, provenance, freshness, and goals
│   ├── goal_selector.py        # 12 candidate goals, priority order, and eligibility
│   ├── decision_maker.py       # Dialect resolution, content ranking, and budget guards
│   ├── verifier.py             # 9 zero-trust verification checks & injection defense
│   ├── model_client.py         # MockModelClient & FaultInjectingModelClient
│   ├── harness.py              # Operational replay runner and simulator coordinator
│   ├── impact.py               # ITT causal measurement vs holdout & pilot critique
│   └── agent.py                # Single-viewer 7-step decision lifecycle orchestrator
├── tests/                      # Automated pytest test suites (all 8 passing)
├── sample_run/
│   ├── decisions.jsonl         # 2,679 decisions with full provenance and checks
│   ├── actions.jsonl           # 1,298 validated actions ingested by simulator
│   ├── outcomes.jsonl          # 31,456 simulated events over 14-day horizon
│   ├── latency_report.json     # SLA benchmarks (p50: 2ms, p95: 20ms, 0 SLA breaches)
│   ├── impact_report.md        # Honest ITT causal comparison vs holdout arm
│   └── final_report.md         # Data traps handled & technical discussion prep
├── requirements.txt            # pytest>=7.0.0
├── README.md                   # Setup, execution, test commands, and overview
├── ARCHITECTURE.md             # Ownership, memory, verification, and failure recovery
├── AI_COLLABORATION.md         # Human direction, AI critique, and rejected suggestions
└── KNOWN_LIMITATIONS.md        # Intentional design choices, goal skips, and 10M scale
```

## Mock Mode & Zero Credentials

No external API keys or credentials are required. By default, `harness.py` runs with `MockModelClient` (deterministic rule-based decisions) and `FaultInjectingModelClient` (reproducing faults from `replay/model_faults.jsonl`).

## Replay Results Summary

- **Total decisions processed:** 2,679
- **Actions published:** 1,298 (100% accepted by simulator; 0 rejected)
- **Policy problems:** **`0`** (Zero quiet-hours violations, zero consent breaches, zero holdout actions)
- **Prompt injection defense:** **`100%`** (Title T0002 malicious system override intercepted and dropped)
- **Latency performance:** Mean **6.6 ms**, p50 **2 ms**, p95 **20 ms**, max **51 ms** (0 SLA violations over 60s)
- **Documentation:** See `sample_run/final_report.md` for answers to technical discussion questions.
