"""Choosing an actor for a task, from one model of every actor on every host project's tasks.

The choice minimizes expected cost per standing result, per unit of value, at one joint draw from the model's
posterior (Thompson sampling), among the actors that capacity and a person's suspensions allow. Nothing else gates
an actor: an unreliable one, or one with integrity defects, costs more per standing result and loses the draw.
"""

import hashlib
import math
import pickle
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from . import __version__, epochs
from .config import EFFORTS, LEAD, Actor, Config, ConfigError, Kind, family_of, load, parse_resource_spec
from .ledger import State, Task, parse_time
from .model import COUNT, DRAWS, LOGIT, TWEEDIE, Head, Row, fit

SCARCITY_FLOOR = 0.05  # quota due to expire unused still costs something, so relative prices hold
DEFAULT_PRICE = 3.0  # the cost prior for an actor with no price in the roster
FAILURE_DAMAGE = 2.0  # an integrity defect costs this many corrections: rework and re-check
CAUSES = ("difficulty", "quota", "capability", "user")


class SelectError(Exception):
    pass


@dataclass
class Env:
    """What select needs from outside the ledgers, injectable for tests."""

    pool: callable  # (target, dollars) -> {"room": bool, "marginal": share, "state": ..., "reasons": [...]}
    rng: random.Random
    cache: Path | None = None


@dataclass
class World:
    """Every host project's tasks, and the host's actors."""

    config: Config
    projects: dict[str, tuple[Config, State]]
    events: list[tuple[str, dict]] = field(default_factory=list)  # (project, event) for non-task events

    def tasks(self):
        for project, (config, state) in self.projects.items():
            for task in state.tasks.values():
                yield project, config, task


def load_world(config: Config, state: State, read) -> World:
    """The current project's state (already read, under its lock) and every other host project's, via READ."""
    projects = {config.project: (config, state)}
    for path in config.host.projects:
        if path == config.path.resolve():
            continue
        try:
            other = load(path, config.host)
        except ConfigError:
            continue
        if other.project in projects:
            continue
        projects[other.project] = (other, read(other.ledger))
    world = World(config, projects)
    for project, (_, st) in projects.items():
        for event in st.events:
            if event.get("event") in ("epoch", "suspend", "resume", "drop"):
                world.events.append((project, event))
    return world


# ---- the rows the model learns from ---------------------------------------------------------------


def _actor(config: Config, name: str) -> Actor | None:
    if name == LEAD:
        return config.host.lead
    return config.host.actors.get(name)


def key_of(identity: dict | None, name: str) -> str:
    """An actor's key in the model: what it is, so one model under two names is one actor and one name reused for
    another model is two. A name nobody described is its own key."""
    if not identity or not identity.get("agent") or not identity.get("model"):
        return f"name:{name}"
    effort = identity.get("effort")
    role = identity.get("role")
    return f"{identity['agent']}/{identity['model']}" + (f"/{effort}" if effort else "") + (f"@{role}" if role else "")


def identity(world: World, project: str, name: str, recorded: dict | None = None) -> dict | None:
    """Who NAME meant in PROJECT: as recorded on the event, else as the project's actors event described it for
    events before opens recorded it, else the roster's actor of that name."""
    if recorded:
        return recorded
    described = world.projects[project][1].actors.get(name) if project in world.projects else None
    if described:
        return described
    actor = _actor(world.config, name)
    return actor.identity if actor else None


def task_key(world: World, project: str, task: Task) -> str:
    return key_of(identity(world, project, task.actor, task.open.get("identity")), task.actor)


def event_key(world: World, project: str, event: dict) -> str:
    return key_of(identity(world, project, event.get("actor", ""), event.get("identity")), event.get("actor", ""))


def actor_key(config: Config, name: str) -> str:
    actor = _actor(config, name)
    return key_of(actor.identity if actor else None, name)


def _roster(config: Config) -> dict[str, Actor]:
    """The roster's actors, the lead included, by key."""
    actors = [*config.host.actors.values(), *([config.host.lead] if config.host.lead else [])]
    return {key_of(a.identity, a.name): a for a in actors}


def _epochs(world: World, key: str, signal: str, before: datetime) -> int:
    return sum(
        1
        for project, e in world.events
        if e.get("event") == "epoch"
        and e.get("signal") == signal
        and parse_time(e["at"]) < before
        and event_key(world, project, e) == key
    )


def _versions(world: World) -> dict[tuple[str, str], int]:
    """Each harness version's rank, by first appearance."""
    first: dict[str, list[str]] = {}
    for project, _, task in sorted(world.tasks(), key=lambda x: x[2].opened):
        version = (task.started or {}).get("harness_version")
        ident = identity(world, project, task.actor, task.open.get("identity"))
        if version and ident:
            seen = first.setdefault(ident["agent"], [])
            if version not in seen:
                seen.append(version)
    return {(h, v): i for h, vs in first.items() for i, v in enumerate(vs)}


def row(
    world: World,
    project: str,
    kind: Kind,
    cls: str,
    key: str,
    ident: dict | None,
    at: datetime,
    signal: str,
    task: Task | None = None,
    versions=None,
) -> Row:
    """The task's attributes for the model, for the actor with KEY and IDENTITY."""
    ident = ident or {}
    roster = _roster(world.config).get(key)
    model = ident.get("model")
    version = None
    if task and versions is not None and ident.get("agent"):
        v = (task.started or {}).get("harness_version")
        version = versions.get((ident["agent"], v)) if v else None
    retry = 1.0 if task is not None and task.open.get("retry_of") else 0.0
    hard = 1.0 if task is not None and task.open.get("cause") == "difficulty" else 0.0
    effort = ident.get("effort")
    return Row(
        actor=key,
        project=project,
        kind=kind.name,
        cls=cls,
        check=kind.check,
        family=roster.family if roster else (family_of(model) if model else None),
        model=model,
        effort=EFFORTS.index(effort) if effort in EFFORTS else None,
        harness=ident.get("agent"),
        version=version,
        epoch=_epochs(world, key, signal, at),
        retry=retry,
        hard=hard,
    )


def _price(config: Config, key: str) -> float:
    a = _roster(config).get(key)
    if a and a.price:
        return a.price
    prices = [x.price for x in config.host.actors.values() if x.price]
    return float(np.median(prices)) if prices else DEFAULT_PRICE


@dataclass
class Heads:
    q: Head | None = None
    integrity: Head | None = None
    score: Head | None = None
    yield_: Head | None = None
    cost: Head | None = None
    brief: Head | None = None
    post: Head | None = None
    minutes: Head | None = None
    id: str = ""


def fit_id(world: World) -> str:
    """Changes only when an outcome, cost or epoch arrives, so opens reuse the fit."""
    digest = hashlib.sha256(__version__.encode())
    if world.config.host.path and world.config.host.path.is_file():
        digest.update(world.config.host.path.read_bytes())
    for project in sorted(world.projects):
        config, state = world.projects[project]
        digest.update(config.path.read_bytes())
        for event in state.events:
            if event.get("event") in ("close", "verdict", "amend", "overturn", "epoch", "started", "actors"):
                digest.update(repr(sorted(event.items())).encode())
    return digest.hexdigest()[:16]


def fit_heads(world: World, cache: Path | None = None) -> Heads:
    ident = fit_id(world)
    path = cache / f"fit-{ident}.pkl" if cache else None
    if path and path.is_file():
        try:
            return pickle.loads(path.read_bytes())
        except Exception:  # noqa: BLE001 - a bad cache is refitted
            pass
    versions = _versions(world)
    data: dict[str, list] = {h: [] for h in ("q", "integrity", "score", "yield", "cost", "brief", "post", "minutes")}
    for project, config, task in world.tasks():
        kind = config.kinds.get(task.kind)
        if kind is None:
            continue
        cls = task.cls if task.cls in kind.classes else kind.classes[0]
        key = task_key(world, project, task)
        ident = identity(world, project, task.actor, task.open.get("identity"))
        r_success = row(world, project, kind, cls, key, ident, task.opened, "success", task, versions)
        r_cost = row(world, project, kind, cls, key, ident, task.opened, "cost", task, versions)
        stood = task.stood
        if stood is not None:
            data["q"].append((r_success, float(stood)))
            if task.actor != LEAD:
                data["integrity"].append((r_success, float(task.integrity)))
            if stood and kind.measure == "lead-score" and task.score is not None:
                data["score"].append((r_success, min(max(task.score / 100.0, 0.0), 1.0)))
            if stood and kind.measure == "yield" and task.yield_ is not None:
                data["yield"].append((r_success, max(task.yield_, 0.0)))
        if task.close and task.outcome != "aborted":
            actor_cost = task.cost("actor")
            if actor_cost is not None:
                data["cost"].append((r_cost, actor_cost, math.log(_price(world.config, key))))
            if task.actor != LEAD:
                brief = task.cost("brief")
                lead = task.cost("lead")
                if brief is not None:
                    data["brief"].append((r_cost, brief))
                if lead is not None and stood is not None:
                    data["post"].append((r_cost, max(lead - (brief or 0.0), 0.0), 0.0 if stood else 1.0))
            minutes = (task.closed_at - task.opened).total_seconds() / 60
            data["minutes"].append((r_cost, max(minutes, 0.0)))
    heads = Heads(id=ident)
    if len(data["q"]) >= 3:
        heads.q = fit(LOGIT, [r for r, _ in data["q"]], np.array([y for _, y in data["q"]]), prior_mean=2.2)
    if len(data["integrity"]) >= 3:
        rows_ = [r for r, _ in data["integrity"]]
        heads.integrity = fit(LOGIT, rows_, np.array([y for _, y in data["integrity"]]), prior_mean=-4.0)
    if len(data["score"]) >= 3:
        heads.score = fit(LOGIT, [r for r, _ in data["score"]], np.array([y for _, y in data["score"]]), prior_mean=1.0)
    if len(data["yield"]) >= 3:
        heads.yield_ = fit(COUNT, [r for r, _ in data["yield"]], np.array([y for _, y in data["yield"]]), budget=1.0)
    if len(data["cost"]) >= 3:
        heads.cost = fit(
            TWEEDIE,
            [r for r, _, _ in data["cost"]],
            np.array([y for _, y, _ in data["cost"]]),
            offset=np.array([o for _, _, o in data["cost"]]),
            budget=1.0,
        )
    if len(data["brief"]) >= 3:
        heads.brief = fit(TWEEDIE, [r for r, _ in data["brief"]], np.array([y for _, y in data["brief"]]), budget=1.0)
    if len(data["post"]) >= 3:
        heads.post = fit(
            TWEEDIE,
            [r for r, _, _ in data["post"]],
            np.array([y for _, y, _ in data["post"]]),
            extra=np.array([[f] for _, _, f in data["post"]]),
            extra_names=("failed",),
            budget=1.0,
            nonnegative=("failed",),
        )
    if len(data["minutes"]) >= 3:
        heads.minutes = fit(
            TWEEDIE, [r for r, _ in data["minutes"]], np.array([y for _, y in data["minutes"]]), budget=1.0
        )
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pickle.dumps(heads))
        except OSError:
            pass
    return heads


# ---- predictions --------------------------------------------------------------------------------


@dataclass
class Forecast:
    """Joint draws for each candidate (draws x candidates): the quantities the unit cost is made of."""

    actors: list[str]
    q: np.ndarray
    value: np.ndarray
    cost: np.ndarray
    brief: np.ndarray
    post: np.ndarray
    increment: np.ndarray
    integrity: np.ndarray
    cost_sd: np.ndarray  # spread of one run's log cost, for the capacity gate's upper quantile

    def unit(self, scarcity: dict[str, float], lead_scarcity: float) -> np.ndarray:
        """Expected cost per standing result per unit of value, for every draw and candidate."""
        out = np.empty_like(self.q)
        for j, actor in enumerate(self.actors):
            q = np.clip(self.q[:, j], 1e-3, 1.0)
            v = np.maximum(self.value[:, j], 1e-3)
            if actor == LEAD:
                spend = lead_scarcity * (self.cost[:, j] + (1 - q) * self.increment[:, j])
            else:
                spend = (
                    scarcity.get(actor, 1.0) * self.cost[:, j]
                    + lead_scarcity * (self.brief[:, j] + self.post[:, j] + (1 - q) * self.increment[:, j])
                    + self.integrity[:, j] * FAILURE_DAMAGE * self.increment[:, j]
                )
            out[:, j] = spend / (q * v)
        return out


def _sig(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def forecast(
    world: World,
    heads: Heads,
    kind: Kind,
    cls: str,
    actors: list[str],
    at: datetime,
    retry: bool,
    rng: np.random.Generator,
    m: int = DRAWS + 1,
) -> Forecast:
    project, config = world.config.project, world.config
    keys = [actor_key(config, a) for a in actors]
    idents = [a.identity if (a := _actor(config, name)) else None for name in actors]
    succ = [row(world, project, kind, cls, k, i, at, "success") for k, i in zip(keys, idents, strict=True)]
    cost_rows = [row(world, project, kind, cls, k, i, at, "cost") for k, i in zip(keys, idents, strict=True)]
    for r in succ + cost_rows:
        r.retry = 1.0 if retry else 0.0
    c = len(actors)

    def logit_draws(head, rows, default):
        if head is None:
            return _sig(rng.normal(default, 0.7, (m, c)))
        return _sig(head.draws(rows, rng, m)[0])

    q = logit_draws(heads.q, succ, 2.2)
    if kind.measure == "lead-score":
        value = logit_draws(heads.score, succ, 1.0)
    elif kind.measure == "yield":
        value = np.exp(heads.yield_.draws(succ, rng, m)[0]) if heads.yield_ else np.ones((m, c))
    else:
        value = np.ones((m, c))
    integrity = logit_draws(heads.integrity, succ, -4.0)
    offsets = np.array([math.log(_price(config, k)) for k in keys])
    if heads.cost is not None:
        eta, _ = heads.cost.draws(cost_rows, rng, m, offset=offsets)
        cost = np.exp(eta)
        mu = np.exp(heads.cost.mean(cost_rows, offset=offsets))
        cost_sd = np.sqrt(np.log1p(heads.cost.phi * np.maximum(mu, 1e-6) ** (-0.5)) + eta.var(axis=0))
    else:
        cost = np.exp(offsets[None, :] + rng.normal(0, 0.5, (m, c)))
        cost_sd = np.full(c, 0.7)
    brief = np.exp(heads.brief.draws(cost_rows, rng, m)[0]) if heads.brief else np.zeros((m, c))
    if heads.post is not None:
        zeros = np.zeros((c, 1))
        eta, b = heads.post.draws(cost_rows, rng, m, extra=zeros)
        post = np.exp(eta)
        failed_coef = b[:, heads.post.fixed.index("failed")]
        increment = post * (np.exp(failed_coef)[:, None] - 1.0)
    else:
        post = np.zeros((m, c))
        increment = np.zeros((m, c))
    return Forecast(actors, q, value, cost, brief, post, increment, integrity, cost_sd)


# ---- capacity -----------------------------------------------------------------------------------


def holding(world: World) -> list[tuple[str, Task]]:
    """Every open task on the host, with its project."""
    return [(p, t) for p, _, t in world.tasks() if t.holding]


def reserve(world: World, specs: list[str]) -> tuple[dict | None, str | None]:
    """The reservation for SPECS against tasks still holding theirs: the project's own resources against its own
    tasks, shared host resources against every project's."""
    config = world.config
    held: dict[str, list] = {}
    for project, task in holding(world):
        for name, value in (task.open.get("resources") or {}).items():
            resource = config.resource(name)
            if resource is None:
                continue
            if resource.shared or project == config.project:
                held.setdefault(name, []).append(value)
    reservation = {}
    for spec in specs:
        name, amount = parse_resource_spec(spec)
        resource = config.resource(name)
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


def suspended(world: World, actor: str, kind: str) -> bool:
    """Whether a person suspended ACTOR, everywhere or in KIND of this project, and has not resumed it."""
    last_stop, last_go = None, None
    key = actor_key(world.config, actor)
    for project, event in world.events:
        if event.get("event") not in ("suspend", "drop", "resume") or event_key(world, project, event) != key:
            continue
        name = event.get("event")
        scoped = event.get("kind")
        applies = scoped is None or (scoped == kind and project == world.config.project)
        if not applies:
            continue
        at = parse_time(event["at"])
        if name in ("suspend", "drop"):
            last_stop = at if last_stop is None or at > last_stop else last_stop
        elif name == "resume":
            last_go = at if last_go is None or at > last_go else last_go
    return last_stop is not None and (last_go is None or last_go < last_stop)


# ---- the choice -----------------------------------------------------------------------------------


def select(
    world: World,
    env: Env,
    kind_name: str,
    cls: str,
    now: datetime,
    resources: list[str] | None = None,
    token_cap: float | None = None,
    retry: str | None = None,
    since: datetime | None = None,
    assign: str | None = None,
    why: str | None = None,
    cause: str | None = None,
) -> tuple[dict, list[dict]]:
    """The answer, and the open event to append (none for a wait)."""
    config = world.config
    state = world.projects[config.project][1]
    kind = config.kinds.get(kind_name)
    if kind is None:
        raise SelectError(f"unknown kind {kind_name!r}")
    if cls not in kind.classes:
        raise SelectError(f"unknown class {cls!r} for kind {kind_name}; classes are {', '.join(kind.classes)}")
    if retry and retry not in state.tasks:
        raise SelectError(f"unknown task {retry!r} to retry")
    excluded = task_key(world, config.project, state.tasks[retry]) if retry else None
    specs = resources if resources is not None else kind.resources
    reservation, busy = reserve(world, specs)
    answer = {"kind": kind_name, "class": cls}
    if busy:
        return {**answer, "answer": "wait", "waiting_for": busy, "since": (since or now).isoformat()}, []
    heads = fit_heads(world, env.cache)
    seed = env.rng.randrange(2**32)
    rng = np.random.default_rng(seed)
    host = config.host
    if assign:
        if assign != LEAD and assign not in host.actors:
            raise SelectError(f"unknown actor {assign!r}")
        if not why:
            raise SelectError("an assignment needs --why, the user's or lead's reason")
        if cause is not None and cause not in CAUSES:
            raise SelectError(f"--cause is one of {', '.join(CAUSES)}")
        if suspended(world, assign, kind_name):
            raise SelectError(f"{assign} is suspended; resume it before assigning it work")
        if assign != LEAD and not set(kind.capabilities) <= set(host.actors[assign].capabilities):
            raise SelectError(
                f"{assign} lacks capabilities {sorted(set(kind.capabilities) - set(host.actors[assign].capabilities))}"
            )
        fc = forecast(world, heads, kind, cls, [assign], now, bool(retry), rng)
        return _open(
            world,
            kind,
            cls,
            assign,
            "assigned",
            fc,
            None,
            reservation,
            token_cap,
            retry,
            now,
            seed,
            heads,
            why=why,
            cause=cause or "user",
        )
    candidates = [
        name
        for name, actor in host.actors.items()
        if actor_key(config, name) != excluded
        and set(kind.capabilities) <= set(actor.capabilities)
        and not suspended(world, name, kind_name)
    ]
    lead_ok = excluded != actor_key(config, LEAD) and not suspended(world, LEAD, kind_name)
    actors = candidates + ([LEAD] if lead_ok else [])
    if not actors:
        return {
            **answer,
            "answer": "wait",
            "waiting_for": "no actor may take this task",
            "since": (since or now).isoformat(),
        }, []
    fc = forecast(world, heads, kind, cls, actors, now, bool(retry), rng)
    open_tasks = holding(world)
    refused: dict[str, dict] = {}
    rooms: dict[str, dict] = {}
    # Each check is a usage-burn process; run them at once rather than one actor after another.
    with ThreadPoolExecutor(max_workers=8) as pool:
        checked = list(pool.map(lambda j: _room(world, env, actors[j], fc, j, open_tasks), range(len(actors))))
    for name, room in zip(actors, checked, strict=True):
        rooms[name] = room
        if not room["room"]:
            refused[name] = room
    eligible = [a for a in actors if a not in refused]
    if not eligible:
        errors = [r for room in refused.values() if room.get("state") == "error" for r in room.get("reasons", [])]
        waiting = {
            **answer,
            "answer": "wait",
            "waiting_for": "pool room",
            "since": (since or now).isoformat(),
            "refused": refused,
        }
        if errors and len(errors) == sum(len(r.get("reasons", [])) for r in refused.values()):
            waiting = {**waiting, "answer": "error", "error": "; ".join(dict.fromkeys(errors))}
        return waiting, []
    scarcity = {a: max(SCARCITY_FLOOR, rooms[a].get("marginal", 1.0)) for a in actors if a != LEAD}
    lead_scarcity = max(SCARCITY_FLOOR, rooms.get(LEAD, {}).get("marginal", 1.0))
    units = fc.unit(scarcity, lead_scarcity)
    idx = [actors.index(a) for a in eligible]
    sub = units[:, idx]
    chosen = eligible[int(np.argmin(sub[0]))]
    winners = np.argmin(sub, axis=1)
    probs = {a: float(np.mean(winners == i)) for i, a in enumerate(eligible)}
    predictions = {
        a: {"unit": round(float(sub[:, i].mean()), 4), "sd": round(float(sub[:, i].std()), 4), "p": round(probs[a], 4)}
        for i, a in enumerate(eligible)
    }
    account = rooms.get(chosen, {}).get("account")
    return _open(
        world,
        kind,
        cls,
        chosen,
        "draw",
        fc,
        predictions,
        reservation,
        token_cap,
        retry,
        now,
        seed,
        heads,
        account=account,
        refused=refused or None,
        scarcity={a: round(s, 3) for a, s in scarcity.items() if s < 1} or None,
    )


def _room(world: World, env: Env, name: str, fc: Forecast, j: int, open_tasks) -> dict:
    """Whether ACTOR's pool has room for one more run: its cap across the host, then the pool check at the run's
    upper quantile of cost with what is already open there. A pool the check cannot price takes one run at a time."""
    roster = _roster(world.config)
    open_keys = [(p, t, task_key(world, p, t)) for p, t in open_tasks]
    return _pool_room(world, env, name, fc, j, open_keys, roster)


def _pool_room(world, env, name, fc, j, open_keys, roster) -> dict:
    host = world.config.host
    actor = host.lead if name == LEAD else host.actors.get(name)
    pool = actor.pool if actor else None
    mean_cost = float(fc.cost[:, j].mean())
    upper = mean_cost * math.exp(1.28 * float(fc.cost_sd[j]))
    if name == LEAD:
        lead_key = actor_key(world.config, LEAD)
        lead_open = sum(1 for _, _, k in open_keys if k == lead_key)
        most = actor.runs if actor and actor.runs else 1
        if lead_open >= most:
            return {
                "room": False,
                "state": "refused",
                "reasons": [f"the lead takes {most} tasks at once and has {lead_open}"],
            }
    on_pool = [t for _, t, k in open_keys if pool and k in roster and roster[k].pool == pool]
    if pool and (most := host.pool_runs.get(pool)) is not None and len(on_pool) >= most:
        return {
            "room": False,
            "state": "refused",
            "reasons": [f"{pool} takes {most} open tasks at once and has {len(on_pool)}"],
        }
    if not pool:
        return {"room": True, "marginal": 1.0, "state": "unchecked"}
    in_flight = sum(float((t.open.get("expected") or {}).get("cost") or 0) for t in on_pool)
    if host.lead and pool == host.lead.pool and name != LEAD:
        upper += float(fc.brief[:, j].mean() + fc.post[:, j].mean())
    room = env.pool(pool, upper + in_flight)
    if room.get("state") == "unavailable" and not on_pool:
        return {**room, "room": True, "marginal": 1.0, "unmeasured": True}
    return room


def _open(world, kind, cls, actor, reason, fc, predictions, reservation, token_cap, retry, now, seed, heads, **extra):
    state = world.projects[world.config.project][1]
    task_id = f"{kind.name}-{sum(t.kind == kind.name for t in state.tasks.values()) + 1:04d}"
    j = fc.actors.index(actor)
    expected = {
        "q": round(float(fc.q[:, j].mean()), 4),
        "value": round(float(fc.value[:, j].mean()), 4),
        "cost": round(float(fc.cost[:, j].mean()), 4),
        "integrity": round(float(fc.integrity[:, j].mean()), 4),
    }
    if actor != LEAD:
        expected.update(
            brief=round(float(fc.brief[:, j].mean()), 4),
            post=round(float(fc.post[:, j].mean()), 4),
            increment=round(float(fc.increment[:, j].mean()), 4),
        )
    event = {
        "event": "open",
        "at": now.isoformat(),
        "task": task_id,
        "kind": kind.name,
        "class": cls,
        "actor": actor,
        "identity": identity(world, world.config.project, actor),
        "reason": reason,
        "resources": reservation or {},
        "token_cap": token_cap,
        "seed": seed,
        "expected": expected,
        "policy": __version__,
        "fit": heads.id,
    }
    if predictions is not None:
        event["predictions"] = predictions
    if retry:
        event["retry_of"] = retry
    for key in ("why", "cause", "account", "refused", "scarcity"):
        if extra.get(key) is not None:
            event[key] = extra[key]
    answer = {
        "answer": "lead" if actor == LEAD else "delegate",
        "kind": kind.name,
        "class": cls,
        "task": task_id,
        "actor": actor,
        "reason": reason,
        "resources": reservation or {},
        "expected": expected,
    }
    if extra.get("account"):
        answer["account"] = extra["account"]
    executor = world.config.host.actors.get(actor)
    if executor:
        answer["executor"] = {k: v for k, v in vars(executor).items() if k != "name"}
    return answer, [event]


# ---- epochs ---------------------------------------------------------------------------------------


def _judged_at(task: Task) -> datetime | None:
    if task.verdict:
        return parse_time(task.verdict["at"])
    return task.closed_at


def detect_epochs(world: World, actor: str, now: datetime) -> list[dict]:
    """New epoch events for ACTOR, if its results or its costs since its last epoch show a change."""
    if actor == LEAD:
        return []
    out = []
    key = actor_key(world.config, actor)
    mine = [(p, c, t) for p, c, t in world.tasks() if task_key(world, p, t) == key]
    last = {
        s: max(
            (
                parse_time(e["at"])
                for p, e in world.events
                if e.get("event") == "epoch" and e.get("signal") == s and event_key(world, p, e) == key
            ),
            default=None,
        )
        for s in ("success", "cost")
    }
    stamp = {"event": "epoch", "at": now.isoformat(), "actor": actor, "identity": identity(world, "", actor)}
    # Success: each result against its predicted chance, shifted by how its kind is going for everyone lately.
    judged = sorted(
        ((t, p) for p, _, t in mine if t.stood is not None and (t.open.get("expected") or {}).get("q") is not None),
        key=lambda x: _judged_at(x[0]),
    )
    if len(judged) >= epochs.WATCH_AFTER:
        obs = []
        for task, project in judged:
            when = _judged_at(task)
            if last["success"] and when <= last["success"]:
                continue
            q = min(max(float(task.open["expected"]["q"]), 1e-3), 1 - 1e-3)
            obs.append((math.log(q / (1 - q)) + _trend(world, project, task, key), 1.0 if task.stood else 0.0))
        if epochs.changed("success", obs):
            out.append({**stamp, "signal": "success", "observations": len(obs)})
    costed = sorted(
        (t for _, _, t in mine if t.cost("actor") and (t.open.get("expected") or {}).get("cost")),
        key=lambda t: t.closed_at,
    )
    if len(costed) >= epochs.WATCH_AFTER:
        obs = [
            (math.log(float(t.open["expected"]["cost"])), math.log(t.cost("actor")))
            for t in costed
            if not (last["cost"] and t.closed_at <= last["cost"])
        ]
        if epochs.changed("cost", obs):
            out.append({**stamp, "signal": "cost", "observations": len(obs)})
    return out


def _trend(world: World, project: str, task: Task, key: str) -> float:
    """How the task's kind went for other actors in the ten results judged before it, against their predictions."""
    when = _judged_at(task)
    _, state = world.projects[project]
    others = [
        t
        for t in state.tasks.values()
        if t.kind == task.kind
        and task_key(world, project, t) != key
        and t.stood is not None
        and _judged_at(t) < when
        and (t.open.get("expected") or {}).get("q")
    ]
    others = sorted(others, key=_judged_at)[-10:]
    if len(others) < 5:
        return 0.0
    rate = min(max(sum(t.stood for t in others) / len(others), 0.02), 0.98)
    predicted = min(max(sum(float(t.open["expected"]["q"]) for t in others) / len(others), 0.02), 0.98)
    return math.log(rate / (1 - rate)) - math.log(predicted / (1 - predicted))


# ---- the scorecard ------------------------------------------------------------------------------


def scorecard(world: World, env: Env, now: datetime, only: str | None = None) -> list[dict]:
    """Each actor's record and current forecast per kind of this project. Tasks count toward the roster actor they
    were, under any name; an actor no longer in the roster keeps its record under its old name, without a forecast."""
    config = world.config
    project = config.project
    state = world.projects[project][1]
    heads = fit_heads(world, env.cache)
    rng = np.random.default_rng(0)
    roster = [*config.host.actors, LEAD]
    label = {actor_key(config, name): name for name in roster}
    rows = []
    for kind in config.kinds.values():
        if only and kind.name != only:
            continue
        mine: dict[str, list[Task]] = {}
        for task in state.tasks.values():
            if task.kind == kind.name:
                key = task_key(world, project, task)
                label.setdefault(key, task.actor)
                mine.setdefault(key, []).append(task)
        fc = forecast(world, heads, kind, kind.classes[0], roster, now, False, rng, m=200)
        units = fc.unit({}, 1.0)
        for key in dict.fromkeys([*(actor_key(config, n) for n in roster), *mine]):
            name, tasks = label[key], mine.get(key, [])
            j = roster.index(name) if name in roster else None
            final = [t for t in tasks if t.stood is not None]
            spend = sum((t.cost("actor") or 0) + (t.cost("lead") or 0) for t in tasks if t.close)
            stood = sum(1 for t in final if t.stood)
            rows.append(
                {
                    "kind": kind.name,
                    "actor": name,
                    "names": sorted({t.actor for t in tasks} - {name}) or None,
                    "suspended": suspended(world, name, kind.name) if j is not None else None,
                    "tasks": len(tasks),
                    "pending": sum(1 for t in tasks if t.stood is None and t.outcome != "aborted"),
                    "verdicts": len(final),
                    "stood": stood,
                    "integrity": sum(1 for t in final if t.integrity),
                    "rescored": sum("score.value" in t.amended for t in final),
                    "epochs": sum(
                        1 for p, e in world.events if e.get("event") == "epoch" and event_key(world, p, e) == key
                    ),
                    "spend": round(spend, 2),
                    "spend_per_stood": round(spend / stood, 2) if stood else None,
                    "q": None if j is None else round(float(fc.q[:, j].mean()), 3),
                    "cost": None if j is None else round(float(fc.cost[:, j].mean()), 2),
                    "unit": None if j is None else round(float(np.median(units[:, j])), 2),
                }
            )
    return rows
