"""Epochs: detecting that an actor changed.

A Bayesian changepoint detector over an actor's recent results, each judged against what was predicted for it when
its task opened. Hypotheses: no change since the actor's last epoch, or a change before one of its recent results,
after which its results shifted by some amount from a grid, worse or better. When a change is more than THRESHOLD
likely and at least three results follow it, the actor starts a new epoch: a new level in the term table that
inherits its old estimate, so the draw re-learns it. Two signals are watched separately: whether results stood,
against the predicted chance (a shift in log-odds), and actor cost at close, against the predicted cost (a shift in
log cost).
"""

import math

WINDOW = 40
HAZARD = 1.0 / 2000.0
THRESHOLD = 0.95
WATCH_AFTER = 20  # observations an actor needs before it is watched
SUCCESS_SHIFTS = (-4.0, -3.0, -2.0, -1.5, -1.0, -0.5, 1.0, 2.0)
COST_SHIFTS = (-0.7, 0.5, 0.7, 1.1, 1.6, 2.3)
COST_SD = 0.6  # spread of a run's log cost around its prediction


def _log_lik(signal: str, predicted: float, observed: float, shift: float) -> float:
    if signal == "success":
        x = predicted + shift if observed > 0.5 else -(predicted + shift)
        return -math.log1p(math.exp(-x)) if x > -30 else x
    r = observed - predicted - shift
    return -0.5 * (r / COST_SD) ** 2


def changed(signal: str, observations: list[tuple[float, float]]) -> bool:
    """Whether OBSERVATIONS, (predicted, observed) pairs in order since the actor's last epoch, show a change.
    For success, predicted is a log-odds and observed is 1 or 0; for cost, both are log costs."""
    obs = observations[-WINDOW:]
    n = len(obs)
    if n < 4:
        return False
    shifts = SUCCESS_SHIFTS if signal == "success" else COST_SHIFTS
    prefix = [0.0]
    for p, o in obs:
        prefix.append(prefix[-1] + _log_lik(signal, p, o, 0.0))
    suffix = [[0.0] * (n + 1) for _ in shifts]
    for d, shift in enumerate(shifts):
        for j in range(n - 1, -1, -1):
            p, o = obs[j]
            suffix[d][j] = suffix[d][j + 1] + _log_lik(signal, p, o, shift)
    none = prefix[n]
    changes = []
    for j in range(0, n - 2):
        terms = [suffix[d][j] for d in range(len(shifts))]
        top = max(terms)
        mix = top + math.log(sum(math.exp(t - top) for t in terms) / len(shifts))
        changes.append(math.log(HAZARD) + prefix[j] + mix)
    top = max([none, *changes])
    p_none = math.exp(none - top)
    p_change = sum(math.exp(c - top) for c in changes)
    return p_change / (p_change + p_none) > THRESHOLD
