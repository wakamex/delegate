"""The delegate command: every subcommand prints one JSON line."""

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from delegate import policy
from delegate.config import ConfigError, find_config, load
from delegate.ledger import Ledger, LedgerError, parse_time
from delegate.policy import LEAD, Env, SelectError

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "delegate"
AOP_RESULTS = STATE / "aop" / "results"
# The pool check: a command taking --check TARGET [--cost DOLLARS] [--need N | --need 7d=N,30d=M] that prints
# one JSON line and exits 0 for room, 1 or 3 for none and 2 for an error, such as usage-burn. Without one,
# pools are not checked.
USAGE_BURN = os.environ.get("DELEGATE_USAGE_BURN") or shutil.which("usage-burn")
SESSION_COST_SECONDS = 60


class UsageError(Exception):
    """A command given arguments that do not fit the task or the config."""


def now() -> datetime:
    """The current time, or DELEGATE_NOW for tests."""
    fixed = os.environ.get("DELEGATE_NOW")
    return parse_time(fixed) if fixed else datetime.now(UTC)


# ---- what select and close need from outside the ledger ------------------------------------------


def pool_check(target: str, dollars: float | None, need: float | dict[str, float] | None) -> dict:
    """Room on a quota pool for runs spending DOLLARS, with NEED points for windows the check has no price for,
    one number for every window or one per window length: exit 0 room, 1 or 3 none, 2 an error. A check that
    cannot answer gives no room with state "error", so the other pools can still be chosen from."""

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
    if need is not None:
        parts = need.items() if isinstance(need, dict) else [(None, need)]
        command += [
            "--need",
            ",".join(f"{length}={points:.4f}" if length else f"{points:.4f}" for length, points in parts),
        ]
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
    }


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


def aop_result(run_id: str) -> dict | None:
    path = AOP_RESULTS / f"{run_id}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def charged_lead_cost(turns, costs, names, opened, other_opens=()) -> float:
    """Sum the lead cost charged to a task: each turn whose triggering message names one of NAMES (its agent-job
    units or aop runs), and its part of the turn in which it was opened, which holds its brief. Every cost row
    belongs to the latest turn started at or before it; other turns, including the user's own, charge no task.

    A turn can open several tasks, so the opening turn is split among them at the times they were opened (this
    task's OPENED and OTHER_OPENS): each takes the responses from the previous opening, or the turn's start, up to
    its own, which hold its brief, and the last one opened also takes the rest of the turn, which holds its
    launch. Nothing is charged to two tasks."""
    starts = [start for start, _ in turns]

    def turn_of(moment):
        return max((j for j, start in enumerate(starts) if start <= moment), default=None)

    # A name matches only as a whole token, so a unit job-ab does not match a message about job-ab2.
    patterns = [re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])") for name in names if name]
    charged = {i for i, (_, text) in enumerate(turns) if any(p.search(text) for p in patterns)}
    opening = turn_of(opened)
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


def lead_cost_from_session(config, task, runs=(), session=None, other_opens=()) -> float | None:
    """The lead's turns charged to TASK, from the lead session's turns and priced responses as clanker-analytics
    reads them. Unknown when there is no session or clanker-analytics cannot say."""
    session = lead_session(config, session)
    if not session:
        return None
    activity = ENV["session_activity"](session)
    if activity is None:
        return None
    since = task.opened - timedelta(days=1)  # leaves out responses a fork copied in with earlier times
    costs = [(moment, cost) for moment, cost in activity["costs"] if moment >= since]
    started = task.started or {}
    names = [name for name in [*(started.get("units") or []), started.get("aop_run"), *runs] if name]
    return charged_lead_cost(activity["turns"], costs, names, task.opened, other_opens)


ENV = {
    "pool": pool_check,
    "aop_result": aop_result,
    "models": aop_models,
    "lead_cost": lead_cost_from_session,
    "session_activity": session_activity,
}


# ---- reaping -------------------------------------------------------------------------------------


def reap(config, session, moment: datetime) -> list[dict]:
    """Close as aborted every open with no started event within the lease."""
    written = []
    lease = timedelta(minutes=config.lease_minutes)
    for task in list(session.state.tasks.values()):
        if task.holding and task.started is None and moment - task.opened > lease:
            written.append(
                session.append(
                    {
                        "event": "close",
                        "at": moment.isoformat(),
                        "task": task.id,
                        "outcome": "aborted",
                        "reason": "no started event within the lease",
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
    return Env(pool=ENV["pool"], rng=random.Random())


def cmd_select(config, session, args, moment):
    resources = args.resources.split(",") if args.resources else None
    since = parse_time(args.since) if args.since else None
    result, events = policy.select(
        config,
        session.state,
        args.kind,
        args.cls,
        moment,
        _env(config),
        resources,
        args.token_cap,
        args.retry,
        since,
        args.assign,
        args.why,
    )
    for event in events:
        session.append(event)
    return result


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
    if task.close:
        raise UsageError(f"{task.id} is already closed")
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
    runs = [r for r in [(task.started or {}).get("aop_run"), *(args.aop_run or [])] if r]
    actor_cost, run_minutes = args.actor_cost, args.run_minutes
    if task.actor != LEAD and runs and actor_cost is None:
        results = [ENV["aop_result"](r) for r in runs]
        if all(results):
            actor_cost = sum(float((r.get("calculated_cost") or {}).get("amount_usd") or 0) for r in results)
            if run_minutes is None:
                run_minutes = sum(float(r.get("duration_seconds") or 0) for r in results) / 60
    lead_cost = (
        args.lead_cost
        if args.lead_cost is not None
        else ENV["lead_cost"](
            config, task, runs, args.lead_session, [t.opened for t in session.state.tasks.values() if t.id != task.id]
        )
    )
    held = {name: round((moment - task.opened).total_seconds() / 60, 1) for name in (task.open.get("resources") or {})}
    event.update(
        {
            "cost": {"actor": actor_cost, "lead": None if task.actor == LEAD else lead_cost},
            "resumes": args.resumes,
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
    return session.append(event)


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
    return session.append(event)


def cmd_overturn(config, session, args, moment):
    root = _task(session, args.root)
    if root.outcome not in ("held", "steered"):
        raise UsageError(f"{root.id} is not a standing success to overturn")
    if args.found_by:
        _task(session, args.found_by)
    return session.append(
        {
            "event": "overturn",
            "at": moment.isoformat(),
            "root": root.id,
            "redo_cost": args.redo_cost,
            "reason": args.reason,
            "found_by": args.found_by,
        }
    )


def cmd_amend(config, session, args, moment):
    """Correct fields of a closed task, recording each old and new value."""
    task = _task(session, args.task)
    if not task.close:
        raise UsageError(f"{task.id} is not closed; give the values to close")
    new = {
        "cost.actor": args.actor_cost,
        "cost.lead": args.lead_cost,
        "minutes.run": args.run_minutes,
        "aop_runs": args.aop_run,
        "commits": args.commit,
        "report": args.report,
    }
    new = {path: value for path, value in new.items() if value is not None}
    if args.from_aop:
        runs = new.get("aop_runs", task.close.get("aop_runs") or [])
        results = [ENV["aop_result"](r) for r in runs]
        if not runs or not all(results):
            raise UsageError(f"aop has no archived result for every run of {task.id}: {', '.join(runs) or 'none'}")
        new.setdefault(
            "cost.actor", sum(float((r.get("calculated_cost") or {}).get("amount_usd") or 0) for r in results)
        )
        new.setdefault("minutes.run", sum(float(r.get("duration_seconds") or 0) for r in results) / 60)
    if not new:
        raise UsageError("amend needs a field to correct, or --from-aop")

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
    if args.kind not in config.kinds:
        raise UsageError(f"unknown kind {args.kind!r}")
    if args.actor not in config.executors:
        raise UsageError(f"unknown executor {args.actor!r}")
    return session.append(
        {"event": args.command, "at": moment.isoformat(), "actor": args.actor, "kind": args.kind, "reason": args.reason}
    )


def cmd_scorecard(config, session, args, moment):
    return {"scorecard": policy.scorecard(config, session.state, _env(config), args.kind)}


def cmd_candidates(config, session, args, moment):
    """Models aop offers that the project has not configured as executors, with price and pool room."""
    configured = {(e.agent, e.model) for e in config.executors.values()}
    configured |= {(e.agent, f"{e.model}-{e.effort}") for e in config.executors.values() if e.effort}
    pools = {}
    for executor in config.executors.values():
        pools.setdefault(executor.agent, executor.pool)
    agents = set(args.agent or []) or (None if args.all_agents else set(pools))
    families = [] if args.any_family else [re.compile(p) for p in config.candidate_families]
    by_agent: dict[str, list] = {}
    for entry in ENV["models"]():
        agent, model = entry.get("agent"), entry.get("model")
        if not agent or not model or (agent, model) in configured or (agents is not None and agent not in agents):
            continue
        if families and not any(p.search(model) for p in families):
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
        room = ENV["pool"](pool, None, None) if pool else None
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
    "suspend": cmd_actor_event,
    "resume": cmd_actor_event,
    "drop": cmd_actor_event,
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
    s.add_argument("--assign", metavar="EXECUTOR", help="open for this executor, skipping the draw and pool room")
    s.add_argument("--why", help="with --assign: who chose it and why, such as 'user: cursor confirms this fixture'")

    s = sub.add_parser("started", help="record what holds the task's reservation")
    s.add_argument("task")
    s.add_argument("--aop-run")
    s.add_argument("--unit", nargs="+", action="extend")

    def judged(s, outcomes):
        s.add_argument("task")
        s.add_argument("--outcome", required=True, choices=outcomes)
        s.add_argument("--caught-by", choices=("gate", "review", "deferred-check"))
        s.add_argument("--defect", choices=("integrity", "scope", "failed-gate"))
        s.add_argument("--score", type=float)
        s.add_argument("--score-reason")
        s.add_argument("--yield", dest="yield_", type=float)
        s.add_argument("--evidence", nargs="+", action="extend", help="artifact paths, hashed, or free references")

    s = sub.add_parser("close", help="close a task once its result has landed or been discarded")
    judged(s, ("held", "steered", "corrected", "aborted", "awaiting"))
    s.add_argument("--brief-error")
    s.add_argument("--resumes", type=int, default=0)
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

    s = sub.add_parser("amend", help="correct a closed task's costs, minutes, runs, commits or report")
    s.add_argument("task")
    s.add_argument("--reason", required=True)
    s.add_argument("--by", default="lead", help="who made the correction (default: lead)")
    s.add_argument("--actor-cost", type=float)
    s.add_argument("--lead-cost", type=float)
    s.add_argument("--run-minutes", type=float)
    s.add_argument("--aop-run", nargs="+", action="extend", help="the task's aop runs, replacing the recorded list")
    s.add_argument("--commit", nargs="+", action="extend", help="the task's commits, replacing the recorded list")
    s.add_argument("--report")
    s.add_argument("--from-aop", action="store_true", help="actor cost and run minutes from aop's records of its runs")

    for name in ("suspend", "resume", "drop"):
        s = sub.add_parser(name, help=f"{name} an executor in a kind")
        s.add_argument("actor")
        s.add_argument("kind")
        s.add_argument("--reason", required=True)

    s = sub.add_parser("scorecard", help="print each actor's record per kind")
    s.add_argument("kind", nargs="?")

    sub.add_parser("reap", help="close opens that never started within the lease")

    s = sub.add_parser("candidates", help="list models aop offers that are not configured as executors")
    s.add_argument(
        "--agent", nargs="+", action="extend", help="only these agents (default: the agents the project runs)"
    )
    s.add_argument("--all-agents", action="store_true", help="every agent aop offers")
    s.add_argument("--any-family", action="store_true", help="ignore the config's candidates.families patterns")
    s.add_argument("--limit", type=int, default=10, help="models per agent, priciest first (0 for all; default 10)")
    return top


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
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
