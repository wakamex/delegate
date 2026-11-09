# Ledger and config format

This is the format the `delegate` helper reads and writes for [the delegation workflow](WORKFLOW.md). A project keeps one config file and one ledger. Everything the workflow computes (the model, the draw, epochs, the scorecard) is recomputed from the host's ledgers; nothing else holds state. The fitted model is cached under `$XDG_CACHE_HOME/delegate`, keyed by a hash of the configs and of every outcome in the ledgers, and can be deleted.

## Config

Two files. The host roster declares everything shared by the host's projects: its actors, the lead among them, its pools' limits, the machines several projects share, and the list of project configs, whose ledgers the model reads. Each project's `delegate.toml`, at its root, declares only the project: its kinds, its own resources and how runs are launched. Anything that changes over time is a ledger event.

### Host roster

`$XDG_CONFIG_HOME/delegate/host.toml` (`~/.config/delegate/host.toml`), or the file `DELEGATE_HOST` names. Without one, the lead is the only actor. [examples/host.toml](examples/host.toml) has one.

```toml
projects = ["/code/sim/delegate.toml", "/code/concord/delegate.toml"]  # every project whose ledger the model reads

[lead]                                   # the lead, an actor like any other
agent = "claude"
model = "claude-opus-5-5"
pool = "claude"                          # pool-check target of the account the lead runs on
price = 4.0                              # the typical cost of one task's turns, the cost prior before it has records
runs = 1                                 # tasks the lead takes at once (default 1)

[actors.terra-max]
agent = "codex"
model = "gpt-6.1-terra"
effort = "max"                           # low, medium, high, xhigh or max; each effort starts from the one below it
pool = "codex"                           # usage-burn target
price = 2.0                              # the typical API-equivalent cost of one run, above 0: the cost prior before it has records
family = "gpt-terra"                     # optional: by default the model id without provider, versions and efforts
capabilities = ["sealed-inputs", "no-web"]

[pools.devin]
runs = 2                                 # the most tasks open on the pool at once, across every project

[resources]
host-memory-gb = { capacity = 31 }       # a machine several projects share, counted across all of them
```

A pool's `runs` caps tasks open on it across the host, for a limit its reading cannot show: Devin's free-model rate limit is one rolling allowance shared by its SWE-2 models that leaves the account's day and week at 0%, and in w3sim four overlapping runs brought lockouts of 2 hours 12 minutes and 5 hours 20 minutes. A task counts from its open until its close. An actor without a price is priced at $3 until it has records.

### Project config

```toml
project = "sim"
ledger = "tmp/team/delegate.jsonl"      # relative to this file
lease_minutes = 30                       # an open with no started event is closed as aborted after this

[lead]
session = "0123abcd"                     # the lead's session id, a default for lead cost

[resources]
checkout = ["sim-a", "sim-b", "sim-c", "sim-d"]  # named instances, one task each
live-client = 2                          # or a count of interchangeable slots
remote-host = 1

[launch]                                 # how delegate run starts an executor's aop run
workdir = "/code/{checkout}"             # aop's --workdir, from the task's reserved resources
directory = "tmp/team"                   # where aop runs, relative to this file (default ".")
profile = "host"                         # aop's --profile (default "host")
timeout_minutes = 210                    # aop's --timeout; the agent-job unit gets 30 minutes more
memory = "12G"                           # the agent-job unit's memory cap

[kinds.port]
check = "reviewed"                       # "gated", "reviewed" or "deferred"
measure = "lead-score"                   # "pass-fail", "yield" or "lead-score"
classes = ["bounded", "first-reading", "redesign"]  # optional; a kind without them has one class, "any"
capabilities = []                        # every actor here must have these
resources = ["checkout", "live-client"]  # what a task of this kind reserves by default; "host-memory-gb=16" reserves an amount, "live-client=2" two slots
investigation = false                    # true for a kind of bounded investigations
```

`check` and `measure` are required for every kind, and every table rejects keys it does not define, so a misspelt key is an error rather than a silent default. A kind may reserve the host's shared resources too; a project may not declare one under the same name. Keys a config may carry from an earlier version are rejected with a message saying where each went: `[executors]`, `[pools]`, `[candidates]` and `[lead] pool` moved to the host roster; `[kinds.NAME] state` and `class_state` are gone because the lead competes in every draw; `wait_deadline_minutes` is gone because a task waits only for capacity; and `need`, `dollars_per_point`, `need_short`, `[lead] grain` and `[kinds.NAME] references` are gone.

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
| `actor` | an actor name from the host roster, or `"lead"` |
| `reason` | `draw` or `assigned`; older events may say `warm-up`, `cheapest`, `lead`, `admit` or `catch-up` |
| `seed` | the draw's seed |
| `predictions` | for `draw`: each eligible actor's predicted cost per standing result (`unit`), its spread (`sd`) and the share of 257 posterior draws in which it was lowest (`p`) |
| `expected` | the chosen actor's predicted chance of standing (`q`), value (`value`), actor cost (`cost`), integrity defect chance (`integrity`), and for an executor the lead's brief (`brief`), post-run cost (`post`) and extra cost when it fails (`increment`); events before November 2026 hold `actor_cost` and `lead_cost` |
| `policy`, `fit` | delegate's version and the hash of the fitted model the choice used |
| `scarcity` | the actors whose pool was priced below 1 for quota due to expire, with that price |
| `refused` | the actors whose pool had no room, with the check's answer |
| `account` | for an executor whose pool has several accounts, such as Claude's: the account the pool check chose for the run to land on, by the name usage-burn gives it (`account_name`), which the launcher applies; the answer carries it too |
| `why`, `cause` | for `assigned`: who chose the actor and why, in the lead's words, and the cause: `difficulty`, `quota`, `capability` or `user` |
| `retry_of` | the task this one retries, if any |
| `resources` | what is reserved: `{NAME: INSTANCE}` for a resource with named instances, `{NAME: SLOTS}` for a counted one, 1 unless more were asked for, such as all of a live client a relaunch would end every job on, `{NAME: AMOUNT}` for one with a capacity in units. A task is admitted only while every reserved amount fits under its capacity, which is how a project keeps two 16 GB jobs off a 31 GB host at once; a host resource counts every project's tasks |
| `token_cap` | the brief's cap |

`started`: what runs the task: `aop_run` (its id) and `units` (agent-job unit names). An aop run inside an agent-job unit records both, since the lead's turns are triggered by messages naming the unit.

`close`, once the result has landed or been discarded:

| Field | Meaning |
|---|---|
| `outcome` | `held`, `steered`, `corrected`, `aborted`, or `awaiting` in a deferred kind |
| `caught_by` | for corrected: `gate`, `review` or `deferred-check` |
| `defect` | for corrected: `integrity`, `scope`, `failed-gate` or `unfinished` |
| `brief_error` | the decision the brief left out, if the lead's |
| `resumes` | resumes in the session |
| `cost` | `{"actor": .., "lead": .., "brief": ..}` in API-equivalent dollars, `brief` being the part of the lead cost spent before the task started; `null` where unknown. For the lead's own task, its turns are the actor cost |
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

`amend`: corrects a closed task's `cost.actor`, `cost.lead`, `cost.brief`, `minutes.run`, `aop_runs`, `commits`, `report`, `score.value` or `score.reason`, such as a cost or score mistyped at close. It holds `changes`, each field's path with its `old` and `new` value, `by` (who corrected it, `lead` unless named) and `reason`. Folding sets each field to its new value, so the scorecard and the model use it. An outcome is not amended: an overturn marks a success wrong later, and a deferred kind's verdict decides its outcome and holds its score. A score is given before the lead reads the cost, so an amend of it is for a slip, its reason saying what was mistyped, and the scorecard counts each actor's rescored tasks.

`overturn`: `root` (the task whose commit introduced the overturned code), `redo_cost`, `reason`, `found_by` and `defect`, `"integrity"` when the result was fabricated or misreported. Folding sets the root's outcome to corrected and its score and yield to zero.

### Lead cost

`close` fills `cost.lead` for the lead session given by `--lead-session`, else the `DELEGATE_LEAD_SESSION` environment variable, else `[lead] session`; a lead's session id changes whenever it starts afresh, so the config value is only a default. delegate decides which turns are the task's: every lead turn whose triggering message names the task id or one of its agent-job units or aop runs as a whole token, and its part of the turn in which `select` opened it: a turn that opens several tasks is split at their openings, each taking the responses from the previous opening, or the turn's start, up to its own, which hold its brief, and the last one also the rest of the turn, which holds its launch, so no response is charged to two tasks. A turn whose trigger names no task, such as a user's message, is charged to no task. Only turns from the one that opened the task to its close count, so tasks that reuse an agent-job unit's name, as a retry of the same work often does, are not charged each other's turns. The turns come from `sch -s SESSION -t user --json`, which reads the user messages of every harness it supports, and each response's cost from `clanker-analytics session-costs`, which deduplicates and prices them, so delegate reads no transcript and does no pricing of its own. The responses before the task's started event are its brief, `cost.brief`. When either tool cannot be had, the lead cost is `null`.

### Actor events

`suspend` and `resume`: `actor`, `reason`, and `kind` when it applies to one kind of the project only. A suspend takes the actor out of every draw it applies to until a later resume; a person suspends an actor whose provider has locked it in a way the pool check cannot see. Older ledgers may hold `drop`, which stops an actor in a kind like a suspend.

`epoch`: `actor`, `signal` (`success` or `cost`) and `observations`, written by `close`, `verdict` or `overturn` when the actor's results since its last epoch show a change more than 95% likely. The model then treats the actor's later tasks as a new level that starts from its old estimate.


## Commands

| Command | Writes | Does |
|---|---|---|
| `delegate select KIND CLASS [--resources R[=AMOUNT],..] [--token-cap N] [--retry TASK] [--assign ACTOR --why .. [--cause C]] [--since TIME]` | `open`, except for wait | prints `delegate`, `wait`, `lead` or `error` with the actor, task id, reserved instances (such as the checkout to pass as aop's `--workdir`) and reason, as one JSON line. A wait answer carries `since`, to pass back as `--since`. An `error` answer is a wait in which every pool check failed to answer, and exits 2 |
| `delegate run TASK --prompt-file PATH` | `started` | starts the task's executor in aop as the agent-job unit named after the task: its agent, model and effort, `[launch]`'s workdir and profile, `--no-web` when the kind requires it, and the Claude home of the account the open names. The unit reports to the lead's route when it ends |
| `delegate continue TASK --prompt-file PATH` | `started` | resumes the task's latest aop run with a follow-up prompt, as unit `TASK-rN` on the same account |
| `delegate started TASK [--aop-run ID] [--unit NAME..]` | `started` | for a run started some other way; a later started event adds its units to the task's |
| `delegate close TASK --outcome ..` | `close`, and `epoch` on a change | fills `cost` from aop's archived results and the lead's transcript. The task's aop runs are those `started` and `--aop-run` name and those its units' agent-job logs name that started within the task's time, since aop gives a run its id only once it is going; `resumes` defaults to the units `continue` started |
| `delegate verdict TASK --outcome ..` | `verdict`, and `epoch` on a change | |
| `delegate overturn ROOT --redo-cost N --reason .. [--found-by TASK] [--defect integrity]` | `overturn`, and `epoch` on a change | ROOT is the task whose commit introduced the overturned code |
| `delegate amend TASK --reason .. [--actor-cost N] [--lead-cost N] [--brief-cost N] [--run-minutes N] [--aop-run ID..] [--commit SHA..] [--report PATH] [--score N] [--score-reason ..] [--from-aop] [--from-lead-session [--lead-session ID]] [--by WHO]` | `amend` | corrects a closed task's fields, recording each old and new value; `--from-aop` refills actor cost and run minutes from aop's records of its runs, and `--from-lead-session` its lead cost and brief from the lead's session; a list option replaces the recorded list |
| `delegate suspend\|resume ACTOR [KIND] --reason ..` | the event | an actor from the host roster, or `lead` |
| `delegate scorecard [KIND]` | nothing | prints each actor's record per kind: tasks, those still pending, verdicts, results that stood, integrity defects, epochs, rescored tasks, `spend` (actor and lead cost of its closed tasks, aborted and failed ones included) and `spend_per_stood`, with the model's current `q`, `cost` and `unit` (cost per standing result) |
| `delegate candidates [--agent A..] [--all-agents] [--limit N]` | nothing | lists the models aop offers that the host roster does not run, for the agents it runs, priciest first, with each agent's pool room |
| `delegate reap` | `close` with `aborted` | closes every open with no started event within the lease as aborted; every other command runs it first; a running task keeps its reservation until the lead closes it |

Every command finds `delegate.toml` by walking up from the current directory, or takes `--config PATH`.
