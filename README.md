# delegate

A lead agent in a project can hand tasks to executor agents, or do them itself. `delegate` decides which: for each task it draws, from one model of every actor on every project's tasks, the actor with the lowest expected cost per result that stands, the lead included, so actors whose record is thin are tried in proportion to the chance they are best. It checks each actor's subscription pool has room, reserves what the task needs, and keeps an append-only ledger per project of how every task went, from which the model is fitted.

- [WORKFLOW.md](WORKFLOW.md): the workflow three projects adopted, its rules and the evidence behind each.
- [FORMAT.md](FORMAT.md): the host roster (`host.toml`), the project config (`delegate.toml`), the ledger format and the commands.
- [proposals/shared-skill.md](proposals/shared-skill.md): the design of the model and the choice, and the simulations behind it; [sim/](sim/README.md) is the simulator.

## Install

```sh
uv tool install git+https://github.com/wakamex/delegate
```

It needs Python 3.12 or later and [NumPy](https://numpy.org). It reads the run records of [aop (agent orchestration process)](https://github.com/wakamex/agent-orchestration-process) for executor costs, and takes the lead's turns from `sch` and its per-response costs from `clanker-analytics session-costs` in [clanker-analytics](https://github.com/wakamex/clanker-analytics).

Pools are checked by a command taking `--check TARGET [--cost DOLLARS]` that prints one JSON line with the pool's windows and exits 0 when the run fits, 1 or 3 when it does not, and 2 on an error; a pool it cannot price takes one run at a time. `delegate` uses `usage-burn` on `PATH`, or the command named by `DELEGATE_USAGE_BURN`; without one it checks no pool, and `select` says so in its output.

## Usage

Declare the host's actors once in `~/.config/delegate/host.toml` ([examples/host.toml](examples/host.toml)), and put a `delegate.toml` declaring the project's kinds at its root ([examples/delegate.toml](examples/delegate.toml)). Then, for each task:

```sh
delegate select port bounded --resources checkout    # {"answer": "delegate", "actor": "sonnet", "task": "port-0007", "resources": {"checkout": "sim-a"}, ...}
aop run --agent claude --model claude-sonnet-5-5 --workdir ../sim-a ...
delegate started port-0007 --aop-run RUN_ID --unit job-port-7
delegate close port-0007 --outcome held --score 60 --score-reason "bounded port, confirmed live" --commit abc1234
delegate scorecard port
```

A `wait` answer names the busy resource or pool, and under `refused` each otherwise eligible actor its pool check turned away, with the check's state and its reasons; a wait opens nothing, so it can be asked as often as needed. Try again later with `--since` set to the time it gave. When a pool check could not answer at all, such as usage-burn failing or timing out, an actor on that pool is passed over like a refused one, and if nothing else can be chosen the answer is `error` instead of `wait`, with the failure in `error`, the rest of the wait's fields, and exit status 2, so a caller polling a wait can tell a check to fix from quota to wait for. A `lead` answer means the lead does the task itself, and records it the same way. Every command prints one JSON line and exits 0, or prints `{"error": ...}` and exits 2.

## Limitations

- Lead cost: the lead session, given with `close --lead-session`, `DELEGATE_LEAD_SESSION` or `[lead] session`, is read for any harness: its turns, the user messages that started them, from `sch` ([search-clanker-history](https://github.com/wakamex/search-clanker-history) 0.3.1 or later, which finds one session's messages in seconds and leaves out lines that start no turn), and each response's cost from `clanker-analytics session-costs`; delegate only decides which turns are the task's. Unit and run names are matched as whole tokens. A message with no time, such as one delivered inside a tool result, starts no turn. Without a session, or when either tool cannot read it, the lead cost is unknown unless given as `close --lead-cost`.
- `candidates` checks pool room only for agents the host roster already runs, using their actors' `pool`. For other agents, shown with `--all-agents`, room is unknown: no tool yet maps a model to the pool it spends. Writing the `pool` when adding the actor fills that in. A general mapping would need aop to report each model's backend in `aop models` and clanker-usage to report which models each pool covers.
- A running task keeps its reservation until the lead closes it; a run that crashed is closed as `aborted`. `reap` only closes opens that never started.

## Development

```sh
uv --no-config run --locked python -m unittest discover -s tests -t .
uvx ruff check delegate tests --select E,F,W,B,I
```

## License

MIT
