# delegate-sim

A simulator that compares delegate's executor-selection policies on generated worlds, where every actor's true success rate and cost are known. Each policy sees only what it would see in use: verdicts, observed costs (the lead's as measured, which may be undercounted) and priced cost guesses. It decides what [../proposals/shared-skill.md](../proposals/shared-skill.md) adopts; that document's "Simulated evidence" section records the results.

## Running it

```sh
cargo build --release
./target/release/delegate-sim 300 2000          # 300 worlds per scenario, 2,000 steps
./target/release/delegate-sim sweep 150 2000    # the same at 150 worlds, across 8 population shapes
```

It needs Rust 1.85 or later. Worlds run in parallel as blocking tasks on a [Tokio](https://tokio.rs) runtime, at most 8 at once (`MAX_THREADS` in `src/main.rs`), so a run uses at most 8 CPUs. At that limit the full run takes about 20 minutes and the sweep about 70. Every output starts with a hash of the simulator's source; the outputs behind the design's figures are in [results/](results/), named by date and version.

## What is scored

Every work item must be completed. An attempt that fails, at its verdict or when a disguised failure is found out, is retried by a different actor, as the workflow requires, up to six attempts. Each attempt is scored by the expected cost to complete the item if it goes to the chosen actor (its cost, review and failure costs, plus, if it fails, the cheapest other actor's cost per standing result), minus the same for the best choice. Waiting for an actor, calibration runs and integrity re-checks add their costs. Regret is reported per work item.

## What a world is

- Projects of five kinds of task. Each kind has a difficulty, a lead review cost, a correction cost when a result does not stand, the cost of the lead doing the task itself, a run time to close, and a lag from close to verdict. 3 of 5 kinds are reviewed, where drop rules apply. In slow kinds the lag is long, and a failure costs more the longer it goes unverified.
- Executors in families. Each has its family's skill plus its own offset, a quirk per kind, and a mean cost that rises with skill. A policy's prior on a newcomer's cost is that mean times a random error, standing for a price times a usage guess.
- The lead, which holds at most three attempts at once until they close, and whose cost a policy learns from a priced guess and its measured turns.
- A chance of success for each attempt: the logistic of base rate + skill + quirk − difficulty − any hardness of the item. The default base rate puts a typical attempt near 90%.
- Two new work items a step, in random kinds. About six attempts are open at once, as in w3sim.

Every policy is run on the same items with the same random numbers for outcomes and noise, retries included, so differences between policies are paired by world.

## Scenarios

| Scenario | What it adds |
|---|---|
| baseline | one project of five kinds |
| cheap-bad | three executors that cost $0.10 and usually fail |
| veteran-breaks | the best-value executor's skill collapses halfway |
| break-recovers | the same, recovering at three quarters |
| moderate-break | the best-value executor gets moderately worse halfway, staying above one half |
| cost-break | the best-value executor's cost rises fivefold halfway |
| clustered-hard | runs of hard items in each kind, so failures cluster for everyone |
| churn | families release versions and retire old ones; a quarter of releases are much worse than their family |
| slow-compound | two kinds give verdicts 150 to 300 steps after the close, failures there cost more the longer they wait, skill there is only loosely related to skill elsewhere, and cheap bad newcomers arrive |
| steady-projects | three projects at the same load |
| capability-gaps | skill varies strongly by kind |
| fabricators | two cheap executors that usually fail disguise 90% of their failures as successes, found out 30 to 150 steps later |
| slip | the best-value executor commits an integrity defect, caught in review, on 2% of its attempts |
| cheap-lead | the lead is cheap enough to be the best choice for many items |
| lead-undercount | the lead's cost is measured at 70% of its true value |

`sweep` repeats every scenario across population shapes:
- the default (15 executors);
- 4 or 40 executors;
- a tight cost-quality frontier, where cost follows skill closely;
- large capability gaps;
- newcomers whose price looks 2.7 times cheaper than it is;
- corrections four times as costly;
- a base rate near 62%.

## Policies

The `proposed` row in the version 3 outputs (source hash cfa48e2be8c8e66e):
- minimizes exact expected cost to complete;
- shares evidence across kinds and families;
- uses a priced cost prior and draws costs from their posterior;
- treats the lead as an arm;
- starts epochs on detected changes in success and cost;
- treats integrity defects as failures with their damage learned;
- has no exposure rule, drop, calibration or warm-up.

The design adopted from those results differs in two choices, each within 0.025 per item of `proposed` in every scenario: it uses cost per standing result (the `ratio objective` row) and prices integrity defects (the `integrity: head` row).

`today` is delegate's current workflow:
- cost per standing result with a flat prior per executor and kind;
- warm-up and the first-verdict hold;
- the drop rule and the drop against the lead;
- dropping an executor in a kind on an integrity defect;
- hand-built calibration in slow kinds;
- the lead only as a fallback.

Every other row changes one mechanism of `proposed`. Calibration, where used, replays the kind's own resolved items (six, from the most recent), so it cannot start until a slow kind has resolved some. The measured variant gates kinds whose mean lag from close to verdict exceeds 50 steps. The declared variant is told which kinds are slow. The evidence variant records the runs without gating.

The shared prior is a two-level stand-in for the design's mixed model. It is the kind's observed rate, shifted by the executor's record in its other kinds (or its family's, when it has none), with a prior worth four tasks.

## Output columns

- `regret/item`: mean over worlds, with a 95% interval.
- `p95`: the 95th percentile world.
- `vs proposed`: the paired difference from `proposed`, with a 95% interval.
- `tries`: attempts per work item.
- `bad%`: share of attempts given to an executor whose success rate in that kind is below one half.
- `lead%`: share of attempts the lead did.
- `broken`: attempts given to an executor while broken.
- `flawed`: attempts given to fabricators or the slipping executor.
- `blind`: attempts given to bad newcomers before their first verdict.
- `fstop`: drops or suspensions of executors whose true success rate was at least 0.75 and that never fabricate or slip.
- `susp`: suspensions.
- `freset`: epochs started away from any real break or recovery.
- `calib$`: cost of calibration runs.
- `wait`: steps that work items spent waiting for an actor.

## Limits

- The worlds come from this generator, not from measurements. Load, base rate and executor count resemble w3sim; the cost-skill relation does not include free executors.
- Outcomes are pass or fail. Scored and yield kinds, pool quotas and capacity other than the lead's are not simulated.
- Compounding is a failure cost that grows with time unverified, not a dependency graph of tasks built on one another.
- The shared prior is a simplification of the mixed model, with independent draws rather than joint ones.
- The oracle knows each item's hardness, which no policy can. This adds the same irreducible regret to every policy.
