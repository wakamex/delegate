"""One model of every actor on every task: a generalized linear mixed model over one term table, with several heads.

Every effect is generated from TERMS, attribute tuples with a prior spread. A head fits one outcome over the same
table: whether a result stood (q), what it was worth given it stood (v), integrity defects (D), actor cost (A), the
lead's brief (B) and post-run cost (L), and minutes held (T). Each head is fitted by penalized Newton steps with a
Laplace approximation, its spreads updated in closed form, and draws are joint, from the full posterior covariance.
"""

import math
from dataclasses import dataclass, field

import numpy as np

LOGIT, TWEEDIE, COUNT = "logit", "tweedie", "count"
TWEEDIE_POWER = 1.5
NU0 = 4.0  # degrees of freedom of each spread's inverse chi-square prior
SPREAD_SWEEPS = 3
DRAWS = 256  # posterior draws for selection probabilities, besides the one that chooses


@dataclass(frozen=True)
class Term:
    name: str
    attrs: tuple[str, ...]
    spread: float
    executor: bool  # in an executor's cold-start budget
    ordered: bool = False  # the last attribute is a rank; each level starts from the one before


TERMS = (
    Term("family", ("family",), 0.4, True),
    Term("model", ("model",), 0.3, True),
    Term("effort", ("family", "effort"), 0.2, True, ordered=True),
    Term("harness", ("harness",), 0.2, True),
    Term("version", ("harness", "version"), 0.15, True, ordered=True),
    Term("actor", ("actor",), 0.3, True),
    Term("epoch", ("actor", "epoch"), 0.3, True, ordered=True),
    Term("check", ("check",), 0.3, False),
    Term("project", ("project",), 0.3, False),
    Term("kind", ("project", "kind"), 0.4, False),
    Term("class", ("project", "kind", "cls"), 0.3, False),
    Term("actor check", ("actor", "check"), 0.2, True),
    Term("actor project", ("actor", "project"), 0.25, True),
    Term("actor kind", ("actor", "project", "kind"), 0.3, True),
    Term("actor class", ("actor", "project", "kind", "cls"), 0.2, True),
)


@dataclass
class Row:
    """One task's attributes for the term table. Any attribute may be None, and a tuple with one emits nothing."""

    actor: str
    project: str
    kind: str
    cls: str
    check: str
    family: str | None = None
    model: str | None = None
    effort: int | None = None
    harness: str | None = None
    version: int | None = None
    epoch: int = 0
    retry: float = 0.0
    hard: float = 0.0  # assigned by hand because it was hard


def _keys(term: Term, row: Row) -> list[tuple]:
    values = [getattr(row, a) for a in term.attrs]
    if any(v is None for v in values):
        return []
    if term.ordered:
        prefix, rank = tuple(values[:-1]), int(values[-1])
        return [(term.name, *prefix, step) for step in range(1, rank + 1)]
    return [(term.name, *values)]


def _scaled(budget: float) -> dict[str, float]:
    """Each term's prior spread, the executor terms scaled so an executor's total spread in a new cell is BUDGET."""
    executor = math.sqrt(sum(t.spread**2 for t in TERMS if t.executor))
    return {t.name: t.spread * (budget / executor if t.executor else 1.0) for t in TERMS}


@dataclass
class Head:
    """A fitted head: coefficients, the Cholesky factor of the posterior precision, and what the columns mean."""

    family: str
    columns: dict[tuple, int]
    terms_of: list[tuple[str, ...]]  # per random column, its terms: identical columns merge into one
    fixed: list[str]  # fixed covariate names, the first being the intercept
    beta: np.ndarray
    chol: np.ndarray
    tau2: dict[str, float]
    aliased: set = field(default_factory=set)  # keys merged into an earlier key's identical column
    phi: float = 1.0
    n: int = 0

    @property
    def p(self) -> int:
        return len(self.beta)

    def design(self, rows: list[Row], extra: np.ndarray | None = None) -> tuple[np.ndarray, list[list[tuple]]]:
        """The design matrix for ROWS, and per row the keys of groups the fit has not seen."""
        X = np.zeros((len(rows), self.p))
        unseen = []
        k = len(self.fixed)
        for i, row in enumerate(rows):
            X[i, 0] = 1.0
            X[i, 1] = row.retry
            X[i, 2] = row.hard
            if extra is not None and k > 3:
                X[i, 3:k] = extra[i]
            missing = []
            for term in TERMS:
                for key in _keys(term, row):
                    j = self.columns.get(key)
                    if j is not None:
                        X[i, k + j] = 1.0
                    elif key not in self.aliased:
                        missing.append(key)
            unseen.append(missing)
        return X, unseen

    def mean(self, rows: list[Row], extra: np.ndarray | None = None, offset: np.ndarray | None = None) -> np.ndarray:
        X, _ = self.design(rows, extra)
        eta = X @ self.beta
        return eta + (offset if offset is not None else 0.0)

    def draws(
        self,
        rows: list[Row],
        rng: np.random.Generator,
        m: int,
        extra: np.ndarray | None = None,
        offset: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """M joint draws of the linear predictor for ROWS (m x rows), and the drawn coefficients (m x p). A group the
        fit has not seen is drawn from its prior, once per draw, shared by every row that has it."""
        X, unseen = self.design(rows, extra)
        z = rng.standard_normal((self.p, m))
        # beta = beta_hat + L^-T z, where the posterior precision H = L L^T.
        b = self.beta[:, None] + np.linalg.solve(self.chol.T, z)
        eta = (X @ b).T
        prior: dict[tuple, np.ndarray] = {}
        for i, keys in enumerate(unseen):
            for key in keys:
                if key not in prior:
                    prior[key] = rng.standard_normal(m) * math.sqrt(self.tau2.get(key[0], 0.09))
                eta[:, i] += prior[key]
        if offset is not None:
            eta += offset[None, :]
        return eta, b.T


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def fit(
    family: str,
    rows: list[Row],
    y: np.ndarray,
    offset: np.ndarray | None = None,
    extra: np.ndarray | None = None,
    extra_names: tuple[str, ...] = (),
    prior_mean: float = 0.0,
    budget: float = 0.7,
    nonnegative: tuple[str, ...] = (),
) -> Head:
    """Fit one head. FAMILY is logit (y in [0, 1]), tweedie (non-negative costs, log link) or count (log link)."""
    n = len(rows)
    y = np.asarray(y, dtype=float)
    offset = np.zeros(n) if offset is None else np.asarray(offset, dtype=float)
    spreads = _scaled(budget)
    columns: dict[tuple, int] = {}
    members: dict[tuple, set] = {}
    order: list[tuple] = []
    for i, row in enumerate(rows):
        for term in TERMS:
            for key in _keys(term, row):
                members.setdefault(key, set()).add(i)
                if key not in columns:
                    columns[key] = -1
                    order.append(key)
    # Levels covering exactly the same rows, such as an actor that is the only one of its family, are one column on
    # the data: they merge, and the column's prior variance is the sum of theirs.
    seen_sets: dict[frozenset, int] = {}
    aliased = set()
    kept: list[tuple] = []
    terms_of: list[tuple[str, ...]] = []
    for key in order:
        rowset = frozenset(members[key])
        if rowset in seen_sets:
            aliased.add(key)
            j = seen_sets[rowset]
            terms_of[j] = (*terms_of[j], key[0])
            continue
        seen_sets[rowset] = len(kept)
        kept.append(key)
        terms_of.append((key[0],))
    columns = {key: j for j, key in enumerate(kept)}
    fixed = ["intercept", "retry", "hard", *extra_names]
    k = len(fixed)
    p = k + len(kept)
    X = np.zeros((n, p))
    X[:, 0] = 1.0
    X[:, 1] = [r.retry for r in rows]
    X[:, 2] = [r.hard for r in rows]
    if extra is not None and k > 3:
        X[:, 3:k] = extra
    for key, j in columns.items():
        X[list(members[key]), k + j] = 1.0
    tau2 = {t.name: spreads[t.name] ** 2 for t in TERMS}
    tau0 = dict(tau2)
    mean0 = np.zeros(p)
    if family == LOGIT:
        mean0[0] = prior_mean
    elif n:
        positive = y[y > 0]
        level = math.log(positive.mean()) if len(positive) else 0.0
        mean0[0] = level - float(offset.mean())
    beta = mean0.copy()
    phi = 1.0
    fixed_var = np.array([9.0] + [1.0] * (k - 1))
    lower = np.full(p, -np.inf)
    for name in nonnegative:
        lower[fixed.index(name)] = 0.0
    chol = np.eye(p)
    for _ in range(SPREAD_SWEEPS):
        prior_var = np.concatenate([fixed_var, [sum(tau2[t] for t in terms) for terms in terms_of]])
        beta, H = _newton(family, X, y, offset, mean0, prior_var, beta, phi, lower)
        chol = np.linalg.cholesky(H)
        if family != LOGIT and n > k:
            mu = np.exp(np.clip(X @ beta + offset, -30, 30))
            power = TWEEDIE_POWER if family == TWEEDIE else 1.0
            phi = float(np.sum((y - mu) ** 2 / np.maximum(mu, 1e-9) ** power) / max(1, n - k))
            phi = min(max(phi, 1e-3), 1e3)
        if not terms_of:
            break
        # Each term's share of a merged column: given the column's effect u with prior variance s, a component with
        # prior variance t has E[u_t^2] = (t/s)^2 E[u^2] + t (1 - t/s).
        cov_diag = _inverse_diagonal(chol)
        moments: dict[str, list[float]] = {}
        for j, terms in enumerate(terms_of):
            u2 = float(beta[k + j] ** 2 + cov_diag[k + j])
            total = sum(tau2[t] for t in terms)
            for t in terms:
                share = tau2[t] / total
                moments.setdefault(t, []).append(share**2 * u2 + tau2[t] * (1 - share))
        for term, values in moments.items():
            tau2[term] = (sum(values) + NU0 * tau0[term]) / (len(values) + NU0 + 2)
    return Head(family, columns, terms_of, fixed, beta, chol, tau2, aliased, phi, n)


def _inverse_diagonal(chol: np.ndarray) -> np.ndarray:
    inv = np.linalg.solve(chol, np.eye(len(chol)))
    return np.sum(inv**2, axis=0)


def _newton(family, X, y, offset, mean0, prior_var, beta, phi, lower, steps=60):
    """Maximize the penalized log-likelihood; returns the mode and the posterior precision there."""
    inv_var = 1.0 / prior_var
    H = None
    for _ in range(steps):
        eta = X @ beta + offset
        if family == LOGIT:
            mu = _sigmoid(eta)
            resid, w = y - mu, mu * (1 - mu)
        elif family == TWEEDIE:
            mu = np.exp(np.clip(eta, -30, 30))
            resid, w = (y - mu) * mu ** (1 - TWEEDIE_POWER) / phi, mu ** (2 - TWEEDIE_POWER) / phi
        else:
            mu = np.exp(np.clip(eta, -30, 30))
            resid, w = (y - mu) / phi, mu / phi
        grad = X.T @ resid - (beta - mean0) * inv_var
        H = X.T @ (X * w[:, None]) + np.diag(inv_var)
        step = np.linalg.solve(H, grad)
        biggest = np.max(np.abs(step)) if len(step) else 0.0
        if biggest > 3.0:
            step *= 3.0 / biggest
        beta = np.maximum(beta + step, lower)
        if biggest < 1e-7:
            break
    if H is None:
        H = np.diag(inv_var)
    return beta, H
