# delegate

A lead agent in a project can hand tasks to executor agents, or do them itself. `delegate` decides which: for each task it picks an executor that has earned the kind of task, by expected cost per successful task, while still trying executors whose record is thin, checks each executor's subscription pool has room, reserves what the task needs, and keeps an append-only ledger of how every task went, from which each later choice is computed.

- [WORKFLOW.md](WORKFLOW.md): the workflow three projects adopted, its rules and the evidence behind each.
- [FORMAT.md](FORMAT.md): the project config (`delegate.toml`), the ledger format and the commands.

## Install

```sh
uv tool install git+https://github.com/wakamex/delegate
```

It needs Python 3.12 or later and nothing beyond the standard library. It reads the run records of [aop (agent orchestration process)](https://github.com/wakamex/agent-orchestration-process) for executor costs, and takes the lead's turns from `sch` and its per-response costs from `clanker-analytics session-costs` in [clanker-analytics](https://github.com/wakamex/clanker-analytics).

Pools are checked by a command taking `--check TARGET [--cost DOLLARS] [--need POINTS]`, where `POINTS` is one number for every window or a list per window length such as `7d=25,30d=6`, that prints one JSON line with the pool's windows and exits 0 when the run fits, 1 or 3 when it does not, and 2 on an error. `delegate` uses `usage-burn` on `PATH`, or the command named by `DELEGATE_USAGE_BURN`; without one it checks no pool, and `select` says so in its output.

## Usage

Put a `delegate.toml` at the project's root; [examples/delegate.toml](examples/delegate.toml) has one. Then, for each task:

```sh
delegate select port bounded --resources checkout    # {"answer": "delegate", "actor": "sonnet", "task": "port-0007", "resources": {"checkout": "sim-a"}, ...}
aop run --agent claude --model claude-sonnet-5-5 --workdir ../sim-a ...
delegate started port-0007 --aop-run RUN_ID --unit job-port-7
delegate close port-0007 --outcome held --score 60 --score-reason "bounded port, confirmed live" --commit abc1234
delegate scorecard port
```

A `wait` answer names the busy resource or pool, and under `refused` each otherwise eligible executor its pool check turned away, with the dollars it asked for, the check's state and its reasons; a wait opens nothing, so it can be asked as often as needed. Try again later with `--since` set to the time it gave. A `lead` answer means the lead does the task itself, and records it the same way. Every command prints one JSON line and exits 0, or prints `{"error": ...}` and exits 2.

## Limitations

- Lead cost: the lead session, given with `close --lead-session`, `DELEGATE_LEAD_SESSION` or `[lead] session`, is read for any harness: its turns, the user messages that started them, from `sch` ([search-clanker-history](https://github.com/wakamex/search-clanker-history) 0.3.1 or later, which finds one session's messages in seconds and leaves out lines that start no turn), and each response's cost from `clanker-analytics session-costs`; delegate only decides which turns are the task's. Unit and run names are matched as whole tokens. A message with no time, such as one delivered inside a tool result, starts no turn. Without a session, or when either tool cannot read it, the lead cost is unknown unless given as `close --lead-cost`.
- `candidates` checks pool room only for agents the project already runs, using their executors' `pool`. For other agents, shown with `--all-agents`, room is unknown: no tool yet maps a model to the pool it spends. Writing the `pool` when adding the executor fills that in. A general mapping would need aop to report each model's backend in `aop models` and clanker-usage to report which models each pool covers.
- A running task keeps its reservation until the lead closes it; a run that crashed is closed as `aborted`. `reap` only closes opens that never started.

## Development

```sh
uv run --locked python -m unittest discover -s tests -t .
uvx ruff check delegate tests --select E,F,W,B,I
```

## License

MIT
