"""The append-only ledger: one JSON event per line, folded into task records."""

import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCHEMA = "delegate-event-1"
TASK_EVENTS = ("open", "started", "close", "verdict", "amend")
# The fields of a close an amend may correct, by path; an outcome is changed by an overturn or verdict.
AMENDABLE = ("cost.actor", "cost.lead", "minutes.run", "aop_runs", "commits", "report", "score.value", "score.reason")
OTHER_EVENTS = ("overturn", "suspend", "resume", "drop")
OUTCOMES = ("held", "steered", "corrected", "aborted", "awaiting")
FINAL = ("held", "steered", "corrected")


class LedgerError(Exception):
    """A ledger line that is not a valid event, or an event that does not fit its task."""


def parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise LedgerError(f"time without an offset: {value}")
    return moment


@dataclass
class Task:
    id: str
    kind: str
    cls: str
    actor: str
    reason: str
    opened: datetime
    open: dict
    started: dict | None = None
    close: dict | None = None
    verdict: dict | None = None
    overturned: bool = False
    redo_cost: float = 0.0
    amended: set = field(default_factory=set)  # the paths of the close that amends changed

    @property
    def outcome(self) -> str | None:
        """The latest judgement: an overturn, then a verdict, then the close."""
        if self.overturned:
            return "corrected"
        if self.verdict:
            return self.verdict["outcome"]
        return self.close["outcome"] if self.close else None

    @property
    def final(self) -> bool:
        return self.outcome in FINAL

    @property
    def holding(self) -> bool:
        """Whether its reservations are still held."""
        return self.close is None

    @property
    def defect(self) -> str | None:
        if self.overturned:
            return "overturned"
        record = self.verdict if self.verdict and self.verdict.get("outcome") == "corrected" else self.close
        return (record or {}).get("defect")

    @property
    def score(self) -> float | None:
        if self.overturned:
            return 0.0
        for record in (self.verdict, self.close):
            if record and record.get("score") is not None:
                return float(record["score"]["value"])
        return None

    @property
    def yield_(self) -> float | None:
        if self.overturned:
            return 0.0
        for record in (self.verdict, self.close):
            if record and record.get("yield") is not None:
                return float(record["yield"])
        return None

    def cost(self, part: str) -> float | None:
        """The recorded actor or lead cost, or None where unknown."""
        value = ((self.close or {}).get("cost") or {}).get(part)
        return None if value is None else float(value)


@dataclass
class State:
    events: list[dict] = field(default_factory=list)
    tasks: dict[str, Task] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


def fold(events: list[dict]) -> State:
    """Task records from events in ledger order."""
    state = State(events=events)
    for number, event in enumerate(events, 1):
        kind = event.get("event")
        if event.get("schema") != SCHEMA or kind not in TASK_EVENTS + OTHER_EVENTS or "at" not in event:
            raise LedgerError(f"line {number} is not a {SCHEMA} event")
        at = parse_time(event["at"])
        if kind == "open":
            if event["task"] in state.tasks:
                raise LedgerError(f"line {number} opens {event['task']} twice")
            state.tasks[event["task"]] = Task(
                event["task"], event["kind"], event["class"], event["actor"], event["reason"], at, event
            )
            state.order.append(event["task"])
            continue
        if kind == "overturn":
            root = state.tasks.get(event["root"])
            if root is None:
                raise LedgerError(f"line {number} overturns unknown task {event['root']}")
            root.overturned = True
            root.redo_cost += float(event.get("redo_cost") or 0)
            continue
        if kind in OTHER_EVENTS:
            continue
        task = state.tasks.get(event.get("task"))
        if task is None:
            raise LedgerError(f"line {number} names unknown task {event.get('task')}")
        if kind == "started":
            task.started = event
        elif kind == "close":
            task.close = event
        elif kind == "verdict":
            task.verdict = event
        elif kind == "amend":
            if task.close is None:
                raise LedgerError(f"line {number} amends {task.id}, which is not closed")
            task.close = amended(task.close, event.get("changes") or {}, number)
            task.amended |= set(event.get("changes") or {})
    return state


def amended(close: dict, changes: dict, number: int) -> dict:
    """A copy of CLOSE with each changed field set to its new value."""
    close = dict(close)
    for path, change in changes.items():
        if path not in AMENDABLE:
            raise LedgerError(f"line {number} amends {path}, which an amend cannot change")
        head, _, leaf = path.partition(".")
        if leaf:
            close[head] = {**(close.get(head) or {}), leaf: change["new"]}
        else:
            close[head] = change["new"]
    return close


class Ledger:
    """A ledger file. Use `locked()` to read and append atomically."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            events = []
            for number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise LedgerError(f"{self.path} line {number} is not JSON: {exc}") from exc
            yield Session(stream, events)

    def read(self) -> State:
        with self.locked() as session:
            return session.state


class Session:
    """The ledger's events under the lock, with appends folded in as they are written."""

    def __init__(self, stream, events: list[dict]):
        self.stream = stream
        self.state = fold(events)

    def append(self, event: dict) -> dict:
        event = {"schema": SCHEMA, **event}
        self.state = fold([*self.state.events, event])
        self.stream.write(json.dumps(event, sort_keys=True) + "\n")
        self.stream.flush()
        return event
