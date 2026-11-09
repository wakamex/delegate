"""Configuration in two files, split by who knows each fact.

The host roster (host.toml) declares the actors on this host and what limits them: each executor's model, effort,
harness, pool and price, the lead's, pool caps nothing measures, shared machines, and the projects whose ledgers
the model learns from. A project's delegate.toml declares only its own work: kinds, resources, how to launch, and
its lead's session. Nothing in a project names a model, a provider, an account or a pool.
"""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "delegate.toml"
CHECKS = ("gated", "reviewed", "deferred")
MEASURES = ("pass-fail", "yield", "lead-score")
LEAD = "lead"
# Efforts in order: an effort's skill starts from the one before it.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(Exception):
    """A config that is missing, unreadable or does not say what the workflow needs."""


@dataclass
class Resource:
    name: str
    instances: list[str] | None = None  # named instances, one task each
    capacity: float = 1.0  # slots, or units when amounts is set
    amounts: bool = False  # reserved as amounts of a capacity, such as GB
    shared: bool = False  # a host resource, counted across every project


@dataclass
class Actor:
    name: str
    agent: str
    model: str
    effort: str | None
    pool: str | None
    price: float | None  # the typical cost of one run, the prior for its cost
    family: str
    capabilities: list[str] = field(default_factory=list)
    runs: int | None = None  # for the lead: tasks it takes at once

    @property
    def effort_rank(self) -> int | None:
        return EFFORTS.index(self.effort) if self.effort in EFFORTS else None


@dataclass
class Kind:
    name: str
    check: str
    measure: str
    classes: list[str]
    capabilities: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    investigation: bool = False


@dataclass
class Launch:
    """How delegate starts an executor's aop run: the [launch] table."""

    workdir: str | None = None  # the run's --workdir, formatted with the task's reserved resources, such as {checkout}
    directory: str = "."  # where aop runs, relative to the config: its .aop state and prompt files live there
    profile: str = "host"
    timeout_minutes: float = 210
    memory: str = "12G"


@dataclass
class Host:
    path: Path | None
    actors: dict[str, Actor]
    lead: Actor | None
    pool_runs: dict[str, int]
    resources: dict[str, Resource]
    projects: list[Path]


@dataclass
class Config:
    path: Path
    project: str
    ledger: Path
    lease_minutes: float
    lead_session: str | None
    resources: dict[str, Resource]
    kinds: dict[str, Kind]
    launch: Launch = field(default_factory=Launch)
    host: Host = field(default_factory=lambda: Host(None, {}, None, {}, {}, []))

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def executors(self) -> dict[str, Actor]:
        return self.host.actors

    def resource(self, name: str) -> Resource | None:
        return self.resources.get(name) or self.host.resources.get(name)


def find_config(start: Path) -> Path:
    """The nearest delegate.toml at or above START."""
    for directory in [start, *start.parents]:
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(f"no {CONFIG_NAME} at or above {start}")


def host_path() -> Path:
    """The host roster: DELEGATE_HOST, else $XDG_CONFIG_HOME/delegate/host.toml."""
    if given := os.environ.get("DELEGATE_HOST"):
        return Path(given)
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "delegate" / "host.toml"


def family_of(model: str) -> str:
    """A model's family: its name without provider, version numbers and effort, such as claude-haiku for
    claude-haiku-5-5, gpt-terra for gpt-6.1-terra and deepseek-flash for opencode-go/deepseek-v4.1-flash."""
    name = model.split("/")[-1]
    tokens = [t for t in re.split(r"-", name) if t]
    kept = [t for t in tokens if not re.fullmatch(r"v?\d+(\.\d+)*[a-z]?", t) and t not in EFFORTS]
    return "-".join(kept) or name


def _choice(value, allowed, where):
    if value not in allowed:
        raise ConfigError(f"{where} must be one of {', '.join(allowed)}, not {value!r}")
    return value


def _strings(value, where) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where} must be a list of strings")
    return value


def _number(value, where, minimum=0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < minimum:
        raise ConfigError(f"{where} must be a number of at least {minimum:g}")
    return float(value)


def _whole(value, where) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where} must be a whole number of at least 1")
    return value


# Keys a project config may still set from an earlier version, with where each went; loading one names it.
REMOVED = {
    "executors": "executors are declared once for the host, in the host roster",
    "pools": "pool limits are declared once for the host, in the host roster",
    "candidates": "candidate lists were removed; delegate candidates lists aop models the host roster lacks",
    "pool": "the lead's pool is declared in the host roster's [lead] table",
    "state": "a kind is never switched off: the lead competes as an actor and wins where it is cheapest",
    "class_state": "a class is never reserved for the lead: the lead competes as an actor and wins where cheapest",
    "need": "a pool's run size is measured, not configured",
    "dollars_per_point": "the pool check prices each window from clanker-analytics' measurements",
    "need_short": "the pool check prices each window from clanker-analytics' measurements",
    "grain": "nothing read it",
    "wait_deadline_minutes": "the lead competes in every draw, so a task waits only for capacity",
    "references": "nothing read it; keep a kind's lead-score reference tasks in the project's notes",
}


def _known(table, keys, where):
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    for key in table:
        if key in REMOVED and key not in keys:
            raise ConfigError(f"{where}.{key} is no longer used: {REMOVED[key]}; remove it")
    if unknown := set(table) - keys:
        raise ConfigError(f"unknown keys in {where}: {', '.join(sorted(unknown))}")


def _resource(name, value, where, shared=False) -> Resource:
    where = f"{where}.{name}"
    if isinstance(value, list):
        instances = _strings(value, where)
        if not instances or len(set(instances)) != len(instances):
            raise ConfigError(f"{where} must list distinct instance names")
        return Resource(name, instances=instances, capacity=len(instances), shared=shared)
    if isinstance(value, dict):
        if set(value) != {"capacity"}:
            raise ConfigError(f"{where} as a table takes only capacity")
        return Resource(
            name, capacity=_number(value["capacity"], f"{where}.capacity", 1e-9), amounts=True, shared=shared
        )
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return Resource(name, capacity=float(value), shared=shared)
    raise ConfigError(f"{where} must be a list of instances, a slot count, or {{ capacity = N }}")


ACTOR_KEYS = {"agent", "model", "effort", "pool", "price", "family", "capabilities", "runs"}


def _actor(name, value, where) -> Actor:
    _known(value, ACTOR_KEYS, where)
    if not isinstance(value.get("agent"), str) or not isinstance(value.get("model"), str):
        raise ConfigError(f"{where} needs agent and model")
    effort = value.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise ConfigError(f"{where}.effort must be text")
    return Actor(
        name,
        value["agent"],
        value["model"],
        effort,
        value.get("pool"),
        _number(value["price"], f"{where}.price", 1e-9) if "price" in value else None,
        value.get("family") or family_of(value["model"]),
        _strings(value.get("capabilities", []), f"{where}.capabilities"),
        _whole(value["runs"], f"{where}.runs") if "runs" in value else None,
    )


def load_host(path: Path | None = None) -> Host:
    """The host roster, or an empty one when the file does not exist."""
    path = path or host_path()
    if not path.is_file():
        return Host(path, {}, None, {}, {}, [])
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    _known(data, {"projects", "lead", "actors", "pools", "resources"}, path.name)
    actors = {}
    for name, value in data.get("actors", {}).items():
        if name == LEAD:
            raise ConfigError("an actor may not be named lead; the lead is the [lead] table")
        actors[name] = _actor(name, value, f"actors.{name}")
    lead = _actor(LEAD, data["lead"], "lead") if "lead" in data else None
    pool_runs = {}
    for name, value in data.get("pools", {}).items():
        _known(value, {"runs"}, f"pools.{name}")
        if "runs" in value:
            pool_runs[name] = _whole(value["runs"], f"pools.{name}.runs")
    resources = {n: _resource(n, v, "resources", shared=True) for n, v in data.get("resources", {}).items()}
    projects = [(path.parent / p).resolve() for p in _strings(data.get("projects", []), "projects")]
    return Host(path, actors, lead, pool_runs, resources, projects)


KIND_KEYS = {"check", "measure", "classes", "capabilities", "resources", "investigation"}


def load(path: Path, host: Host | None = None) -> Config:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    _known(
        data,
        {"project", "ledger", "lease_minutes", "lead", "resources", "kinds", "launch"},
        path.name,
    )
    for key in ("project", "ledger"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ConfigError(f"{key} is required")
    host = host if host is not None else load_host()
    lead = data.get("lead", {})
    _known(lead, {"session"}, "lead")
    resources = {name: _resource(name, value, "resources") for name, value in data.get("resources", {}).items()}
    for name in resources:
        if name in host.resources:
            raise ConfigError(
                f"resources.{name} is also a host resource; declare a shared machine once, in the host roster"
            )
    kinds = {}
    for name, value in data.get("kinds", {}).items():
        where = f"kinds.{name}"
        _known(value, KIND_KEYS, where)
        for key in ("check", "measure"):
            if key not in value:
                raise ConfigError(f"{where}.{key} is required")
        classes = _strings(value.get("classes", ["any"]), f"{where}.classes") or ["any"]
        reserved = _strings(value.get("resources", []), f"{where}.resources")
        for spec in reserved:
            name_ = parse_resource_spec(spec)[0]
            if name_ not in resources and name_ not in host.resources:
                raise ConfigError(f"{where}.resources names unknown resource {spec!r}")
        kinds[name] = Kind(
            name,
            _choice(value["check"], CHECKS, f"{where}.check"),
            _choice(value["measure"], MEASURES, f"{where}.measure"),
            classes,
            _strings(value.get("capabilities", []), f"{where}.capabilities"),
            reserved,
            bool(value.get("investigation", False)),
        )
    launch = data.get("launch", {})
    _known(launch, {"workdir", "directory", "profile", "timeout_minutes", "memory"}, "launch")
    for key in ("workdir", "directory", "profile", "memory"):
        if key in launch and (not isinstance(launch[key], str) or not launch[key]):
            raise ConfigError(f"launch.{key} must be text")
    return Config(
        path=path,
        project=data["project"],
        ledger=(path.parent / data["ledger"]).resolve(),
        lease_minutes=_number(data.get("lease_minutes", 30), "lease_minutes", 1e-9),
        lead_session=lead.get("session"),
        resources=resources,
        kinds=kinds,
        launch=Launch(
            workdir=launch.get("workdir"),
            directory=launch.get("directory", "."),
            profile=launch.get("profile", "host"),
            timeout_minutes=_number(launch.get("timeout_minutes", 210), "launch.timeout_minutes", 1),
            memory=launch.get("memory", "12G"),
        ),
        host=host,
    )


def parse_resource_spec(spec: str) -> tuple[str, float | None]:
    """NAME or NAME=AMOUNT."""
    name, equals, amount = spec.partition("=")
    if not equals:
        return name, None
    try:
        value = float(amount)
    except ValueError as exc:
        raise ConfigError(f"bad resource amount in {spec!r}") from exc
    if value <= 0:
        raise ConfigError(f"resource amount in {spec!r} must be positive")
    return name, value
