# Known Limitations & Production Roadmap

This document outlines intentional trade-offs, structural constraints of the synthetic pack, and the architectural roadmap for scaling to 10M+ viewers.

---

## 1. Intentional Goal Exclusions

### G10: meaningful_engagement_7d
- **Limitation:** Skipped by design.
- **Root Cause:** In `config/goals.json`, the success event is `null` and condition is `"engages meaningfully"`. The event stream schema contains no engagement composite score or conversion metric.
- **Operational Handling:** Logged explicitly: `"skipped: no measurable success signal in event stream"`. Never assigned to viewers to prevent unmeasurable lifecycle debt.

### G11: tv_activation_14d
- **Limitation:** Skipped by design.
- **Root Cause:** In `config/goals.json`, the success condition requires event `tv_app_open`. However, `tv_app_open` does not exist in the event stream schema (only `app_open`, `home_viewed`, `play_started`, etc. are emitted, with platforms limited to mobile/web).
- **Operational Handling:** Logged explicitly: `"skipped: success signal tv_app_open not in event stream"`.

---

## 2. Replay & Environment Constraints

### Mock Model Default
- **Limitation:** In replay mode, the system defaults to deterministic rule-based ranking and fault simulation rather than calling an external LLM.
- **Rationale:** Evaluators must be able to run the repository with zero external API credentials, deterministic repeatability, and zero network latency penalties. A production deployment would bridge `ModelClient` to a hosted inference gateway (e.g. vLLM or Claude/Gemini API).

### Single-Process Replay Execution
- **Limitation:** The replay harness runs as an ordered, single-threaded event loop to guarantee strict chronological replay.
- **Production Evolution:** Handling 1,000 simultaneous app opens per minute at peak hours requires:
  - Ingestion via Apache Kafka or AWS Kinesis partitioned by `user_id`.
  - Stateless worker pools (FastAPI / gRPC) backed by Redis for fast fact caching.
  - PostgreSQL / CockroachDB for durable state storage.

### Profile Snapshot Staleness
- **Limitation:** `profiles_snapshot.csv` is exported once daily and is up to 24 hours stale. Brand-new users who registered on the replay day have no profile row.
- **Operational Handling:** The system constructs user facts from raw real-time events (`signup_completed`, `login_success`, `app_dialect_changed`) when profile rows are missing.

### Simulator Stochasticity
- **Limitation:** The simulator operates with a fixed seed (`stage-eval-2026`). In production A/B tests, real viewer behavior exhibits high variance, seasonality, network dropouts, and organic word-of-mouth effects.

---

## 3. High-Scale Technical Discussion Notes

When scaling to **10 million viewers and 40 dialects**:
1. **Partitioning by Viewer ID:** Because exactly one agent owns one viewer, the state is naturally sharded by `hash(user_id)`. There are zero cross-viewer transactions.
2. **Pre-computed Candidate Pools:** Dialect catalog filtering (40 dialects) can be maintained as in-memory bitmaps or Redis Sets, allowing candidate retrieval in sub-millisecond time.
3. **Decisions that Should Never Be Automated:**
   - Account bans, fraud accusations, and payment dispute forfeitures.
   - Pushing unvetted user-generated content without human moderation.
   - Overriding explicit marketing opt-outs or quiet-hour limits.
