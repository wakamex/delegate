"""A project's delegate.toml: what it declares about its kinds, executors and resources."""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "delegate.toml"
CHECKS = ("gated", "reviewed", "deferred")
STATES = ("on", "off")
CLASS_STATES = ("on", "off", "lead-only")
MEASURES = ("pass-fail", "yield", "lead-score")
GRAINS = ("task", "slice")


class ConfigError(Exception):
    """A config that is missing, unreadable or does not say what the workflow needs."""


@dataclass
class Resource:
    name: str
    instances: list[str] | None = None  # named instances, one task each
    capacity: float = 1.0  # slots, or units when amounts is set
    amounts: bool = False  # reserved as amounts of a capacity, such as GB


@dataclass
class Executor:
    name: str
    agent: str
    model: str
    effort: str | None
    pool: str | None
    capabilities: list[str] = field(default_factory=list)


@dataclass
class Kind:
    name: str
    check: str
    state: str
    measure: str
    classes: list[str]
    class_state: dict[str, str] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    investigation: bool = False
    references: dict[str, float] = field(default_factory=dict)

    def state_of(self, cls: str) -> str:
        """The class's delegation state: its override, else the kind's."""
        return self.class_state.get(cls, self.state)


@dataclass
class Config:
    path: Path
    project: str
    ledger: Path
    lease_minutes: float
    wait_deadline_minutes: float
    lead_pool: str | None
    lead_session: str | None
    grain: str
    resources: dict[str, Resource]
    executors: dict[str, Executor]
    kinds: dict[str, Kind]
    dollars_per_point: dict[str, float]
    pool_need: dict[str, float] = field(default_factory=dict)
    candidate_families: list[str] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.path.parent


def find_config(start: Path) -> Path:
    """The nearest delegate.toml at or above START."""
    for directory in [start, *start.parents]:
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(f"no {CONFIG_NAME} at or above {start}")


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


KIND_KEYS = {
    "check",
    "state",
    "measure",
    "classes",
    "class_state",
    "capabilities",
    "resources",
    "investigation",
    "references",
}
EXECUTOR_KEYS = {"agent", "model", "effort", "pool", "capabilities"}
LEAD_KEYS = {"pool", "session", "grain"}


def _known(table, keys, where):
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    if unknown := set(table) - keys:
        raise ConfigError(f"unknown keys in {where}: {', '.join(sorted(unknown))}")


def _resource(name, value) -> Resource:
    where = f"resources.{name}"
    if isinstance(value, list):
        instances = _strings(value, where)
        if not instances or len(set(instances)) != len(instances):
            raise ConfigError(f"{where} must list distinct instance names")
        return Resource(name, instances=instances, capacity=len(instances))
    if isinstance(value, dict):
        if set(value) != {"capacity"}:
            raise ConfigError(f"{where} as a table takes only capacity")
        return Resource(name, capacity=_number(value["capacity"], f"{where}.capacity", 1e-9), amounts=True)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return Resource(name, capacity=float(value))
    raise ConfigError(f"{where} must be a list of instances, a slot count, or {{ capacity = N }}")


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    known = {
        "project",
        "ledger",
        "lease_minutes",
        "wait_deadline_minutes",
        "lead",
        "resources",
        "executors",
        "kinds",
        "pools",
        "candidates",
    }
    if unknown := set(data) - known:
        raise ConfigError(f"unknown keys in {path.name}: {', '.join(sorted(unknown))}")
    for key in ("project", "ledger"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ConfigError(f"{key} is required")
    lead = data.get("lead", {})
    _known(lead, LEAD_KEYS, "lead")
    resources = {name: _resource(name, value) for name, value in data.get("resources", {}).items()}
    executors = {}
    for name, value in data.get("executors", {}).items():
        if name == "lead":
            raise ConfigError("an executor may not be named lead")
        _known(value, EXECUTOR_KEYS, f"executors.{name}")
        if not isinstance(value.get("agent"), str) or not isinstance(value.get("model"), str):
            raise ConfigError(f"executors.{name} needs agent and model")
        executors[name] = Executor(
            name,
            value["agent"],
            value["model"],
            value.get("effort"),
            value.get("pool"),
            _strings(value.get("capabilities", []), f"executors.{name}.capabilities"),
        )
    kinds = {}
    for name, value in data.get("kinds", {}).items():
        where = f"kinds.{name}"
        _known(value, KIND_KEYS, where)
        for key in ("check", "state", "measure", "classes"):
            if key not in value:
                raise ConfigError(f"{where}.{key} is required")
        check = _choice(value["check"], CHECKS, f"{where}.check")
        classes = _strings(value["classes"], f"{where}.classes")
        if not classes:
            raise ConfigError(f"{where}.classes must name at least one class")
        class_state = value.get("class_state", {})
        for cls, state in class_state.items():
            if cls not in classes:
                raise ConfigError(f"{where}.class_state names unknown class {cls!r}")
            _choice(state, CLASS_STATES, f"{where}.class_state.{cls}")
        reserved = _strings(value.get("resources", []), f"{where}.resources")
        for spec in reserved:
            if parse_resource_spec(spec)[0] not in resources:
                raise ConfigError(f"{where}.resources names unknown resource {spec!r}")
        kinds[name] = Kind(
            name,
            check,
            _choice(value["state"], STATES, f"{where}.state"),
            _choice(value["measure"], MEASURES, f"{where}.measure"),
            classes,
            class_state,
            _strings(value.get("capabilities", []), f"{where}.capabilities"),
            reserved,
            bool(value.get("investigation", False)),
            {task: _number(score, f"{where}.references.{task}") for task, score in value.get("references", {}).items()},
        )
    candidates = data.get("candidates", {})
    _known(candidates, {"families"}, "candidates")
    families = _strings(candidates.get("families", []), "candidates.families")
    for pattern in families:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"candidates.families has a bad pattern {pattern!r}: {exc}") from exc
    pools, pool_need = {}, {}
    for name, value in data.get("pools", {}).items():
        _known(value, {"dollars_per_point", "need"}, f"pools.{name}")
        if ("dollars_per_point" in value) == ("need" in value):
            raise ConfigError(f"pools.{name} needs one of dollars_per_point or need")
        if "need" in value:
            pool_need[name] = _number(value["need"], f"pools.{name}.need")
        else:
            pools[name] = _number(value["dollars_per_point"], f"pools.{name}.dollars_per_point", 1e-12)
    return Config(
        path=path,
        project=data["project"],
        ledger=(path.parent / data["ledger"]).resolve(),
        lease_minutes=_number(data.get("lease_minutes", 30), "lease_minutes", 1e-9),
        wait_deadline_minutes=_number(data.get("wait_deadline_minutes", 240), "wait_deadline_minutes"),
        lead_pool=lead.get("pool"),
        lead_session=lead.get("session"),
        grain=_choice(lead.get("grain", "task"), GRAINS, "lead.grain"),
        resources=resources,
        executors=executors,
        kinds=kinds,
        dollars_per_point=pools,
        pool_need=pool_need,
        candidate_families=families,
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
