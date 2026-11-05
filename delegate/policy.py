"""The workflow's arithmetic: what each actor has done, who may take a task, and who gets it."""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from delegate.config import Config, ConfigError, Kind, parse_resource_spec
from delegate.ledger import State, Task

LEAD = "lead"
DROP_CONFIDENCE = 0.95  # drop when cost per success is this likely to exceed the lead's
FALLBACK_CONFIDENCE = 0.90  # drop when the success rate is this likely below one half
MIN_VERDICTS = 5  # verdicts an actor needs before it is compared
DRAWS = 4000


class SelectError(Exception):
    """A select that cannot be made as asked."""


@dataclass
class Env:
    """What select needs from outside the ledger, injectable for tests."""

    pool: callable  # (target, dollars, need) -> {"room": bool, "marginal": share of the run's dollars it costs}
    rng: random.Random


# ---- what an actor has done ----------------------------------------------------------------------


def kind_tasks(state: State, kind: str) -> list[Task]:
    return [state.tasks[t] for t in state.order if state.tasks[t].kind == kind]


def success(task: Task, kind: Kind) -> float | None:
    """A final task's success, from 0 to 1: a lead score counts as its fraction."""
    if not task.final:
        return None
    if task.outcome == "corrected":
        return 0.0
    if kind.measure == "lead-score" and task.score is not None:
        return task.score / 100
    return 1.0


def counts(tasks: list[Task], kind: Kind, prior: float = 0.5) -> tuple[float, float]:
    """Beta(a, b) over final tasks: successes and failures plus two prior tasks split at the PRIOR success rate,
    Beta(1, 1) at the default one half."""
    values = [v for t in tasks if (v := success(t, kind)) is not None]
    return 2 * prior + sum(values), 2 * (1 - prior) + sum(1 - v for v in values)


def shrunk(values: list[float], prior: float | None) -> float | None:
    """(n times the mean plus one prior task) over (n plus 1); the mean alone without a prior."""
    if prior is None:
        return sum(values) / len(values) if values else None
    return (sum(values) + prior) / (len(values) + 1)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


class Stats:
    """Per-actor figures for one kind, from the ledger."""

    def __init__(self, config: Config, state: State, kind: Kind, env: Env):
        self.config, self.state, self.kind, self.env = config, state, kind, env
        self.tasks = kind_tasks(state, kind.name)

    def of(self, actor: str) -> list[Task]:
        return [t for t in self.tasks if t.actor == actor]

    def final(self, actor: str) -> list[Task]:
        return [t for t in self.of(actor) if t.final]

    def in_class(self, actor: str, cls: str) -> list[Task]:
        return [t for t in self.of(actor) if t.cls == cls]

    def success_counts(self, actor: str, cls: str) -> tuple[float, float]:
        """Beta(a, b) of the actor's success in the class, its prior the actor's success rate in the kind's other
        classes, so a class it has not done starts from how it does in the others, and its own verdicts there soon
        decide."""
        a, b = counts([t for t in self.final(actor) if t.cls != cls], self.kind)
        return counts([t for t in self.final(actor) if t.cls == cls], self.kind, a / (a + b))

    def _costs(self, tasks: list[Task], part: str) -> list[float]:
        return [c for t in tasks if (c := t.cost(part)) is not None]

    def actor_prior(self, actor: str) -> float | None:
        """The actor's own mean actor cost over all its tasks, else the kind's mean actor cost."""
        own = [c for t in self.state.tasks.values() if t.actor == actor and (c := t.cost("actor")) is not None]
        if own:
            return mean(own)
        others = [t for t in self.tasks if (t.actor == LEAD) == (actor == LEAD)]
        return mean(self._costs(others, "actor"))

    def cost_parts(self, actor: str, cls: str) -> tuple[float | None, float, float]:
        """Expected actor, lead and redo cost of one task, each a mean over the actor's tasks in the class shrunk
        toward its mean in the kind's other classes, itself shrunk toward a prior; the actor part None when unknown."""
        here = self.in_class(actor, cls)
        others = [t for t in self.of(actor) if t.cls != cls]

        def part(costs, prior):
            return shrunk(costs(here), shrunk(costs(others), prior))

        actor_part = part(lambda ts: self._costs(ts, "actor"), self.actor_prior(actor))
        delegated = [t for t in self.tasks if t.actor != LEAD]
        lead_part = 0.0
        if actor != LEAD:
            lead_part = part(lambda ts: self._costs(ts, "lead"), mean(self._costs(delegated, "lead"))) or 0.0
        redo_prior = mean([t.redo_cost for t in self.tasks if t.final]) or 0.0
        redo_part = part(lambda ts: [t.redo_cost for t in ts if t.final], redo_prior) or 0.0
        return actor_part, lead_part, redo_part

    def marginal_cost(self, actor: str, cls: str, marginal: float = 1.0) -> float | None:
        """Expected cost of one task with the actor's part priced at MARGINAL."""
        actor_part, lead_part, redo_part = self.cost_parts(actor, cls)
        return None if actor_part is None else actor_part * marginal + lead_part + redo_part

    def expected_cost(self, actor: str, cls: str) -> float | None:
        """Expected total cost of one task: shrunk actor, lead and redo costs."""
        actor_part, lead_part, redo_part = self.cost_parts(actor, cls)
        return None if actor_part is None else actor_part + lead_part + redo_part

    def yield_factor(self, actor: str) -> float:
        """Yield kinds: the shrunk mean yield per successful task; 1 otherwise."""
        if self.kind.measure != "yield":
            return 1.0

        def per_success(tasks):
            done = [t for t in tasks if t.final and t.outcome != "corrected" and t.yield_ is not None]
            return [t.yield_ for t in done]

        prior = mean(per_success(self.tasks)) or 1.0
        return max(shrunk(per_success(self.of(actor)), prior), 1e-9)

    def cost_per_success_draws(
        self, actor: str, cls: str, rng: random.Random, n: int = DRAWS, marginal: float = 1.0
    ) -> list[float]:
        """Draws of cost per success, the actor's part priced at MARGINAL, the share of it the run really spends."""
        cost = self.marginal_cost(actor, cls, marginal)
        cost = 1.0 if cost is None else cost
        a, b = self.success_counts(actor, cls)
        factor = self.yield_factor(actor)
        return [cost / (max(rng.betavariate(a, b), 1e-12) * factor) for _ in range(n)]

    def cost_per_success(self, actor: str, cls: str) -> float | None:
        cost = self.expected_cost(actor, cls)
        if cost is None:
            return None
        a, b = self.success_counts(actor, cls)
        return cost / (a / (a + b) * self.yield_factor(actor))


# ---- executor standing in a kind -----------------------------------------------------------------


def _events(state: State, name: str, kind: str, actor: str | None = None):
    for event in state.events:
        if event.get("event") != name:
            continue
        if event.get("kind") == kind and (actor is None or event.get("actor") == actor):
            yield event


def last_time(events) -> datetime | None:
    times = [datetime.fromisoformat(e["at"]) for e in events]
    return max(times) if times else None


def judged_at(task: Task) -> datetime:
    record = task.verdict or task.close
    return datetime.fromisoformat(record["at"])


def standing(stats: Stats, actor: str) -> str:
    """active, dropped or suspended. Every configured executor is active in every kind until a rule takes it out:
    its record in each class, not a trial, decides how much work it gets."""
    kind, state = stats.kind, stats.state
    resumed = last_time(_events(state, "resume", kind.name, actor))
    dropped = last_time(_events(state, "drop", kind.name, actor))
    if dropped and (resumed is None or dropped > resumed):
        return "dropped"
    suspended = last_time(_events(state, "suspend", kind.name, actor))
    if suspended and (resumed is None or suspended > resumed):
        return "suspended"
    if kind.check != "gated":
        for task in stats.final(actor):
            if task.defect == "integrity" and (resumed is None or judged_at(task) > resumed):
                return "suspended"
    return "active"


def prob_exceeds(draws_a: list[float], draws_b: list[float]) -> float:
    return sum(x > y for x, y in zip(draws_a, draws_b, strict=True)) / len(draws_a)


def beta_below(a: float, b: float, threshold: float, rng: random.Random, n: int = DRAWS) -> float:
    return sum(rng.betavariate(a, b) < threshold for _ in range(n)) / n


def should_drop(stats: Stats, actor: str, cls: str) -> str | None:
    """The drop rule's reason, or None.

    An executor is dropped when its success rate over the kind is probably below one half, or,
    once it and the lead each have MIN_VERDICTS verdicts, when doing the task as the lead is probably cheaper per
    success. It is not dropped for costing more than another executor: the draw already prefers the cheaper one,
    and a pool the cheapest executor runs on can be exhausted.
    """
    rng = random.Random(f"{stats.kind.name}:{actor}:{len(stats.state.events)}")
    mine = stats.final(actor)
    if mine:
        a, b = counts(mine, stats.kind)
        p = beta_below(a, b, 0.5, rng)
        if p > FALLBACK_CONFIDENCE:
            return f"success rate {p:.0%} likely below one half"
    if len(mine) >= MIN_VERDICTS and len(stats.final(LEAD)) >= MIN_VERDICTS:
        p = prob_exceeds(stats.cost_per_success_draws(actor, cls, rng), stats.cost_per_success_draws(LEAD, cls, rng))
        if p > DROP_CONFIDENCE:
            return f"cost per success {p:.0%} likely above the lead's"
    return None


# ---- resources -----------------------------------------------------------------------------------


def reserve(config: Config, state: State, specs: list[str]) -> tuple[dict | None, str | None]:
    """The reservation for SPECS against tasks still holding theirs, or None and the busy resource."""
    held: dict[str, list] = {}
    for task in state.tasks.values():
        if task.holding:
            for name, value in (task.open.get("resources") or {}).items():
                held.setdefault(name, []).append(value)
    reservation = {}
    for spec in specs:
        name, amount = parse_resource_spec(spec)
        resource = config.resources.get(name)
        if resource is None:
            raise ConfigError(f"unknown resource {name!r}")
        taken = held.get(name, [])
        if resource.instances:
            if amount is not None:
                raise ConfigError(f"{name} has named instances, one task each; {spec!r} asks for an amount")
            free = [i for i in resource.instances if i not in taken]
            if not free:
                return None, name
            reservation[name] = free[0]
            continue
        # A counted resource takes whole slots, such as all of a live client a relaunch would end every job on.
        need = amount if amount is not None else 1.0
        if not resource.amounts:
            if need != int(need):
                raise ConfigError(f"{name} is counted in whole slots; {spec!r} asks for part of one")
            need = int(need)
        if need > resource.capacity:
            raise ConfigError(f"{spec!r} asks for more than {name}'s capacity of {resource.capacity:g}")
        if sum(float(v) for v in taken) + need > resource.capacity:
            return None, name
        reservation[name] = need
    return reservation, None


# ---- the choice ----------------------------------------------------------------------------------


def busy_until_verdict(stats: Stats, actor: str) -> bool:
    """No further task in the kind before a first verdict there."""
    mine = stats.of(actor)
    if any(t.final for t in mine):
        return False
    return any(t.outcome != "aborted" for t in mine)


def select(
    config: Config,
    state: State,
    kind_name: str,
    cls: str,
    now: datetime,
    env: Env,
    resources: list[str] | None = None,
    token_cap: float | None = None,
    retry: str | None = None,
    since: datetime | None = None,
    assign: str | None = None,
    why: str | None = None,
) -> tuple[dict, list[dict]]:
    """The answer, and the events to append: drops the rule made, then the open (none for wait)."""
    kind = config.kinds.get(kind_name)
    if kind is None:
        raise SelectError(f"unknown kind {kind_name!r}")
    if cls not in kind.classes:
        raise SelectError(f"unknown class {cls!r} for kind {kind_name}; classes are {', '.join(kind.classes)}")
    stats = Stats(config, state, kind, env)
    specs = resources if resources is not None else kind.resources
    reservation, busy = reserve(config, state, specs)
    excluded = state.tasks[retry].actor if retry and retry in state.tasks else None
    if retry and retry not in state.tasks:
        raise SelectError(f"unknown task {retry!r} to retry")
    events: list[dict] = []

    def answer(kind_of_answer, actor=None, reason=None, **extra):
        result = {"answer": kind_of_answer, "kind": kind_name, "class": cls}
        if kind_of_answer == "wait":
            waiting = {**result, "waiting_for": extra["waiting_for"], "since": (since or now).isoformat()}
            # A pool check that could not answer is a failure to fix, not quota to wait for.
            if failures := [r for room in refused.values() if room["state"] == "error" for r in room["reasons"]]:
                waiting = {**waiting, "answer": "error", "error": "; ".join(dict.fromkeys(failures))}
            return ({**waiting, "refused": refused} if refused else waiting), events
        task_id = f"{kind_name}-{sum(t.kind == kind_name for t in state.tasks.values()) + 1:04d}"
        actor_part, lead_part, _ = stats.cost_parts(actor, cls)
        expected = {"actor_cost": actor_part, "lead_cost": None if actor == LEAD else lead_part}
        event = {
            "event": "open",
            "at": now.isoformat(),
            "task": task_id,
            "kind": kind_name,
            "class": cls,
            "actor": actor,
            "reason": reason,
            "resources": reservation or {},
            "token_cap": token_cap,
            "expected": expected,
        }
        for key in ("seed", "retry_of", "why", "marginal"):
            if extra.get(key) is not None:
                event[key] = extra[key]
        # The account of a pool with several, such as Claude's, that the pool check chose for the run to land on.
        executor = config.executors.get(actor)
        if executor and executor.pool:
            account = (checked.get(actor) or pool_room(executor)).get("account")
            if account:
                event["account"] = account
        events.append(event)
        return {
            **result,
            "task": task_id,
            "actor": actor,
            "reason": reason,
            "resources": reservation or {},
            **({"account": event["account"]} if "account" in event else {}),
            **({"executor": vars(config.executors[actor])} if actor in config.executors else {}),
        }, events

    running: dict[str, list[Task]] = {}
    for task in state.tasks.values():
        if task.holding and (pool := getattr(config.executors.get(task.actor), "pool", None)):
            running.setdefault(pool, []).append(task)

    def pool_dollars(name: str, cost: float | None, lead_cost: float | None) -> float | None:
        """What a run spends on its actor's pool: its actor cost, and its lead cost when the lead shares the pool."""
        if cost is None:
            return None
        return cost + (lead_cost or 0) if config.executors[name].pool == config.lead_pool else cost

    def check_pool(pool: str, dollars: float | None) -> dict:
        """The pool check for one more run spending DOLLARS on POOL, with the project's runs already going there,
        which the pool's reading cannot show yet, and for a pool with no measured price its need in points for each
        of those runs."""
        going = running.get(pool, [])
        for task in going:
            expected = task.open.get("expected") or {}
            spent = pool_dollars(task.actor, expected.get("actor_cost"), expected.get("lead_cost"))
            dollars = spent if dollars is None else dollars + (spent or 0)
        need, runs = config.pool_need.get(pool), 1 + len(going)
        if isinstance(need, dict):
            need = {length: points * runs for length, points in need.items()}
        elif need is not None:
            need *= runs
        return {**env.pool(pool, dollars, need), "dollars": dollars, "need": need}

    # Executors otherwise eligible, and the lead past the wait deadline, whose pool check refused them, with what
    # was asked and why, for a wait answer.
    refused: dict[str, dict] = {}

    def refuse(name: str, room: dict):
        refused[name] = {
            "pool": config.lead_pool if name == LEAD else config.executors[name].pool,
            "dollars": None if room.get("dollars") is None else round(room["dollars"], 2),
            **({"need": room["need"]} if room.get("need") is not None else {}),
            "state": room.get("state"),
            "reasons": room.get("reasons") or [],
        }

    checked: dict[str, dict] = {}  # each executor's pool check in this select

    def pool_room(executor) -> dict:
        if not executor.pool:
            return {"room": True, "marginal": 1.0}
        actor_part, lead_part, _ = stats.cost_parts(executor.name, cls)
        checked[executor.name] = check_pool(executor.pool, pool_dollars(executor.name, actor_part, lead_part))
        return checked[executor.name]

    if assign:
        executor = config.executors.get(assign)
        if executor is None:
            raise SelectError(f"unknown executor {assign!r}")
        if not why:
            raise SelectError("an assignment needs --why, the user's or lead's reason")
        if (position := standing(stats, assign)) != "active":
            raise SelectError(f"{assign} is {position} in {kind_name}; resume it before assigning it work")
        if not set(kind.capabilities) <= set(executor.capabilities):
            raise SelectError(
                f"{assign} lacks capabilities {sorted(set(kind.capabilities) - set(executor.capabilities))}"
            )
        if busy:
            return answer("wait", waiting_for=busy)
        return answer("delegate", assign, "assigned", why=why)

    def lead(reason="lead"):
        if busy:
            return answer("wait", waiting_for=busy)
        return answer("lead", LEAD, reason)

    if kind.state_of(cls) != "on":
        return lead()
    eligible, pool_blocked = [], []
    for name, executor in config.executors.items():
        if standing(stats, name) != "active" or busy_until_verdict(stats, name):
            continue
        if name == excluded or not set(kind.capabilities) <= set(executor.capabilities):
            continue
        if reason := should_drop(stats, name, cls):
            events.append(
                {
                    "event": "drop",
                    "at": now.isoformat(),
                    "actor": name,
                    "kind": kind_name,
                    "reason": f"drop rule: {reason}",
                }
            )
            continue
        eligible.append(name)
    rooms = {}
    for name in list(eligible):
        rooms[name] = pool_room(config.executors[name])
        if not rooms[name]["room"]:
            refuse(name, rooms[name])
            eligible.remove(name)
            pool_blocked.append(config.executors[name].pool)
    if not eligible:
        if busy or pool_blocked:
            deadline = timedelta(minutes=config.wait_deadline_minutes)
            if since and now - since >= deadline and not busy:
                lead_room = (
                    check_pool(config.lead_pool, stats.expected_cost(LEAD, cls)) if config.lead_pool else {"room": True}
                )
                if lead_room["room"]:
                    return lead()
                refuse(LEAD, lead_room)
            return answer("wait", waiting_for=busy or sorted(set(pool_blocked))[0])
        return lead()
    if busy:
        return answer("wait", waiting_for=busy)

    # Quota due to expire unused costs nothing, so a run on a pool behind its pace is priced at its marginal share
    # of the actor's cost, the success rate still counting; not on the lead's pool, whose quota the lead needs, and
    # not in a deferred kind, whose choice is the cheapest by its real cost.
    marginal = {
        n: 1.0
        if kind.check == "deferred" or config.executors[n].pool == config.lead_pool
        else rooms[n].get("marginal", 1.0)
        for n in eligible
    }
    discounts = {n: round(m, 3) for n, m in marginal.items() if m < 1}
    # Every executor gets one task in a kind before the record decides, and no second until that one is judged.
    untried = [n for n in eligible if not stats.of(n)]
    if untried:
        ranked = sorted(
            untried,
            key=lambda n: (stats.expected_cost(n, cls) is None, stats.marginal_cost(n, cls, marginal[n]) or 0, n),
        )
        return answer("delegate", ranked[0], "warm-up", marginal=discounts or None)
    if kind.check == "deferred":
        ranked = sorted(
            eligible, key=lambda n: (stats.cost_per_success(n, cls) is None, stats.cost_per_success(n, cls) or 0, n)
        )
        return answer("delegate", ranked[0], "cheapest")
    seed = env.rng.randrange(2**32)
    rng = random.Random(seed)
    draws = {n: stats.cost_per_success_draws(n, cls, rng, 1, marginal[n])[0] for n in sorted(eligible)}
    best = min(draws, key=lambda n: (draws[n], n))
    return answer("delegate", best, "draw", seed=seed, retry_of=retry, marginal=discounts or None)


def scorecard(config: Config, state: State, env: Env, only: str | None = None) -> list[dict]:
    rows = []
    for name, kind in config.kinds.items():
        if only and name != only:
            continue
        stats = Stats(config, state, kind, env)
        actors = sorted({t.actor for t in stats.tasks} | set(config.executors))
        for actor in actors:
            mine = stats.of(actor)
            final = stats.final(actor)
            scores = [t.score for t in final if t.score is not None]
            rows.append(
                {
                    "kind": name,
                    "actor": actor,
                    "standing": "lead" if actor == LEAD else standing(stats, actor),
                    "tasks": len(mine),
                    "verdicts": len(final),
                    "successes": round(sum(success(t, kind) for t in final), 2),
                    "cost_per_success": stats.cost_per_success(actor, kind.classes[0]) if final else None,
                    "integrity": sum(t.defect == "integrity" for t in final),
                    "scores": {"min": min(scores), "median": median(scores), "max": max(scores)} if scores else None,
                    "yield": sum(t.yield_ or 0 for t in final) if kind.measure == "yield" else None,
                    "held_minutes": round(
                        sum(sum(((t.close or {}).get("minutes") or {}).get("held", {}).values()) for t in mine), 1
                    ),
                    "open": sum(not t.final and t.outcome != "aborted" for t in mine),
                }
            )
    return rows
