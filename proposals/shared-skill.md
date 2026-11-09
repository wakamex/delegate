# One skill model shared across projects and kinds

Status: end-state design, sixth draft, 2026-11-08, implemented in delegate 0.2.0. It revises the fifth draft from the simulator's third version ([../sim/](../sim/README.md)), which added retries, after reviews by Claude Opus 5.5 and Gemini 4 Argon. The sources are listed at the end.

## Problem

delegate learns each executor separately in every project and kind, and starts every newcomer at a 50% chance of success where about 90% of tasks stand. A model's record in one project tells delegate nothing in another, and a new effort level of a known model starts from nothing. At heavy use, with many projects, thousands of tasks a month and model or harness updates every few weeks, cold start is the normal state, not an initial one.

## Principles

- A few general mechanisms, each declared as data, instead of named terms and special rules.
- Exploration comes from one source, the Thompson draw, and nothing but a person stops an actor. Every risk is a cost the model learns and the choice weighs.
- Consequences are measured, and the consumer gets no setting without a demonstrated need. Nothing about a kind's worth is declared or derived: the choice minimizes cost per standing result, which needs no worth.
- Every choice records what was predicted and how likely each eligible executor was to be chosen, so the model and the policy can be scored on the tasks already routed.

## Declared

The host roster declares each actor's attributes: family, model, effort, harness, pool, capabilities and price index. The lead is in the roster too, as the model and harness it runs on, so the model predicts its cost and success in every kind before it has done any. A project declares its kinds: check, classes (optional; a kind without them is one class), value measure, and required capabilities and resources. Each task carries its project, kind and class, and delegate records the rest itself: a retry when `select --retry` names the failed task, and the cause of a hand assignment, chosen from a short list (difficulty, quota, capability, user) when `--assign` is used.

## The model

### One term table

Every effect in every outcome is generated from one table of attribute tuples, each with a prior spread:

| Tuple | Captures |
|---|---|
| family; model; family × effort; harness; harness × version; actor; actor × epoch | an actor's skill, with new models, efforts and versions inheriting from their parents; effort and version are ordered, each level starting from the one before it; an epoch is a detected change, inheriting the same way |
| check; project; project × kind; project × kind × class | task difficulty |
| actor × check; actor × project; actor × project × kind; actor × project × kind × class | an executor's idiosyncrasies |
| retry; assigned for difficulty | how the task came to be, which otherwise biases whoever gets hard tasks |

Three generic rules replace every special case:
- A tuple with a missing attribute emits nothing for that task.
- A level identical to its parent on the data is dropped until a second sibling appears. A single-class kind or a single-version harness adds nothing.
- Prior spreads are scaled so that the total spread of an executor in a new cell is about 0.7 on the logit scale, and about 1.0 on the log scale of the cost heads. The task-history flags are fixed effects outside that budget. Adding attributes then never pulls newcomers away from the base rate.

Adding an attribute is one row in the table, once a ledger shows it matters.

### One engine, a few heads

One fitting engine serves every outcome: a sparse generalized linear mixed model, fitted by penalized Fisher scoring with a Laplace approximation. Each spread is updated in closed form with an inverse chi-square prior (scale from the table, 4 degrees of freedom): the update cannot reach zero and holds a level with few groups near its prior, with no special rule. Evidence is not decayed with age; a change in an actor is detected and starts a new epoch instead (see Exploration and trust).

The heads, each a link and a family over the same table:

| Head | Fitted on | Family |
|---|---|---|
| q: the result stood (held or steered) | tasks with a verdict | Bernoulli, logit |
| v: value, given it stood | tasks that stood | per the kind's measure: 1 for pass-fail; a 0 to 1 score (1 = everything the brief asked) as a fractional logit; yield as a count with dispersion (quasi-Poisson) |
| A: actor cost | priced runs, with log price index as an offset, and its dispersion | Tweedie, log link; the dispersion gives the capacity gate its upper quantile |
| B, L: the lead's cost before launch (brief) and after (review, and correction when it did not stand) | tasks with lead cost, split at the started time | Tweedie, log link; L with a non-negative increment for failure |
| T: minutes the task holds its resources | closed tasks, and its dispersion | Tweedie, log link |
| D: an integrity defect (a fabricated or misreported result, found in review or later) | tasks with a verdict | Bernoulli, logit; its expected cost is the chance times the rework and re-check it causes |

Tweedie handles $0 costs and predicts the mean directly. A run whose model has no price is left out of A and predicted from the shared terms; a run that costs nothing, on a free pool, is recorded as $0 and its pool's scarcity prices it. Aborted tasks enter no head, but their lead turns are charged to the kind's brief cost B, so frequent aborts make delegating dearer.

### What the lead's turns cost

A task's lead cost is every lead turn whose triggering message names the task, its agent-job unit or its aop run, from its opening to its close, split at the started time into brief and post-run. Turns before the start can name only the task, since the unit and run do not exist yet. A correction after close is an amend of the task's lead cost. On w3sim's lead session from 2026-11-04 to 2026-11-08, 9.5% of the cost ($19.83 of $208.52) was charged to no task; that is 1.7 times the measured correction premium, so its breakdown by category is the next measurement.

## Capacity

Every limited thing is a capacity: a pool window, a pool's `runs` cap, a shared machine, a checkout, an amount of memory. Each has a reading, the runs open on it across the host, and a health state. One layer does two things:
- Gate: a task starts only if every capacity it needs fits, at the 90th percentile of its predicted use (pool points from A, minutes from T), with what is open. A pool whose run size is unmeasured takes one run at a time. A quota stop blocks the pool until its reset; an outage or capacity error blocks it with backoff.
- Price: a scarcity multiplier per pool, 1 for pay-per-token pools; for subscriptions 0.05 when the window's quota will expire unused, 1 at steady pace, and rising above 1 as pace binds. The floor keeps relative prices meaningful on spare quota, so haiku still beats opus at equal quality. Records stay in stationary dollars.

## Choosing

For each eligible actor e, the expected cost of each result that stands, per unit of value:

    unit_e = [ s_e * A_e + s_lead * (B + L_e(stood) + (1 - q_e) * increment_e) + D_e ] / (q_e * v_e)

The task goes to the actor with the lowest drawn unit cost.
- q_e, v_e: the chance the result stands, and its value given it stands (1 in pass-fail kinds).
- A_e, B, L_e, increment_e: actor cost, the brief, the lead's review, and its extra cost when a result fails. For the lead itself, A is its own turns, B and L are zero, and its failures cost it their correction like anyone else's.
- D_e: the expected cost of integrity defects, from the D head.
- s_e, s_lead: the scarcity multipliers of the executor's and the lead's pools.
- Minutes held (T) carry no price: a resource below its capacity costs nothing, and T acts only through the capacity gate.

This is the expected cost to finish the task with e when a failure is redone: the workflow sends a failed task to someone else, so a failure's true cost is the next attempt, and dividing by q counts those attempts. It needs no worth. In the simulator with retries, it was within 0.02 per work item of the exact cost-to-complete objective in every scenario, while valuing results at the lead's cost per unit, as the fifth draft did, was 0.02 to 0.27 worse everywhere. The fifth draft's net value objective and its derived worth are gone.

The lead being an arm replaces WORKFLOW's drop-against-the-lead and kind-off rules: the lead wins where it is cheapest per standing result, and stops winning when its pool binds. The lead's cost comes from its own priced roster entry until it has records, so this works with no lead tasks in the ledger.

Mechanics:
- Thompson sampling: draw the joint posterior of the eligible actors' predictors for each head, compute each unit cost, and take the lowest.
- The selection probability of every eligible actor comes from 256 draws plus the draw that chose, with a floor of 1/257, so no chosen actor is recorded at zero and offline evaluation never divides by zero.
- `select` reads a cached fit under the lock, refitting it when an outcome has changed since. The cache is derived and can be deleted.

## Exploration and trust

Thompson sampling chooses. Two general mechanisms keep its estimates honest, and everything else is a cost in the objective. Each was tested against the alternatives in the simulator; the next section has the numbers.

- No exposure rule and no stop. There is no warm-up, no first-verdict hold, no cap on unverified work, no drop rule and no calibration gate. A low success rate raises an actor's cost per standing result, so an unreliable executor loses the draw by itself. Every exposure rule tested cost more than it saved, most when verification is slow, where it throttles the good executors along with the uncertain ones.
- Epochs. For each actor, a Bayesian changepoint detector watches two signals: its results, each judged against what the model predicted for it, adjusted for how the kind is going for everyone lately; and its costs at close, against its usual cost. A shift in either direction that is more than 95% likely starts a new epoch, a child level of the actor in the term table, like a new harness version: the actor's estimate restarts from its old value with the weight of a few tasks, while its old results stay in the shared terms for its family and kinds. A broken executor, a fixed one and a repriced one are all re-learned the same way. The detector watches actors with at least 20 verdicts.
- Integrity as a cost. An integrity defect, a fabricated or misreported result, whether caught in review or found later, enters the D head. Its expected cost, rework and re-check, raises the actor's cost per standing result. An actor that slips occasionally keeps working; one that fabricates often prices itself out. There is no automatic suspension. w3sim recorded 6 integrity corrections in about 250 closes in 10 days, across four executors, and suspending each one everywhere would have stopped good executors far more often than it caught bad ones.
- Late verdicts. A kind whose verdict comes long after the work records it as a verdict event; the success head counts a result as standing only once its final verdict says so, and the failure cost it learns includes what compounds while the result waits. Nothing else is special about such a kind. With retries, calibration gates for slow kinds cost more than they saved in every variant tested (replaying the kind's own resolved tasks, a declared gate, calibration as evidence only), so there is no calibration and no input snapshot to keep.

A person can still suspend or resume an actor with a reason, as a ledger event.

### Simulated evidence

The simulator ([../sim/](../sim/README.md); outputs in [../sim/results/](../sim/results/), source hash cfa48e2be8c8e66e) compares policies on generated worlds where every actor's true success rate and cost are known. Every work item must be completed: a failed attempt is retried by a different actor. Regret is the expected cost to complete each attempt above the best choice, plus waiting, calibration and re-check costs, per work item. Policies learn costs, including the lead's from its priced guess and its measured, possibly undercounted, turns; nothing is given to them exactly.

Each mechanism was changed one at a time from the proposal, in 15 scenarios at 300 worlds each. The scenarios are:
- the baseline (one project of five kinds, about 6 attempts open at once, as in w3sim);
- cheap executors that usually fail;
- a veteran whose skill collapses, collapses and recovers, worsens moderately, or whose cost rises fivefold;
- runs of hard tasks;
- model churn with a quarter of releases bad;
- slow, compounding kinds with unpredictable skill and cheap bad newcomers;
- three projects at the same load;
- capability gaps across kinds;
- fabricators;
- an executor that slips on 2% of its tasks;
- a cheap lead;
- a lead whose cost is measured 30% low.

Regret per work item compared with the proposal; positive is worse, and the range is across the scenarios where the mechanism matters:

| Change from the proposal | Effect | Decision |
|---|---|---|
| Today's whole workflow | 0.03 to 0.27 worse in 14 of 15 scenarios; 0.04 better with fabricators, where it suspends them | replaced |
| Valuing results at the lead's cost per unit (the fifth draft) | 0.02 to 0.27 worse in every scenario | not used |
| Valuing results at the cheapest actor's cost per unit; cost per standing result | within 0.025 of exact cost to complete, slightly better in most scenarios | equivalent; cost per standing result is the simplest |
| No change detection | 0.21 worse when a veteran breaks, 0.15 when it breaks and recovers, 0.14 when its cost breaks; within 0.015 elsewhere | epochs used |
| The earlier discount, success only | 0.14 worse on cost breaks, 0.02 on skill breaks | epochs used |
| Suspending on integrity defects, everywhere or in the kind | 0.06 to 0.08 better with fabricators; 0.23 to 0.33 worse when a good executor slips | not used |
| Pricing integrity defects (the D head) | 0.09 better with fabricators; 0.02 worse with slips; up to 0.02 better elsewhere | used |
| Calibration gate in slow kinds: measured, declared, or evidence only | 0.09, 0.36 and 0.01 worse | not used |
| First-verdict hold, host-wide cap, open-task fantasy draws | 0.21, 0.78 and 0.10 worse in slow kinds; within 0.01 elsewhere | not used |
| No evidence shared across kinds and families | up to 0.33 worse, in every scenario | sharing used |
| Warm-up | 0.04 to 0.16 worse in every scenario | not used |
| The lead only as a fallback | 0.11 worse with a cheap lead; neutral elsewhere | the lead is an arm |
| Today's drop rule | within ±0.005 | not used |

The same comparisons were repeated across 8 population shapes at 150 worlds each (the default; 4 or 40 executors; a tight cost-quality frontier; capability gaps; newcomers that look 2.7 times cheaper than they are; corrections four times dearer; a 62% base rate), 120 scenario-population cells per mechanism. The decisions hold in every shape:
- Cost per standing result was significantly better than exact cost to complete in 60 cells and worse in 1, by at most 0.015.
- Change detection saved 0.06 to 0.56 per item when an actor broke or its cost jumped, most with 4 executors, and cost at most 0.015 elsewhere, except 0.09 with fabricators among 4 executors.
- Pricing integrity defects saved 0.04 to 0.30 with fabricators and cost 0.01 to 0.04 with slips. Suspension, everywhere or in the kind, cost up to 1.0 per item with slips among 4 executors.
- Sharing evidence was never significantly worse and was better in 91 of 120 cells. Warm-up was worse in 109.
- The exposure rules were worse in slow, compounding kinds in every shape: the host-wide cap by up to 2.8.

One result goes the other way, and it is the one open case. The calibration gate saved 0.20 per item in slow, compounding kinds when corrections cost four times as much, and 0.04 with only 4 executors, while costing up to 0.45 elsewhere. A gate may pay where a late failure is very expensive. If a project's ledger shows such a kind, the gate is the mechanism to test against it, as a cost of calibration weighed against the expected cost of failures still waiting for a verdict, not as a rule.

Both outputs are in [../sim/results/](../sim/results/).

The simulator's worlds are generated, not measured. Its README lists what it does not model, chief among them errors that compound through tasks built on one another.

## Recorded

- open: policy version and fit snapshot; retry and assignment cause; the seed; for every eligible actor, the mean and standard deviation of its cost per standing result and its selection probability.
- started: the harness version.
- close: tokens by type (uncached input, cache reads, cache writes, output), score on 0 to 1, yield, actor cost, and lead cost split at the started time.
- verdict and overturn: a kind's final verdict, when it comes after the close, and a later finding that a result did not stand. The success head counts a result as standing only once its final verdict says so.
- integrity defects, at close or found later; epochs; suspend and resume by a person.
- The lead's own tasks, as tasks.

## Measured offline

Prequential scores and calibration per head, with an executor's first tasks in a cell as a separate slice; doubly robust off-policy evaluation of other configurations, using the recorded selection probabilities; the scorecard's frontier view of each actor's cost against its success. Concord's 151 imported tasks enter as one batch. None of these switch anything at runtime.

## Code

delegate 0.2.0 implements this design in five modules:
- `config`: the host roster and project configs, and each model's family;
- `ledger`: the events, folded per task, with standing, integrity and the lead-cost split;
- `model`: the term table and its rules, and one fitting engine for logit and fractional logit, Tweedie and quasi-Poisson counts with dispersion, with the closed-form spread updates and joint draws;
- `epochs`: the changepoint detector on results and costs;
- `policy`: the world of every host project's ledger, the heads, capacity, eligibility, cost per standing result, the Thompson choice with selection probabilities, epochs and the scorecard.

It needs NumPy only: the design matrices are dense, which at a few thousand tasks fits in milliseconds. The fit is cached by a hash of the configs and of every outcome in the ledgers, and refitted by the first `select` after one changes. Three parts are not built yet: incremental folding from ledger offsets, sparse matrices, and the offline evaluation report on the recorded selection probabilities. Each waits until a ledger is large enough for its absence to show: a `select` slower than a second, or enough routed tasks to score the policy.

## What it replaces

Warm-up, per-class priors, per-kind Beta counts, three separate outcome models (now one value head per measure on the shared table), the ban on cross-project evidence, the expiring-quota discount and `need`, the first-verdict hold and any cap on unverified work, every drop rule, drop-against-the-lead and kind-off, age decay of evidence, calibration and admission, and integrity suspension. What stays: append-only ledgers per project as the only state, cost per standing result as the objective, final verdicts and overturns, `suspend` and `resume` by a person, and `runs` for limits nothing measures.

## Sources

| Need | Source |
|---|---|
| Skill as a sum of uncertain terms, results as matches | TrueSkill (2006); Bradley-Terry with random effects (2012) |
| Borrowing across modes | TrueSkill 2 (2018): one of five changes that together raised Halo 5 prediction from 52% to 68% |
| Difficulty learned from everyone | item response theory, tinyBenchmarks (2024) |
| Spread priors for few groups | Gelman (2006) |
| Joint draws from a hierarchical posterior | HierTS (2022); linear Thompson sampling (2013) |
| Late verdicts and concurrent draws | Chapelle and Li (2011); Thompson sampling tutorial (2018) |
| Routing with caps, budgets and new models | StageRoute (2025) |
| Change over time | TrueSkill Through Time (2007, 2025) and Glicko-2 model drift as a continuous process; this design treats change as discrete: declared versions, and detected epochs, are new levels |
