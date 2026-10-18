# Impact Report: Agent vs Randomized Holdout

## Method

This analysis uses **Intention-To-Treat (ITT)** by comparing all users in the randomized **agent arm** against all users in the randomized **holdout arm**.

> [!IMPORTANT]
> **Never compare served users vs all other users.** That comparison is fatally confounded by self-selection bias.

## Results

| Group | Users | Goal Success Rate | Successful Users |
|---|---|---|---|
| **Agent arm** | 2513 | **15.44%** | 388 |
| **Holdout arm** | 2487 | **25.93%** | 645 |
| **Lift** | - | **-10.50 points** (-40.5%) | [95% CI: -12.72 to -8.27 pts] |

## Why the Pilot Report Was Misleading

The internal pilot summary report claimed a massive **+22.4 points uplift (40.1% vs 17.7%)** by comparing:
- **Served by the agent:** 217 users who actively opened the app and received an intervention.
- **All other users:** 3,674 users, which included holdout users *plus* treatment users who never opened the app or were inactive.

### The Selection Bias Mechanism
1. **Conditioning on Post-Treatment Activity:** Users who receive an intervention are by definition users who opened the app. Inactive users never receive an intervention.
2. **Confounded Counterfactual:** Comparing active users against an inactive majority guarantees an illusion of huge uplift, even if the agent had zero causal impact.
3. **Honest ITT Comparison:** When evaluated properly on randomized arms (treatment vs holdout), the true causal impact is measured without cherry-picking active subsets.

## Breakdown by Candidate Goal

| Goal ID | Agent Users | Agent Success | Holdout Users | Holdout Success |
|---|---|---|---|---|
| G01 | 61 | 59.0% (36) | 0 | 0.0% (0) |
| G02 | 270 | 24.1% (65) | 0 | 0.0% (0) |
| G03 | 1 | 100.0% (1) | 0 | 0.0% (0) |
| G05 | 173 | 8.1% (14) | 0 | 0.0% (0) |
| G06 | 67 | 10.4% (7) | 0 | 0.0% (0) |
| G07 | 247 | 91.9% (227) | 0 | 0.0% (0) |
| G08 | 44 | 86.4% (38) | 0 | 0.0% (0) |
| unassigned | 1650 | 0.0% (0) | 2487 | 25.9% (645) |

## Limitations & Threats to Validity
- **Simulation Model Assumptions:** The simulator utilizes stochastic outcome sampling with fixed propensity baselines.
- **Sample Size Constraints:** Replay window of 1 day with 2,769 triggers yields moderate statistical power for rare event goals (e.g. trial-to-paid conversion).
- **Single Replay Day:** Seasonality and day-of-week effects are not captured in a single 24-hour trigger stream.
