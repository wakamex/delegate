# Ledger and config format

This is the format the `delegate` helper reads and writes for [the delegation workflow](WORKFLOW.md). A project keeps one config file and one ledger. Everything the workflow computes (counts, expected costs, the draw, the drop rule, the scorecard) is recomputed from the ledger on each call; nothing else holds state.

## Config

`delegate.toml` at the project's root. It holds only what the project declares; anything that changes over time is a ledger event.

```toml
project = "sim"
ledger = "tmp/team/delegate.jsonl"      # relative to this file
lease_minutes = 30                       # an open with no started event is closed as aborted after this
wait_deadline_minutes = 240              # after this a waiting task goes to the lead if its pool has room

[lead]
pool = "claude/lead-account"             # pool-check target of the account the lead runs on
session = "0123abcd"                     # the lead's session id, a default for lead cost
grain = "task"                           # "task", or "slice" for one record per committed slice

[resources]
checkout = ["sim-a", "sim-b", "sim-c", "sim-d"]  # named instances, one task each
live-client = 2                          # or a count of interchangeable slots
remote-host = 1
host-memory-gb = { capacity = 31 }       # or a capacity in units, reserved as amounts

[pools.devin]
need = 40                                # for a pool with no measured price, the points of each window a run takes

[executors.terra-max]
agent = "codex"
model = "gpt-6.1-terra"
effort = "max"
pool = "codex"                           # usage-burn target
capabilities = ["sealed-inputs", "no-web"]

[kinds.port]
check = "reviewed"                       # "gated", "reviewed" or "deferred"
state = "on"                             # "on" or "off"
measure = "lead-score"                   # "pass-fail", "yield" or "lead-score"
classes = ["bounded", "first-reading", "redesign"]
class_state = { redesign = "lead-only" } # overrides of the kind's state per class
capabilities = []                        # every executor here must have these
resources = ["checkout", "live-client"]  # what a task of this kind reserves by default; "host-memory-gb=16" reserves an amount
references = { "port-0012" = 30, "port-0031" = 60, "port-0044" = 90 }  # lead-score reference tasks
investigation = false                    # true for a kind of bounded investigations
```

`[candidates] families` is an optional list of regular expressions matched against model ids: `delegate candidates` lists only the unconfigured models that match one, so a lead sees the families worth trying rather than every model and effort aop offers. For example `"^(.*/)?gpt-6-luna"` matches codex's `gpt-6-luna` and opencode's `openai/gpt-6-luna` but not `gpt-5.6-luna`.

`check`, `state`, `measure` and `classes` are required for every kind, and every table rejects keys it does not define, so a misspelt key is an error rather than a silent default.

The pool check takes a run's expected dollars and converts them to each window's points itself, at the dollars per point clanker-analytics measures for that window. `[pools.NAME] need` is for a pool it has no measured price for, such as Devin's: the points of each window a run takes, from how far past runs moved it (in w3sim one Kimi K3 run on Devin used 43 points of its week), counted for the new run and for each of the project's runs already going on the pool. Defaults: `lead_refresh` is true for gated kinds and false otherwise, `awaiting_cap` is 1, and the other fields are empty.

## Ledger

One JSON object per line, appended under an exclusive lock and never edited. Every event has:

| Field | Meaning |
|---|---|
| `schema` | `"delegate-event-1"` |
| `event` | the event type, below |
| `at` | ISO 8601 time with offset |
| `task` | the task id, for task events |

A task's record is its events folded in order: later fields replace earlier ones, and an overturn changes the task it names.

### Task events

`open`, written by `select` for every answer but wait:

| Field | Meaning |
|---|---|
| `kind`, `class` | from the config |
| `actor` | an executor name from the config, or `"lead"` |
| `reason` | `warm-up`, `draw`, `cheapest`, `catch-up`, `admit`, `assigned`, `lead` |
| `seed` | the draw's seed, for `draw` |
| `why` | for `assigned`: who chose the executor and why, in the lead's words |
| `retry_of` | the task this one retries, if any |
| `resources` | what is reserved: `{NAME: INSTANCE}` for a resource with named instances, `{NAME: 1}` for a counted one, `{NAME: AMOUNT}` for one with a capacity in units. A task is admitted only while every reserved amount fits under its capacity, which is how a project keeps two 16 GB jobs off a 31 GB host at once |
| `token_cap` | the brief's cap |
| `expected` | `{"actor_cost": .., "lead_cost": ..}` at selection; older events may carry `pool_points` |
| `check`, `cases` | deferred kinds: the authoritative check that will close it and what it must cover |

`started`: what runs the task: `aop_run` (its id) and `units` (agent-job unit names). An aop run inside an agent-job unit records both, since the lead's turns are triggered by messages naming the unit.

`close`, once the result has landed or been discarded:

| Field | Meaning |
|---|---|
| `outcome` | `held`, `steered`, `corrected`, `aborted`, or `awaiting` in a deferred kind |
| `caught_by` | for corrected: `gate`, `review` or `deferred-check` |
| `defect` | for corrected: `integrity`, `scope` or `failed-gate` |
| `brief_error` | the decision the brief left out, if the lead's |
| `resumes` | resumes in the session |
| `cost` | `{"actor": .., "lead": ..}` in API-equivalent dollars; `null` where unknown |
| `score` | lead-score kinds: `{"value": 0..100, "reason": ".."}`, given before the lead reads the cost |
| `yield` | yield kinds: the count from the reference check |
| `minutes` | `{"run": .., "held": {RESOURCE: ..}}` |
| `aop_runs` | every aop run id of the task |
| `commits` | the commits the task landed, which the overturn rule needs to find the root task |
| `claims` | optional claim ids the task touched, such as a reverse-engineering project's claim ids, by which an overturn is traced back |
| `evidence` | optional: the primary artifacts the result rests on, a list of `{"path": .., "sha256": ..}` or free references. Required for investigations |
| `falsifier_checked` | investigations: whether the falsifier named in the brief was checked, `true` or `false` |
| `report` | the path to the executor's report, so the score and judgement can be traced |

`verdict`, deferred kinds: `outcome` (`held` or `corrected`), `score` or `yield`, `evidence`, the run of the authoritative check that decided it (such as a block height and a fixture hash), and `unresolved`, the branches left out. The evidence is what lets a later reader confirm that the open's `cases` were covered.

`overturn`: `root` (the task whose commit introduced the overturned code), `redo_cost`, `reason`. Folding sets the root's outcome to corrected and its score and yield to zero.

### Lead cost

`close` fills `cost.lead` for the lead session given by `--lead-session`, else the `DELEGATE_LEAD_SESSION` environment variable, else `[lead] session`; a lead's session id changes whenever it starts afresh, so the config value is only a default. delegate decides which turns are the task's: every lead turn whose triggering message names one of the task's agent-job units or aop runs as a whole token, and its part of the turn in which `select` opened it: a turn that opens several tasks is split at their openings, each taking the responses from the previous opening, or the turn's start, up to its own, which hold its brief, and the last one also the rest of the turn, which holds its launch, so no response is charged to two tasks. A turn whose trigger names no task, such as a user's message, is charged to no task. The turns come from `sch -s SESSION -t user --json`, which reads the user messages of every harness it supports, and each response's cost from `clanker-analytics session-costs`, which deduplicates and prices them, so delegate reads no transcript and does no pricing of its own. When either cannot be had, the lead cost is `null`.

### Actor and kind events

`suspend`, `resume`, `drop`: `actor`, `kind`, `reason`.


## Commands

| Command | Writes | Does |
|---|---|---|
| `delegate select KIND CLASS [--resources R[=AMOUNT],..] [--token-cap N] [--retry TASK] [--admit EXECUTOR] [--assign EXECUTOR --why ..] [--since TIME]` | `open`, except for wait; `drop` when the drop rule fires | prints `delegate`, `admit`, `wait` or `lead` with the actor, task id, reserved instances (such as the checkout to pass as aop's `--workdir`) and reason, as one JSON line. A wait answer carries `since`; passing it back as `--since` on the next try is how the wait deadline is measured |
| `delegate started TASK [--aop-run ID] [--unit NAME..]` | `started` | |
| `delegate close TASK --outcome ..` | `close` | fills `cost` from aop's archived results and the lead's transcript |
| `delegate verdict TASK --outcome ..` | `verdict` | |
| `delegate overturn ROOT --redo-cost N --reason .. [--found-by TASK]` | `overturn` | ROOT is the task whose commit introduced the overturned code |
| `delegate suspend\|resume\|drop ACTOR KIND --reason ..` | the event | |
| `delegate scorecard [KIND]` | nothing | prints each actor's cost per success or per unit, successes out of verdicts, score spread, integrity corrections, held minutes and open tasks |
| `delegate candidates [--agent A..] [--all-agents] [--any-family] [--limit N]` | nothing | lists the models aop offers that the project has not configured as executors, for the agents it runs and the families its config names, priciest first, with each agent's pool room |
| `delegate reap` | `close` with `aborted` | closes every open with no started event within the lease as aborted; every other command runs it first; a running task keeps its reservation until the lead closes it |

Every command finds `delegate.toml` by walking up from the current directory, or takes `--config PATH`.
