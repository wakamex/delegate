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
    """Pool room by target, as usage-burn's check gives it: True, False, or "error" for a check that could not
    answer. A target nobody declared is a bug in the test and raises. BUDGET is the most dollars a target admits,
    and MAX_NEED the most points for a pool with no measured price."""

    def __init__(self, rooms: dict[str, bool], marginal=None, budget=None, max_need=None, accounts=None):
        self.rooms, self.marginal, self.calls, self.accounts = rooms, marginal or {}, [], accounts or {}
        self.budget, self.max_need = budget or {}, max_need or {}

    def __call__(self, target, dollars, need):
        if target not in self.rooms:
            raise AssertionError(f"pool check for undeclared target {target!r}")
        needs = list(need.values()) if isinstance(need, dict) else [need or 0]
        if (dollars is not None and dollars < 0) or min(needs) < 0:
            raise AssertionError("negative cost")
        self.calls.append((target, None if dollars is None else round(dollars, 6), need))
        fits = (target not in self.budget or (dollars or 0) <= self.budget[target]) and (
            target not in self.max_need or max(needs) <= self.max_need[target]
        )
        if self.rooms[target] == "error":
            return {"room": False, "marginal": 1.0, "state": "error", "reasons": [f"could not check {target}"]}
        room = self.rooms[target] and fits
        return {
            "room": room,
            "marginal": self.marginal.get(target, 1.0),
            "state": "admitted" if room else "refused",
            "reasons": [] if room else [f"{target} has no room"],
            **({"account": self.accounts[target]} if target in self.accounts else {}),
        }


def env(rooms=None, marginal=None, seed=1, budget=None, max_need=None, accounts=None) -> Env:
    rooms = {"cheap-pool": True, "dear-pool": True, "lead-pool": True} if rooms is None else rooms
    return Env(pool=FakePools(rooms, marginal, budget, max_need, accounts), rng=random.Random(seed))


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
