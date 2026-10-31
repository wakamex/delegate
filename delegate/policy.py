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
CLASS_VERDICTS = 3  # verdicts in a class before cost is taken per class
DRAWS = 4000
# Points of each daily or 5-hour window an executor's first task in a kind must leave room for. Its cost is unknown,
# and first runs have taken about half of such a window: two concurrent Devin runs used up its daily window in an hour.
WARM_UP_NEED_SHORT = 50
# usage-burn's default needs of a run, in points of each long (3 days or more) and short window.
DEFAULT_NEED, DEFAULT_NEED_SHORT = 5.0, 25.0


class SelectError(Exception):
    """A select that cannot be made as asked."""


@dataclass
class Env:
    """What select needs from outside the ledger, injectable for tests."""

    pool: callable  # (target, need, need_short) -> {"room": bool, "catch_up": bool}
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


def counts(tasks: list[Task], kind: Kind) -> tuple[float, float]:
    """Beta(a, b) over final tasks: one plus successes, one plus failures."""
    values = [v for t in tasks if (v := success(t, kind)) is not None]
    return 1 + sum(values), 1 + sum(1 - v for v in values)


def shrunk(values: list[float], prior: float | None) -> float | None:
    """(n times the mean plus one prior task) over (n plus 1); the mean alone without a prior."""
    if prior is None:
        return sum(values) / len(values) if values else None
    return (sum(values) + prior) / (len(values) + 1)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


class Stats:
    """Per-actor figures for one kind, from the ledger."""

    def __init__(self, config: Config, state: State, kind: Kind, env: Env, token_cap: float | None = None):
        self.config, self.state, self.kind, self.env, self.token_cap = config, state, kind, env, token_cap
        self.tasks = kind_tasks(state, kind.name)

    def of(self, actor: str) -> list[Task]:
        return [t for t in self.tasks if t.actor == actor]

    def final(self, actor: str) -> list[Task]:
        return [t for t in self.of(actor) if t.final]

    def scope(self, actor: str, cls: str) -> list[Task]:
        """The actor's tasks in the class once it has CLASS_VERDICTS there, else in the kind."""
        in_class = [t for t in self.of(actor) if t.cls == cls]
        return in_class if sum(t.final for t in in_class) >= CLASS_VERDICTS else self.of(actor)

    def _costs(self, tasks: list[Task], part: str) -> list[float]:
        skip_catch_up = part == "actor"
        return [c for t in tasks if (c := t.cost(part)) is not None and not (skip_catch_up and t.reason == "catch-up")]

    def actor_prior(self, actor: str) -> float | None:
        """The actor's own mean actor cost over all its tasks, else the kind's mean actor cost."""
        own = [
            c
            for t in self.state.tasks.values()
            if t.actor == actor
            for c in [t.cost("actor")]
            if c is not None and t.reason != "catch-up"
        ]
        if own:
            return mean(own)
        others = [t for t in self.tasks if (t.actor == LEAD) == (actor == LEAD)]
        return mean(self._costs(others, "actor"))

    def expected_cost(self, actor: str, cls: str) -> float | None:
        """Expected total cost of one task: shrunk actor, lead and redo costs."""
        scope = self.scope(actor, cls)
        actor_part = shrunk(self._costs(scope, "actor"), self.actor_prior(actor))
        if actor_part is None:
            return None
        delegated = [t for t in self.tasks if t.actor != LEAD]
        lead_part = 0.0
        if actor != LEAD:
            lead_part = shrunk(self._costs(scope, "lead"), mean(self._costs(delegated, "lead"))) or 0.0
        redo_prior = mean([t.redo_cost for t in self.tasks if t.final]) or 0.0
        redo_part = shrunk([t.redo_cost for t in scope if t.final], redo_prior) or 0.0
        return actor_part + lead_part + redo_part

    def yield_factor(self, actor: str) -> float:
        """Yield kinds: the shrunk mean yield per successful task; 1 otherwise."""
        if self.kind.measure != "yield":
            return 1.0

        def per_success(tasks):
            done = [t for t in tasks if t.final and t.outcome != "corrected" and t.yield_ is not None]
            return [t.yield_ for t in done]

        prior = mean(per_success(self.tasks)) or 1.0
        return max(shrunk(per_success(self.of(actor)), prior), 1e-9)

    def cost_per_success_draws(self, actor: str, cls: str, rng: random.Random, n: int = DRAWS) -> list[float]:
        cost = self.expected_cost(actor, cls)
        cost = 1.0 if cost is None else cost
        a, b = counts(self.final(actor), self.kind)
        factor = self.yield_factor(actor)
        return [cost / (max(rng.betavariate(a, b), 1e-12) * factor) for _ in range(n)]

    def cost_per_success(self, actor: str, cls: str) -> float | None:
        cost = self.expected_cost(actor, cls)
        if cost is None:
            return None
        a, b = counts(self.final(actor), self.kind)
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
    """admitted, trial, failed, candidate, dropped or suspended."""
    kind, state = stats.kind, stats.state
    resumed = last_time(_events(state, "resume", kind.name, actor))
    dropped = last_time(_events(state, "drop", kind.name, actor))
    if dropped and (resumed is None or dropped > resumed):
        return "dropped"
    if kind.check != "gated":
        for task in stats.final(actor):
            if task.defect == "integrity" and (resumed is None or judged_at(task) > resumed):
                return "suspended"
    if kind.check == "gated" or resumed is not None:
        return "admitted"
    trials = [t for t in stats.of(actor) if t.reason == "admit"]
    judged = [t for t in trials if t.final]
    if _trial_passed(judged, kind):
        return "admitted"
    if _trial_failed(judged, kind):
        return "failed"
    return "trial" if trials else "candidate"


def _trial_passed(tasks: list[Task], kind: Kind) -> bool:
    successes = [t.outcome in ("held", "steered") for t in tasks]
    if kind.check == "reviewed":
        return sum(successes[:3]) >= 2
    return sum(successes) >= 2 and all(successes)


def _trial_failed(tasks: list[Task], kind: Kind) -> bool:
    failures = sum(t.outcome == "corrected" for t in tasks)
    return failures >= 2 if kind.check == "reviewed" else failures >= 1


def prob_exceeds(draws_a: list[float], draws_b: list[float]) -> float:
    return sum(x > y for x, y in zip(draws_a, draws_b, strict=True)) / len(draws_a)


def beta_below(a: float, b: float, threshold: float, rng: random.Random, n: int = DRAWS) -> float:
    return sum(rng.betavariate(a, b) < threshold for _ in range(n)) / n


def should_drop(stats: Stats, actor: str, cls: str) -> str | None:
    """The drop rule's reason, or None.

    An executor is dropped when its success rate is probably below one half over its tasks after admission, or,
    once it and the lead each have MIN_VERDICTS verdicts, when doing the task as the lead is probably cheaper per
    success. It is not dropped for costing more than another executor: the draw already prefers the cheaper one,
    and a pool the cheapest executor runs on can be exhausted.
    """
    rng = random.Random(f"{stats.kind.name}:{actor}:{len(stats.state.events)}")
    mine = stats.final(actor)
    after = [t for t in mine if t.reason != "admit"]
    if after:
        a, b = counts(after, stats.kind)
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
            free = [i for i in resource.instances if i not in taken]
            if not free:
                return None, name
            reservation[name] = free[0]
        elif resource.amounts:
            need = amount if amount is not None else 1.0
            if sum(float(v) for v in taken) + need > resource.capacity:
                return None, name
            reservation[name] = need
        else:
            if len(taken) + 1 > resource.capacity:
                return None, name
            reservation[name] = 1
    return reservation, None


# ---- the choice ----------------------------------------------------------------------------------


def class_allows(stats: Stats, actor: str, cls: str) -> bool:
    """Reviewed and deferred kinds: a held or steered task of the class or the one below."""
    if stats.kind.check == "gated":
        return True
    index = stats.kind.classes.index(cls)
    if index == 0:
        return True
    allowed = set(stats.kind.classes[index - 1 : index + 1])
    return any(t.cls in allowed and t.outcome in ("held", "steered") for t in stats.final(actor))


def busy_until_verdict(stats: Stats, actor: str) -> bool:
    """No further task in the kind before a first verdict there."""
    mine = stats.of(actor)
    if any(t.final for t in mine):
        return False
    return any(t.outcome != "aborted" for t in mine)


def need_points(config: Config, pool: str | None, dollars: float | None) -> float | None:
    """The points of each long window a run takes: its dollars at the pool's rate, else the pool's fixed need."""
    rate = config.dollars_per_point.get(pool or "")
    if rate is None:
        return config.pool_need.get(pool or "")
    return None if dollars is None else dollars / rate


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
    admit: str | None = None,
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
    stats = Stats(config, state, kind, env, token_cap)
    specs = resources if resources is not None else kind.resources
    reservation, busy = reserve(config, state, specs)
    excluded = state.tasks[retry].actor if retry and retry in state.tasks else None
    if retry and retry not in state.tasks:
        raise SelectError(f"unknown task {retry!r} to retry")
    events: list[dict] = []

    def answer(kind_of_answer, actor=None, reason=None, **extra):
        result = {"answer": kind_of_answer, "kind": kind_name, "class": cls}
        if kind_of_answer == "wait":
            return {**result, "waiting_for": extra["waiting_for"], "since": (since or now).isoformat()}, events
        task_id = f"{kind_name}-{sum(t.kind == kind_name for t in state.tasks.values()) + 1:04d}"
        expected = {
            "actor_cost": stats.expected_cost(actor, cls),
            "lead_cost": None,
            "pool_points": extra.get("points"),
        }
        if actor != LEAD:
            expected["lead_cost"] = (stats.expected_cost(actor, cls) or 0) - (
                shrunk(stats._costs(stats.scope(actor, cls), "actor"), stats.actor_prior(actor)) or 0
            )
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
        for key in ("seed", "retry_of", "why"):
            if extra.get(key) is not None:
                event[key] = extra[key]
        events.append(event)
        return {
            **result,
            "task": task_id,
            "actor": actor,
            "reason": reason,
            "resources": reservation or {},
            **({"executor": vars(config.executors[actor])} if actor in config.executors else {}),
        }, events

    running: dict[str, list[Task]] = {}
    for task in state.tasks.values():
        if task.holding and (pool := getattr(config.executors.get(task.actor), "pool", None)):
            running.setdefault(pool, []).append(task)

    def running_need(task: Task) -> float:
        """A running task's need of each long window: as reserved at its open, else its pool's fixed need."""
        points = (task.open.get("expected") or {}).get("pool_points")
        if points is not None:
            return float(points)
        return config.pool_need.get(config.executors[task.actor].pool, DEFAULT_NEED)

    def pool_room(executor, need_short=None) -> dict:
        cost = stats.expected_cost(executor.name, cls)
        points = need_points(config, executor.pool, cost)
        if executor.pool and executor.pool == config.lead_pool and executor.pool in config.dollars_per_point:
            lead_cost = (stats.expected_cost(executor.name, cls) or 0) - (
                shrunk(stats._costs(stats.scope(executor.name, cls), "actor"), stats.actor_prior(executor.name)) or 0
            )
            extra = need_points(config, executor.pool, lead_cost)
            points = None if points is None else points + (extra or 0)
        if not executor.pool:
            return {"room": True, "catch_up": False, "points": points}
        need, short_each = points, config.pool_need_short.get(executor.pool)
        if need_short is None:
            need_short = short_each
        # usage-burn cannot see this project's runs already going on the pool, so their needs join this run's.
        if going := running.get(executor.pool):
            need = (DEFAULT_NEED if points is None else points) + sum(running_need(t) for t in going)
            each = DEFAULT_NEED_SHORT if short_each is None else short_each
            need_short = (each if need_short is None else need_short) + each * len(going)
        return {**env.pool(executor.pool, need, need_short), "points": points}

    if admit:
        executor = config.executors.get(admit)
        if executor is None:
            raise SelectError(f"unknown executor {admit!r}")
        if standing(stats, admit) in ("admitted", "suspended", "dropped"):
            raise SelectError(f"{admit} is {standing(stats, admit)} in {kind_name}; admission is for new candidates")
        if kind.check == "reviewed" and cls != kind.classes[0]:
            raise SelectError(f"a reviewed admission trial takes the lowest class, {kind.classes[0]}")
        if not set(kind.capabilities) <= set(executor.capabilities):
            raise SelectError(
                f"{admit} lacks capabilities {sorted(set(kind.capabilities) - set(executor.capabilities))}"
            )
        if busy:
            return answer("wait", waiting_for=busy)
        room = pool_room(executor)
        if not room["room"]:
            return answer("wait", waiting_for=executor.pool)
        return answer("admit", admit, "admit", points=room["points"])

    if assign:
        executor = config.executors.get(assign)
        if executor is None:
            raise SelectError(f"unknown executor {assign!r}")
        if admit:
            raise SelectError("--assign and --admit cannot be combined")
        if not why:
            raise SelectError("an assignment needs --why, the user's or lead's reason")
        if (position := standing(stats, assign)) != "admitted":
            raise SelectError(
                f"{assign} is {position} in {kind_name}; assign an admitted executor, or --admit a new one"
            )
        if not set(kind.capabilities) <= set(executor.capabilities):
            raise SelectError(
                f"{assign} lacks capabilities {sorted(set(kind.capabilities) - set(executor.capabilities))}"
            )
        if busy:
            return answer("wait", waiting_for=busy)
        points = need_points(config, executor.pool, stats.expected_cost(assign, cls))
        return answer("delegate", assign, "assigned", points=points, why=why)

    def lead(reason="lead"):
        if busy:
            return answer("wait", waiting_for=busy)
        return answer("lead", LEAD, reason)

    if kind.state_of(cls) != "on":
        return lead()
    eligible, pool_blocked = [], []
    for name, executor in config.executors.items():
        position = standing(stats, name)
        if position != "admitted":
            continue
        if name == excluded or not set(kind.capabilities) <= set(executor.capabilities):
            continue
        if not class_allows(stats, name, cls) or busy_until_verdict(stats, name):
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
    # An executor's first task in a kind waits for a pool with no run of this project on it and room in its short
    # windows for a run of unknown cost, so warm-up does not stack a trial on a small subscription already in use.
    rooms = {}
    for name in list(eligible):
        executor, untried = config.executors[name], not stats.of(name)
        if untried and executor.pool in running:
            rooms[name] = {"room": False}
        else:
            rooms[name] = pool_room(executor, WARM_UP_NEED_SHORT if untried else None)
        if not rooms[name]["room"]:
            eligible.remove(name)
            pool_blocked.append(config.executors[name].pool)
    if not eligible:
        if busy or pool_blocked:
            deadline = timedelta(minutes=config.wait_deadline_minutes)
            if since and now - since >= deadline and not busy:
                lead_room = env.pool(config.lead_pool, None, None) if config.lead_pool else {"room": True}
                if lead_room["room"]:
                    return lead()
            return answer("wait", waiting_for=busy or sorted(set(pool_blocked))[0])
        return lead()
    if busy:
        return answer("wait", waiting_for=busy)

    def pick(names: list[str], reason: str):
        return answer("delegate", names[0], reason, points=rooms[names[0]]["points"])

    if kind.check == "deferred":
        ranked = sorted(
            eligible, key=lambda n: (stats.cost_per_success(n, cls) is None, stats.cost_per_success(n, cls) or 0, n)
        )
        return pick(ranked, "cheapest")
    pool_reason = None
    if kind.check == "gated":
        catch_up = [n for n in eligible if rooms[n].get("catch_up")]
        if catch_up:
            eligible, pool_reason = catch_up, "catch-up"
    untried = [n for n in eligible if not stats.of(n)]
    if untried:
        ranked = sorted(
            untried, key=lambda n: (stats.expected_cost(n, cls) is None, stats.expected_cost(n, cls) or 0, n)
        )
        return pick(ranked, pool_reason or "warm-up")
    seed = env.rng.randrange(2**32)
    rng = random.Random(seed)
    draws = {n: stats.cost_per_success_draws(n, cls, rng, 1)[0] for n in sorted(eligible)}
    best = min(draws, key=lambda n: (draws[n], n))
    return answer("delegate", best, pool_reason or "draw", points=rooms[best]["points"], seed=seed, retry_of=retry)


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
