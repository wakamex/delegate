"""Shared test helpers: configs in a temporary directory, ledgers built event by event, strict fakes."""

import json
import random
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from delegate.config import load
from delegate.ledger import SCHEMA, fold
from delegate.policy import Env

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

BASE = """
project = "test"
ledger = "ledger.jsonl"
lease_minutes = 30
wait_deadline_minutes = 60

[lead]
pool = "lead-pool"

[resources]
checkout = ["a", "b"]
memory = { capacity = 31 }

[executors.cheap]
agent = "devin"
model = "swe-2-high"
pool = "cheap-pool"

[executors.dear]
agent = "codex"
model = "gpt-6.1-sol"
pool = "dear-pool"

[kinds.tool]
check = "gated"
state = "on"
measure = "pass-fail"
classes = ["bounded", "hard"]

[kinds.port]
check = "reviewed"
state = "on"
measure = "lead-score"
classes = ["bounded", "first-reading", "redesign"]
class_state = { redesign = "lead-only" }

[kinds.recon]
check = "deferred"
state = "on"
measure = "pass-fail"
classes = ["bounded"]

[kinds.probe]
check = "reviewed"
state = "on"
measure = "pass-fail"
classes = ["bounded"]
investigation = true
"""


def make_config(text: str = BASE, directory: Path | None = None):
    directory = directory or Path(tempfile.mkdtemp())
    path = directory / "delegate.toml"
    path.write_text(text)
    return load(path)


class FakePools:
    """Pool room by target; a target nobody declared is a bug in the test and raises."""

    def __init__(self, rooms: dict[str, bool], catch_up: set[str] = frozenset(), short_full: set[str] = frozenset()):
        self.rooms, self.catch_up, self.short_full, self.calls = rooms, set(catch_up), set(short_full), []

    def __call__(self, target, need, need_short):
        if target not in self.rooms:
            raise AssertionError(f"pool check for undeclared target {target!r}")
        if need is not None and need < 0:
            raise AssertionError("negative need")
        if need_short is not None and need_short < 0:
            raise AssertionError("negative short need")
        self.calls.append((target, need, need_short))
        # A target in SHORT_FULL has room for the default short need but not for a larger one.
        short_room = need_short is None or target not in self.short_full
        return {"room": self.rooms[target] and short_room, "catch_up": target in self.catch_up}


def env(rooms=None, catch_up=(), seed=1, short_full=()) -> Env:
    rooms = {"cheap-pool": True, "dear-pool": True, "lead-pool": True} if rooms is None else rooms
    return Env(pool=FakePools(rooms, set(catch_up), set(short_full)), rng=random.Random(seed))


class Book:
    """A ledger built in memory, event by event."""

    def __init__(self):
        self.events, self.clock, self.counter = [], NOW - timedelta(days=10), 0

    def tick(self):
        self.clock += timedelta(minutes=1)
        return self.clock.isoformat()

    def add(self, **event):
        self.events.append({"schema": SCHEMA, "at": self.tick(), **event})
        return self

    def task(
        self,
        kind,
        actor,
        outcome,
        cls="bounded",
        reason="draw",
        cost=1.0,
        lead_cost=0.0,
        score=None,
        yield_=None,
        defect=None,
        verdict=None,
    ):
        """A task opened, started and closed; deferred tasks close awaiting and take VERDICT."""
        self.counter += 1
        task = f"{kind}-{self.counter:04d}"
        self.add(event="open", task=task, kind=kind, **{"class": cls}, actor=actor, reason=reason, resources={})
        self.add(event="started", task=task, aop_run=f"run-{task}")
        close = {"outcome": outcome, "cost": {"actor": cost, "lead": lead_cost}}
        if outcome == "corrected":
            close.update(caught_by="review", defect=defect or "scope")
        if score is not None:
            close["score"] = {"value": score, "reason": "test"}
        if yield_ is not None:
            close["yield"] = yield_
        self.add(event="close", task=task, **close)
        if verdict:
            extra = {"caught_by": "deferred-check", "defect": defect or "scope"} if verdict == "corrected" else {}
            self.add(event="verdict", task=task, outcome=verdict, evidence=["block 1"], **extra)
        return task

    def state(self):
        return fold(self.events)

    def write(self, path: Path):
        path.write_text("".join(json.dumps(e) + "\n" for e in self.events))
