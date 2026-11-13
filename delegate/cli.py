"""The delegate command: every subcommand prints one JSON line."""

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from delegate import policy
from delegate.config import LEAD, REMOVED, ConfigError, find_config, load, load_host
from delegate.ledger import REAPED, Ledger, LedgerError, parse_time
from delegate.policy import Env, SelectError

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "delegate"
AOP_RESULTS = STATE / "aop" / "results"
AGENT_JOBS = STATE / "agent-jobs"  # each agent-job unit's log, where aop prints its run ids
# The pool check: a command taking --check TARGET [--cost DOLLARS] that prints one JSON line and exits 0 for room,
# 1 or 3 for none and 2 for an error, such as usage-burn. Without one, pools are not checked.
USAGE_BURN = os.environ.get("DELEGATE_USAGE_BURN") or shutil.which("usage-burn")
SESSION_COST_SECONDS = 60


class UsageError(Exception):
    """A command given arguments that do not fit the task or the config."""


def now() -> datetime:
    """The current time, or DELEGATE_NOW for tests."""
    fixed = os.environ.get("DELEGATE_NOW")
    return parse_time(fixed) if fixed else datetime.now(UTC)


# ---- what select and close need from outside the ledger ------------------------------------------


def pool_check(target: str, dollars: float | None) -> dict:
    """Room on a quota pool for runs spending DOLLARS: exit 0 room, 1 or 3 none, 2 an error. A check that cannot
    answer gives no room with state "error", so the other pools can still be chosen from; one that cannot price the
    run answers "unavailable", which delegate treats as a pool to run one task on at a time."""

    def failed(reason: str) -> dict:
        return {
            "room": False,
            "marginal": 1.0,
            "state": "error",
            "reasons": [f"usage-burn could not check {target}: {reason}"],
        }

    if not USAGE_BURN:
        return {"room": True, "marginal": 1.0, "state": "unchecked"}
    command = [USAGE_BURN, "--check", target]
    if dollars is not None:
        command += ["--cost", f"{dollars:.4f}"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return failed(str(exc))
    try:
        result = json.loads(completed.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        result = None
    if completed.returncode == 2 or not isinstance(result, dict):
        return failed((result or {}).get("error") or completed.stdout.strip() or completed.stderr.strip() or "no JSON")
    return {
        "room": completed.returncode == 0,
        "marginal": marginal(result.get("windows") or []),
        "state": result.get("state"),
        "reasons": result.get("reasons") or ([result["error"]] if result.get("error") else []),
        **({"account": result["account_name"]} if result.get("account_name") else {}),
        **({"headroom": headroom(result.get("windows") or [])} if result.get("state") == "unavailable" else {}),
    }


def headroom(windows: list[dict]) -> bool:
    """For a pool the check cannot price a run on: whether every window is below what its pace allows by now, so
    one more run of unknown size can start. A window without a reading leaves no room, and so does a pool whose
    windows are priced, since the check then called it unavailable for another reason, such as a stale reading."""
    return bool(windows) and all(
        w.get("run_points") is None
        and w.get("used_pct") is not None
        and w.get("allowed_pct") is not None
        and w["used_pct"] < w["allowed_pct"]
        for w in windows
    )


def marginal(windows: list[dict]) -> float:
    """The share of a run's points that comes out of quota the pool would otherwise use: 0 when the run fits in
    allowance due to expire unused, 1 at full price. A window used at average pace P (a multiple of the steady pace)
    ends at 100 P percent, so 100 (1 - P) points of it expire; the window leaving the run the least of those binds.
    A window without a pace yet, early in its period, gives no discount."""
    shares = []
    for window in windows:
        if points := window.get("run_points"):
            pace = window.get("pace")
            expiring = 0.0 if pace is None else max(0.0, 100 * (1 - pace))
            shares.append(max(0.0, points - expiring) / points)
    return max(shares, default=1.0)


def aop_models() -> list[dict]:
    """The models aop can run, from aop models --json."""
    try:
        completed = subprocess.run(["aop", "models", "--json"], capture_output=True, text=True, timeout=120)
        return json.loads(completed.stdout).get("models") or []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise UsageError(f"aop models gave no model list: {exc}") from exc


def unit_runs(units: list[str], since: datetime, until: datetime | None = None) -> list[str]:
    """The aop runs the agent-job units' logs name, in order, that started between SINCE and UNTIL: a unit name
    reused by another task leaves runs in its log that are not this task's."""
    found = []
    for unit in units:
        try:
            text = (AGENT_JOBS / f"{unit}.log").read_text(errors="replace")
        except OSError:
            continue
        for run in re.findall(r"run_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", text):
            started = (ENV["aop_result"](run) or {}).get("started_at")
            moment = parse_time(started) if started else None
            if run not in found and moment and moment >= since and (until is None or moment <= until):
                found.append(run)
    return found


def claude_home(account: str) -> str | None:
    """The CLAUDE_CONFIG_DIR of the Claude Code home signed in as ACCOUNT, the email name usage-burn gives it: None
    for the default home, which needs none."""
    home = Path.home()
    homes = {None: home / ".claude.json", **{str(p.parent): p for p in home.glob(".claude*/.claude.json")}}
    found = []
    for directory, settings in homes.items():
        try:
            email = (json.loads(settings.read_text()).get("oauthAccount") or {}).get("emailAddress") or ""
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if email.split("@")[0] == account:
            found.append(directory)
    if None in found:
        return None
    if len(found) != 1:
        raise UsageError(f"{len(found)} Claude homes are signed in as {account}")
    return found[0]


def launch_job(
    unit: str, command: list[str], environment: dict[str, str], directory: Path, minutes: float, memory: str
):
    """Start COMMAND in DIRECTORY as the agent-job UNIT, which reports to the lead's route when it ends."""
    env_args = [f"{k}={v}" for k, v in environment.items()]
    shell = f"cd {shlex.quote(str(directory))} && exec {shlex.join(command)}"
    job = [
        "agent-job",
        unit,
        "--max",
        f"{int(minutes)}min",
        "--mem",
        memory,
        "--",
        "/usr/bin/env",
        *env_args,
        "sh",
        "-c",
        shell,
    ]
    try:
        completed = subprocess.run(job, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsageError(f"agent-job could not start {unit}: {exc}") from exc
    if completed.returncode != 0:
        raise UsageError(f"agent-job could not start {unit}: {completed.stderr.strip() or completed.stdout.strip()}")


def aop_result(run_id: str) -> dict | None:
    path = AOP_RESULTS / f"{run_id}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def runs_cost(results: list[dict]) -> float | None:
    """What aop calculated the runs cost, or None when it priced any of them at nothing: a model aop has no price
    for, or a run that reported no usage, is unknown rather than free."""
    amounts = [(r.get("calculated_cost") or {}).get("amount_usd") for r in results]
    return None if any(a is None for a in amounts) else sum(float(a) for a in amounts)


def runs_minutes(results: list[dict]) -> float:
    return sum(float(r.get("duration_seconds") or 0) for r in results) / 60


def charged_lead_cost(turns, costs, names, opened, other_opens=(), own=(), other_names=()) -> float:
    """Sum the lead cost charged to a task: each turn whose triggering message names one of NAMES (its agent-job
    units or aop runs), and its part of the turn in which it was opened, which holds its brief. Every cost row
    belongs to the latest turn started at or before it; other turns, including the user's own and those before the
    task was opened, charge no task.

    A turn can open several tasks, so the opening turn is split among them at the times they were opened (this
    task's OPENED and OTHER_OPENS): each takes the responses from the previous opening, or the turn's start, up to
    its own, which hold its brief, and the last one opened also takes the rest of the turn, which holds its
    launch. Nothing is charged to two tasks.

    The lead's own task has no brief and is not split by turns: the lead works it in turns started by anything, and
    opens its next task inside the turn that finished this one. OWN lists the spans in which it was the lead's latest
    open own task, and it takes every response in them, except those of turns whose trigger names another task
    (OTHER_NAMES) and the part of a turn up to opening an executor's task (OTHER_OPENS), which is that task's
    brief."""
    starts = [start for start, _ in turns]

    def turn_of(moment):
        return max((j for j, start in enumerate(starts) if start <= moment), default=None)

    # A name matches only as a whole token, so a unit job-ab does not match a message about job-ab2.
    patterns = [re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])") for name in names if name]
    opening = turn_of(opened)
    # A turn before the one that opened the task belongs to an earlier task, even one that reused its unit's name.
    charged = {
        i
        for i, (_, text) in enumerate(turns)
        if (opening is None or i >= opening) and any(p.search(text) for p in patterns)
    }
    if own:
        others = [re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])") for name in other_names if name]
        elsewhere = {i for i, (_, text) in enumerate(turns) if any(p.search(text) for p in others)} - charged
        briefs = []
        for t in other_opens:
            j = turn_of(t)
            if j is not None:
                briefs.append((max((o for o in other_opens if turn_of(o) == j and o < t), default=starts[j]), t))
        total = 0.0
        for moment, cost in costs:
            i = turn_of(moment)
            if i is None or i in elsewhere or any(a < moment <= b for a, b in briefs):
                continue
            if i in charged or any(a <= moment and (b is None or moment <= b) for a, b in own):
                total += cost
        return round(total, 4)
    segment_start, segment_end = None, None
    if opening is not None:
        same_turn = [t for t in other_opens if turn_of(t) == opening]
        segment_start = max((t for t in same_turn if t < opened), default=starts[opening])
        # Only the last task opened in the turn runs on to its end.
        segment_end = opened if any(t > opened for t in same_turn) else None
    total = 0.0
    for moment, cost in costs:
        i = turn_of(moment)
        if i in charged:
            total += cost
        elif (
            i is not None and i == opening and segment_start < moment and (segment_end is None or moment <= segment_end)
        ):
            total += cost
    return round(total, 4)


def _run_json(command: list[str]):
    completed = subprocess.run(command, capture_output=True, text=True, timeout=SESSION_COST_SECONDS)
    if completed.returncode != 0:
        raise ValueError(f"{command[0]} exited {completed.returncode}")
    return completed.stdout


def session_activity(session: str) -> dict | None:
    """A lead session's turns and priced responses, for any harness: {"turns": [(time, text)], "costs": [(time,
    dollars)]}. The turns, the user messages that started them, come from sch (search-clanker-history), and each
    response's cost from clanker-analytics session-costs. None when either cannot say."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", session):
        raise UsageError(f"lead session {session!r} is not a session id")
    try:
        responses = json.loads(_run_json(["clanker-analytics", "session-costs", session, "--json"]))["responses"]
        messages = [
            json.loads(line)
            for line in _run_json(["sch", "-s", session, "-t", "user", "--json", "."]).splitlines()
            if line.strip()
        ]
        # A message with no time cannot start a timed turn.
        turns = [(parse_time(m["time"].replace("Z", "+00:00")), m["text"]) for m in messages if m.get("time")]
        costs = [
            (parse_time(r["timestamp"].replace("Z", "+00:00")), float(r["api_cost_usd"]))
            for r in responses
            if r.get("api_cost_usd") is not None
        ]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return {"turns": sorted(turns), "costs": costs}


def lead_session(config, given: str | None = None) -> str | None:
    """The lead's session: --lead-session, else DELEGATE_LEAD_SESSION, else the config's. A lead's session id changes
    whenever it starts afresh, so the flag or the variable is the usual source and the config only a default."""
    return given or os.environ.get("DELEGATE_LEAD_SESSION") or config.lead_session


def lead_cost_from_session(config, task, runs=(), session=None, others=()) -> tuple[float, float] | None:
    """The lead's turns charged to TASK, from the lead session's turns and priced responses as clanker-analytics
    reads them, in total and before the task started (its brief). OTHERS are the ledger's other tasks. Unknown when
    there is no session or clanker-analytics cannot say."""
    session = lead_session(config, session)
    if not session:
        return None
    activity = ENV["session_activity"](session)
    if activity is None:
        return None
    since = task.opened - timedelta(days=1)  # leaves out responses a fork copied in with earlier times
    # A closed task's lead cost ends at its close, so a later task reusing its unit's name is not charged to it.
    until = parse_time(task.close["at"]) if task.close else None
    costs = [
        (moment, cost) for moment, cost in activity["costs"] if moment >= since and (until is None or moment <= until)
    ]
    started = task.started or {}
    # Turns before the start can name only the task: its unit and run do not exist yet.
    names = [name for name in [task.id, *(started.get("units") or []), started.get("aop_run"), *runs] if name]
    other_opens = [t.opened for t in others]
    own, other_names = (), []
    if task.actor == LEAD:
        other_opens = [t.opened for t in others if t.actor != LEAD]
    end = until or datetime.max.replace(tzinfo=UTC)
    for other in others:
        if other.opened <= end and (other.closed_at is None or other.closed_at >= task.opened):
            st = other.started or {}
            other_names += [
                other.id,
                *(st.get("units") or []),
                st.get("aop_run"),
                *((other.close or {}).get("aop_runs") or []),
            ]
    if task.actor == LEAD:
        # The lead works its latest own task: one opened later takes over from its open while it is open, so an
        # earlier task left open does not hold every later one's turns.
        later = [(t.opened, t.closed_at) for t in others if t.actor == LEAD and t.opened > task.opened]
        own = _alone(task.opened, until, later)
    args = (names, task.opened, other_opens, own, other_names)
    total = charged_lead_cost(activity["turns"], costs, *args)
    began = parse_time(started["at"]) if started.get("at") else None
    brief = (
        total if began is None else charged_lead_cost(activity["turns"], [(m, c) for m, c in costs if m < began], *args)
    )
    return total, brief


def _alone(start, end, busy) -> list[tuple]:
    """The parts of START to END (None: still open) that no span in BUSY overlaps."""
    spans = [(start, end)]
    for b_start, b_end in busy:
        cut = []
        for a, b in spans:
            if (b is not None and b < b_start) or (b_end is not None and b_end < a):
                cut.append((a, b))
                continue
            if a < b_start:
                cut.append((a, b_start))
            if b_end is not None and (b is None or b_end < b):
                cut.append((b_end, b))
        spans = cut
    return spans


ENV = {
    "pool": pool_check,
    "aop_result": aop_result,
    "models": aop_models,
    "lead_cost": lead_cost_from_session,
    "session_activity": session_activity,
    "unit_runs": unit_runs,
    "launch": launch_job,
    "claude_home": claude_home,
}


# ---- reaping -------------------------------------------------------------------------------------


def reap(config, session, moment: datetime) -> list[dict]:
    """Close as aborted every executor's open with no started event within the lease. The lead's own task has no
    run to start: it is the lead's own session, which closes it."""
    written = []
    lease = timedelta(minutes=config.lease_minutes)
    for task in list(session.state.tasks.values()):
        if task.holding and task.actor != LEAD and task.started is None and moment - task.opened > lease:
            written.append(
                session.append(
                    {
                        "event": "close",
                        "at": moment.isoformat(),
                        "task": task.id,
                        "outcome": "aborted",
                        "reason": REAPED,
                    }
                )
            )
    return written


def _task(session, task_id):
    task = session.state.tasks.get(task_id)
    if task is None:
        raise UsageError(f"unknown task {task_id!r}")
    return task


def _env(config) -> Env:
    return Env(pool=ENV["pool"], rng=random.Random(), cache=CACHE)


def _world(config, session) -> policy.World:
    return policy.load_world(config, session.state, lambda path: Ledger(path).read())


def cmd_select(config, session, args, moment):
    if args.ignore_pool and not args.assign:
        raise UsageError("--ignore-pool goes with --assign")
    resources = args.resources.split(",") if args.resources else None
    since = parse_time(args.since) if args.since else None
    result, events = policy.select(
        _world(config, session),
        _env(config),
        args.kind,
        args.cls,
        moment,
        resources,
        args.token_cap,
        args.retry,
        since,
        args.assign,
        args.why,
        args.cause,
        args.ignore_pool,
    )
    for event in events:
        session.append(event)
    return result


def _epochs(config, session, actor, moment) -> list[dict]:
    """Append an epoch for ACTOR if its record since its last epoch shows a change."""
    return [session.append(e) for e in policy.detect_epochs(_world(config, session), actor, moment)]


def cmd_started(config, session, args, moment):
    task = _task(session, args.task)
    if task.close:
        raise UsageError(f"{task.id} is already closed")
    if not (args.aop_run or args.unit):
        raise UsageError("started needs --aop-run or --unit")
    event = {"event": "started", "at": moment.isoformat(), "task": task.id}
    if args.aop_run:
        event["aop_run"] = args.aop_run
    if args.unit:
        event["units"] = args.unit
    return session.append(event)


def task_runs(task, given=None, until=None) -> list[str]:
    """A task's aop runs: those its started event and GIVEN name, and those its units' logs name, which is how the
    runs delegate launched are found, since aop gives a run its id only once it is going."""
    started = task.started or {}
    runs = [r for r in [started.get("aop_run"), *(given or [])] if r]
    for run in ENV["unit_runs"](started.get("units") or [], task.opened, until):
        if run not in runs:
            runs.append(run)
    return runs


def _launch(config, task, unit: str, aop: list[str]):
    """Start AOP for TASK as agent-job UNIT, on the Claude account the task was opened for, and record it."""
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())}
    if account := task.open.get("account"):
        if home := ENV["claude_home"](account):
            environment["CLAUDE_CONFIG_DIR"] = home
    launch = config.launch
    ENV["launch"](unit, aop, environment, config.root / launch.directory, launch.timeout_minutes + 30, launch.memory)
    return {"event": "started", "task": task.id, "units": [unit]}


def cmd_run(config, session, args, moment):
    """Start an executor's aop run for an open task: its agent, model and effort, the task's checkout as workdir,
    its Claude account, and the kind's no-web requirement, as agent-job named after the task."""
    task = _task(session, args.task)
    if task.close or task.started:
        raise UsageError(f"{task.id} is already {'closed' if task.close else 'started'}")
    executor = config.executors.get(task.actor)
    if executor is None:
        raise UsageError(f"{task.id} is the lead's; delegate runs only executors")
    launch, kind = config.launch, config.kinds[task.kind]
    aop = ["aop", "run", "--agent", executor.agent, "--model", executor.model, "--profile", launch.profile]
    if executor.effort:
        aop += ["--effort", executor.effort]
    if launch.workdir:
        try:
            workdir = launch.workdir.format(task=task.id, **(task.open.get("resources") or {}))
        except KeyError as exc:
            raise UsageError(f"launch.workdir names {exc}, which {task.id} did not reserve") from exc
        aop += ["--workdir", workdir]
    if "no-web" in kind.capabilities:
        aop.append("--no-web")
    aop += ["--timeout", str(int(launch.timeout_minutes * 60)), "--prompt-file", str(Path(args.prompt_file).resolve())]
    event = _launch(config, task, task.id, aop)
    return session.append({**event, "at": moment.isoformat()})


def cmd_continue(config, session, args, moment):
    """Resume a task's latest aop run with a follow-up prompt, as the next agent-job unit of the task."""
    task = _task(session, args.task)
    if task.close:
        raise UsageError(f"{task.id} is already closed")
    runs = task_runs(task)
    if not runs:
        raise UsageError(f"{task.id} has no aop run to continue; one killed before aop wrote its result cannot be")
    units = (task.started or {}).get("units") or []
    unit = f"{task.id}-r{len(units)}"
    aop = [
        "aop",
        "resume",
        "--timeout",
        str(int(config.launch.timeout_minutes * 60)),
        "--prompt-file",
        str(Path(args.prompt_file).resolve()),
        runs[-1],
    ]
    event = _launch(config, task, unit, aop)
    return session.append({**event, "at": moment.isoformat()})


def _evidence(values):
    out = []
    for value in values or []:
        path = Path(value)
        try:
            is_file = path.is_file()
        except OSError:  # free text too long to be a file name
            is_file = False
        if is_file:
            out.append({"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        else:
            out.append(value)
    return out


def _score(kind, args, required):
    if kind.measure != "lead-score":
        if args.score is not None:
            raise UsageError(f"kind {kind.name} does not use a lead score")
        return None
    if args.score is None:
        if required:
            raise UsageError(f"kind {kind.name} needs --score and --score-reason for a successful task")
        return None
    if not 0 <= args.score <= 100 or not args.score_reason:
        raise UsageError("--score is from 0 to 100 and needs --score-reason")
    return {"value": args.score, "reason": args.score_reason}


def _yield(kind, args, required):
    if kind.measure != "yield":
        if args.yield_ is not None:
            raise UsageError(f"kind {kind.name} does not count yield")
        return None
    if args.yield_ is None and required:
        raise UsageError(f"kind {kind.name} needs --yield, counted by its reference check, for a successful task")
    if args.yield_ is not None and args.yield_ < 0:
        raise UsageError("--yield must not be negative")
    return args.yield_


def _correction(args, outcome, default_caught=None):
    if outcome != "corrected":
        if args.caught_by or args.defect:
            raise UsageError("--caught-by and --defect are for corrected outcomes")
        return {}
    caught = args.caught_by or default_caught
    if not caught or not args.defect:
        raise UsageError("a correction needs --caught-by and --defect")
    return {"caught_by": caught, "defect": args.defect}


def cmd_close(config, session, args, moment):
    task = _task(session, args.task)
    kind = config.kinds[task.kind]
    if task.close and not task.reaped:
        raise UsageError(f"{task.id} is already closed")
    # A close may replace a reap: the work went on without a started event. It ended when the reap saw it, since the
    # lead's later turns may be other work, so its costs and minutes stop there.
    ended = task.closed_at if task.reaped else None
    outcome = args.outcome
    if kind.check == "deferred" and outcome in ("held", "steered"):
        raise UsageError("a deferred kind closes as awaiting; held comes from its verdict")
    if kind.check != "deferred" and outcome == "awaiting":
        raise UsageError("only a deferred kind closes as awaiting")
    success = outcome in ("held", "steered")
    event = {
        "event": "close",
        "at": moment.isoformat(),
        "task": task.id,
        "outcome": outcome,
        **_correction(args, outcome),
    }
    score, yielded = _score(kind, args, success), _yield(kind, args, success)
    if score:
        event["score"] = score
    if yielded is not None:
        event["yield"] = yielded
    if kind.investigation and success:
        if not args.evidence or args.falsifier_checked is None:
            raise UsageError("an investigation needs --evidence and --falsifier-checked")
    runs = task_runs(task, args.aop_run, moment)
    actor_cost, run_minutes = args.actor_cost, args.run_minutes
    if task.actor != LEAD and runs and actor_cost is None:
        results = [ENV["aop_result"](r) for r in runs]
        if all(results):
            actor_cost = runs_cost(results)
            if run_minutes is None:
                run_minutes = runs_minutes(results)
    measured = ENV["lead_cost"](
        config, task, runs, args.lead_session, [t for t in session.state.tasks.values() if t.id != task.id]
    )
    lead_cost, brief = measured if measured else (None, None)
    if args.lead_cost is not None:
        lead_cost, brief = args.lead_cost, None
    if task.actor == LEAD and actor_cost is None:
        actor_cost = lead_cost  # the lead's own work is its actor cost
    end = ended or moment
    held = {name: round((end - task.opened).total_seconds() / 60, 1) for name in (task.open.get("resources") or {})}
    event.update(
        {
            "cost": {
                "actor": actor_cost,
                "lead": None if task.actor == LEAD else lead_cost,
                "brief": None if task.actor == LEAD else brief,
            },
            "resumes": args.resumes
            if args.resumes is not None
            else sum(
                bool(re.fullmatch(rf"{re.escape(task.id)}-r\d+", u)) for u in (task.started or {}).get("units") or []
            ),
            "brief_error": args.brief_error,
            "minutes": {"run": run_minutes, "held": held},
            "aop_runs": runs,
            "commits": args.commit or [],
            "claims": args.claim or [],
            "evidence": _evidence(args.evidence),
            "report": args.report,
        }
    )
    if kind.investigation:
        event["falsifier_checked"] = args.falsifier_checked == "true"
    if ended:
        event.update(ended=ended.isoformat(), replaces="reap")
    written = session.append(event)
    epochs = _epochs(config, session, task.actor, moment)
    return {**written, "epochs": epochs} if epochs else written


def cmd_verdict(config, session, args, moment):
    task = _task(session, args.task)
    kind = config.kinds[task.kind]
    if kind.check != "deferred" or not task.close or task.close["outcome"] != "awaiting":
        raise UsageError(f"{task.id} is not a deferred task awaiting its verdict")
    if task.verdict:
        raise UsageError(f"{task.id} already has its verdict; use overturn")
    success = args.outcome == "held"
    event = {
        "event": "verdict",
        "at": moment.isoformat(),
        "task": task.id,
        "outcome": args.outcome,
        **_correction(args, args.outcome, "deferred-check"),
        "evidence": _evidence(args.evidence),
        "unresolved": args.unresolved or [],
    }
    if not event["evidence"]:
        raise UsageError("a verdict needs --evidence naming the run of the check that decided it")
    score, yielded = _score(kind, args, success), _yield(kind, args, success)
    if score:
        event["score"] = score
    if yielded is not None:
        event["yield"] = yielded
    written = session.append(event)
    epochs = _epochs(config, session, task.actor, moment)
    return {**written, "epochs": epochs} if epochs else written


def cmd_overturn(config, session, args, moment):
    root = _task(session, args.root)
    if root.outcome not in ("held", "steered"):
        raise UsageError(f"{root.id} is not a standing success to overturn")
    if args.found_by:
        _task(session, args.found_by)
    written = session.append(
        {
            "event": "overturn",
            "at": moment.isoformat(),
            "root": root.id,
            "redo_cost": args.redo_cost,
            "reason": args.reason,
            "found_by": args.found_by,
            "defect": args.defect,
        }
    )
    epochs = _epochs(config, session, root.actor, moment)
    return {**written, "epochs": epochs} if epochs else written


def cmd_amend(config, session, args, moment):
    """Correct fields of a closed task, recording each old and new value."""
    task = _task(session, args.task)
    if not task.close:
        raise UsageError(f"{task.id} is not closed; give the values to close")
    new = {
        "cost.actor": args.actor_cost,
        "cost.lead": args.lead_cost,
        "cost.brief": args.brief_cost,
        "minutes.run": args.run_minutes,
        "aop_runs": args.aop_run,
        "commits": args.commit,
        "report": args.report,
        "score.value": args.score,
        "score.reason": args.score_reason,
    }
    new = {path: value for path, value in new.items() if value is not None}
    if args.score is not None or args.score_reason is not None:
        # A score is given before the lead reads the cost; a rescore corrects a slip and shows on the scorecard.
        if not (task.close.get("score") or {}).get("reason"):
            raise UsageError(f"{task.id} has no lead score to correct")
        if args.score is not None and not 0 <= args.score <= 100:
            raise UsageError("--score is from 0 to 100")
    if args.from_aop:
        runs = new.get("aop_runs") or task.close.get("aop_runs") or task_runs(task, None, parse_time(task.close["at"]))
        if runs != task.close.get("aop_runs"):
            new.setdefault("aop_runs", runs)
        results = [ENV["aop_result"](r) for r in runs]
        if not runs or not all(results):
            raise UsageError(f"aop has no archived result for every run of {task.id}: {', '.join(runs) or 'none'}")
        new.setdefault("cost.actor", runs_cost(results))
        new.setdefault("minutes.run", runs_minutes(results))
    if args.from_lead_session:
        others = [t for t in session.state.tasks.values() if t.id != task.id]
        runs = new.get("aop_runs", task.close.get("aop_runs") or [])
        measured = ENV["lead_cost"](config, task, runs, args.lead_session, others)
        if measured is None:
            raise UsageError(f"no lead session to recompute {task.id}'s lead cost from; give --lead-session")
        cost, brief = measured
        if task.actor == LEAD:
            new.setdefault("cost.actor", cost)
        else:
            new.setdefault("cost.lead", cost)
            new.setdefault("cost.brief", brief)
    if not new:
        raise UsageError("amend needs a field to correct, --from-aop or --from-lead-session")

    def old(path):
        head, _, leaf = path.partition(".")
        value = task.close.get(head)
        return (value or {}).get(leaf) if leaf else value

    changes = {path: {"old": old(path), "new": value} for path, value in new.items() if old(path) != value}
    if not changes:
        raise UsageError(f"{task.id} already records these values")
    return session.append(
        {
            "event": "amend",
            "at": moment.isoformat(),
            "task": task.id,
            "by": args.by,
            "reason": args.reason,
            "changes": changes,
        }
    )


def cmd_actor_event(config, session, args, moment):
    """A person stops or restarts an actor, everywhere or in one kind of this project."""
    if args.kind is not None and args.kind not in config.kinds:
        raise UsageError(f"unknown kind {args.kind!r}")
    if args.actor != LEAD and args.actor not in config.executors:
        raise UsageError(f"unknown actor {args.actor!r}; actors are the host roster's")
    event = {"event": args.command, "at": moment.isoformat(), "actor": args.actor, "reason": args.reason}
    if args.kind is not None:
        event["kind"] = args.kind
    return session.append(event)


def cmd_scorecard(config, session, args, moment):
    return {"scorecard": policy.scorecard(_world(config, session), _env(config), moment, args.kind)}


def cmd_candidates(config, session, args, moment):
    """Models aop offers that the host roster does not run, with price and pool room."""
    configured = {(e.agent, e.model) for e in config.executors.values()}
    configured |= {(e.agent, f"{e.model}-{e.effort}") for e in config.executors.values() if e.effort}
    pools = {}
    for executor in config.executors.values():
        pools.setdefault(executor.agent, executor.pool)
    agents = set(args.agent or []) or (None if args.all_agents else set(pools))
    by_agent: dict[str, list] = {}
    for entry in ENV["models"]():
        agent, model = entry.get("agent"), entry.get("model")
        if not agent or not model or (agent, model) in configured or (agents is not None and agent not in agents):
            continue
        by_agent.setdefault(agent, []).append(
            {
                "model": model,
                "input_per_million_usd": entry.get("input_per_million_usd"),
                "output_per_million_usd": entry.get("output_per_million_usd"),
                "availability": entry.get("availability"),
            }
        )
    result = []
    for agent in sorted(by_agent):
        models = sorted(
            by_agent[agent],
            key=lambda m: (m["output_per_million_usd"] is None, -(m["output_per_million_usd"] or 0), m["model"]),
        )
        pool = pools.get(agent)
        room = ENV["pool"](pool, None) if pool else None
        result.append(
            {
                "agent": agent,
                "pool": pool,
                "pool_room": None if room is None else room["room"],
                "models": models[: args.limit] if args.limit else models,
                "more": max(0, len(models) - args.limit) if args.limit else 0,
            }
        )
    return {"candidates": result}


def cmd_reap(config, session, args, moment):
    return {"reaped": []}  # reap runs before every command; its events are reported by main


COMMANDS = {
    "select": cmd_select,
    "started": cmd_started,
    "close": cmd_close,
    "verdict": cmd_verdict,
    "overturn": cmd_overturn,
    "amend": cmd_amend,
    "run": cmd_run,
    "continue": cmd_continue,
    "suspend": cmd_actor_event,
    "resume": cmd_actor_event,
    "scorecard": cmd_scorecard,
    "reap": cmd_reap,
    "candidates": cmd_candidates,
}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def parser() -> argparse.ArgumentParser:
    top = Parser(prog="delegate", description="Choose which agent takes each delegated task, and keep its ledger.")
    top.add_argument(
        "--config", type=Path, help="delegate.toml (default: the nearest at or above the current directory)"
    )
    sub = top.add_subparsers(dest="command", required=True, parser_class=Parser)

    s = sub.add_parser("select", help="choose delegate, wait or lead, and open the task")
    s.add_argument("kind")
    s.add_argument("cls", metavar="class")
    s.add_argument("--resources", help="R[=AMOUNT],... (default: the kind's)")
    s.add_argument("--token-cap", type=float)
    s.add_argument("--retry", metavar="TASK")
    s.add_argument("--since", help="when this task started waiting, from a previous wait answer")
    s.add_argument("--assign", metavar="ACTOR", help="open for this actor, skipping the draw but not its pool check")
    s.add_argument("--why", help="with --assign: who chose it and why, such as 'user: cursor confirms this fixture'")
    s.add_argument("--cause", choices=policy.CAUSES, help="with --assign: why the draw was overridden")
    s.add_argument(
        "--ignore-pool",
        action="store_true",
        help="with --assign: open even if its pool has no room, recorded on the open",
    )

    s = sub.add_parser("started", help="record what holds the task's reservation")
    s.add_argument("task")
    s.add_argument("--aop-run")
    s.add_argument("--unit", nargs="+", action="extend")

    def judged(s, outcomes):
        s.add_argument("task")
        s.add_argument("--outcome", required=True, choices=outcomes)
        s.add_argument("--caught-by", choices=("gate", "review", "deferred-check"))
        s.add_argument("--defect", choices=("integrity", "scope", "failed-gate", "unfinished"))
        s.add_argument("--score", type=float)
        s.add_argument("--score-reason")
        s.add_argument("--yield", dest="yield_", type=float)
        s.add_argument("--evidence", nargs="+", action="extend", help="artifact paths, hashed, or free references")

    s = sub.add_parser("close", help="close a task once its result has landed or been discarded")
    judged(s, ("held", "steered", "corrected", "aborted", "awaiting"))
    s.add_argument("--brief-error")
    s.add_argument("--resumes", type=int, help="resumes in the session (default: the units delegate continue started)")
    s.add_argument("--actor-cost", type=float, help="the actor's cost, if not from aop's records")
    s.add_argument("--lead-cost", type=float)
    s.add_argument("--lead-session", help="the lead's session id (default: DELEGATE_LEAD_SESSION, then the config)")
    s.add_argument("--run-minutes", type=float)
    s.add_argument("--aop-run", nargs="+", action="extend", help="further aop runs of the task")
    s.add_argument("--commit", nargs="+", action="extend")
    s.add_argument("--claim", nargs="+", action="extend")
    s.add_argument("--report")
    s.add_argument(
        "--falsifier-checked",
        type=str.lower,
        choices=("true", "false"),
        help="investigations: true or false",
    )

    s = sub.add_parser("verdict", help="record a deferred task's final verdict")
    judged(s, ("held", "corrected"))
    s.add_argument("--unresolved", nargs="+", action="extend")

    s = sub.add_parser("overturn", help="overturn a standing result")
    s.add_argument("root", help="the task whose commit introduced the overturned code")
    s.add_argument("--redo-cost", type=float, required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--found-by", metavar="TASK")
    s.add_argument("--defect", choices=("integrity",), help="integrity: the result was fabricated or misreported")

    s = sub.add_parser("run", help="start an open task's executor in aop, as agent-job named after the task")
    s.add_argument("task")
    s.add_argument("--prompt-file", required=True, help="the prompt the executor is given")

    s = sub.add_parser("continue", help="resume an open task's latest aop run with a follow-up prompt")
    s.add_argument("task")
    s.add_argument("--prompt-file", required=True)

    s = sub.add_parser("amend", help="correct a closed task's costs, minutes, runs, commits, report or score")
    s.add_argument("task")
    s.add_argument("--reason", required=True)
    s.add_argument("--by", default="lead", help="who made the correction (default: lead)")
    s.add_argument("--actor-cost", type=float)
    s.add_argument("--lead-cost", type=float)
    s.add_argument("--brief-cost", type=float, help="the part of the lead cost spent before the task started")
    s.add_argument("--run-minutes", type=float)
    s.add_argument("--aop-run", nargs="+", action="extend", help="the task's aop runs, replacing the recorded list")
    s.add_argument("--commit", nargs="+", action="extend", help="the task's commits, replacing the recorded list")
    s.add_argument("--report")
    s.add_argument("--score", type=float, help="the lead score meant at close; the reason says what was mistyped")
    s.add_argument("--score-reason")
    s.add_argument("--from-aop", action="store_true", help="actor cost and run minutes from aop's records of its runs")
    s.add_argument("--from-lead-session", action="store_true", help="lead cost recomputed from the lead's session")
    s.add_argument("--lead-session", help="the lead's session id (default: DELEGATE_LEAD_SESSION, then the config)")

    for name in ("suspend", "resume"):
        s = sub.add_parser(name, help=f"{name} an actor, everywhere or in one kind of this project")
        s.add_argument("actor")
        s.add_argument("kind", nargs="?")
        s.add_argument("--reason", required=True)

    s = sub.add_parser("scorecard", help="print each actor's record per kind")
    s.add_argument("kind", nargs="?")

    sub.add_parser("reap", help="close opens that never started within the lease")
    sub.add_parser(
        "migrate", help="record who an earlier config's actor names meant, and list the keys the config must drop"
    )

    s = sub.add_parser("candidates", help="list models aop offers that the host roster does not run")
    s.add_argument(
        "--agent", nargs="+", action="extend", help="only these agents (default: the agents the project runs)"
    )
    s.add_argument("--all-agents", action="store_true", help="every agent aop offers")
    s.add_argument("--limit", type=int, default=10, help="models per agent, priciest first (0 for all; default 10)")
    return top


def migrate(path: Path, moment: datetime) -> dict:
    """Record in the project's ledger who each actor name of its earlier config meant, so its tasks keep their
    actors once the config no longer names them, and list the keys the config must drop. It reads the config as
    plain TOML, since the current loader rejects an earlier version's keys."""
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data.get("ledger"), str):
        raise ConfigError("ledger is required")
    actors = {
        name: {"agent": e.get("agent"), "model": e.get("model"), "effort": e.get("effort")}
        for name, e in (data.get("executors") or {}).items()
        if isinstance(e, dict) and e.get("agent") and e.get("model")
    }
    lead = load_host().lead  # the roster's: an earlier config named no lead model
    if lead:
        actors[LEAD] = lead.identity
    removed = [key for key in data if key in REMOVED]
    removed += [f"lead.{key}" for key in data.get("lead") or {} if key in REMOVED]
    for name, kind in (data.get("kinds") or {}).items():
        removed += [f"kinds.{name}.{key}" for key in kind if key in REMOVED]
    with Ledger((path.parent / data["ledger"]).resolve()).locked() as session:
        if session.state.actors == {**session.state.actors, **actors}:
            return {"actors": {}, "remove": removed, "note": "the ledger already describes these actors"}
        session.append({"event": "actors", "at": moment.isoformat(), "actors": actors})
    return {"actors": actors, "remove": removed}


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.command == "migrate":
            print(json.dumps(migrate(args.config or find_config(Path.cwd()), now())))
            return 0
        config = load(args.config or find_config(Path.cwd()))
        moment = now()
        with Ledger(config.ledger).locked() as session:
            reaped = reap(config, session, moment)
            result = COMMANDS[args.command](config, session, args, moment)
        if reaped:
            result = {**result, "reaped": [{"task": e["task"], "event": e["event"]} for e in reaped]}
        if args.command == "select" and not USAGE_BURN:
            result = {**result, "pool_check": "skipped: no usage-burn on PATH and no DELEGATE_USAGE_BURN"}
        print(json.dumps(result, default=str))
        return 2 if result.get("answer") == "error" else 0
    except (UsageError, ConfigError, LedgerError, SelectError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    except Exception as exc:  # a bug must still print JSON, never pass for a choice
        print(json.dumps({"error": f"internal error: {exc!r}"}))
        return 2
