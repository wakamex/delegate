import unittest

import numpy as np

from delegate import epochs
from delegate.model import COUNT, LOGIT, TERMS, TWEEDIE, Row, _scaled, fit


def row(actor, family, kind="tool", project="p", check="gated"):
    return Row(actor=actor, project=project, kind=kind, cls="any", check=check, family=family, model=actor)


class ModelTests(unittest.TestCase):
    def test_success_heads_rank_actors_and_carry_across_kinds(self):
        rng = np.random.default_rng(0)
        skill = {"good": 2.0, "bad": -1.0, "mid": 0.5}
        rows, y = [], []
        for actor, s in skill.items():
            for kind in ("tool", "port"):
                for _ in range(30):
                    rows.append(row(actor, actor, kind))
                    y.append(float(rng.random() < 1 / (1 + np.exp(-s))))
        head = fit(LOGIT, rows, np.array(y))
        new_kind = head.mean([row(a, a, "recon") for a in skill])
        self.assertEqual(list(np.argsort(-new_kind)), [0, 2, 1])

    def test_cost_heads_recover_relative_costs_with_a_price_offset(self):
        rng = np.random.default_rng(1)
        rows, y, offset = [], [], []
        for actor, cost, price in (("a", 0.5, 1.0), ("b", 4.0, 1.0), ("c", 2.0, 2.0)):
            for _ in range(40):
                rows.append(row(actor, actor))
                y.append(cost * rng.lognormal(0, 0.3))
                offset.append(np.log(price))
        head = fit(TWEEDIE, rows, np.array(y), offset=np.array(offset))
        predicted = np.exp(head.mean([row(a, a) for a in "abc"], offset=np.log([1.0, 1.0, 2.0])))
        np.testing.assert_allclose(predicted, [0.5, 4.0, 2.0], rtol=0.2)
        self.assertEqual(fit(COUNT, rows, np.ones(len(rows))).family, COUNT)

    def test_identical_columns_merge_with_their_prior_variances_summed(self):
        # One actor per family: family, model, actor and their kind terms all cover the same rows.
        rows = [row("a", "fa")] * 5 + [row("b", "fb")] * 5
        head = fit(LOGIT, rows, np.array([1.0] * 5 + [0.0] * 5))
        merged = head.terms_of[head.columns[("family", "fa")]]
        self.assertIn("actor", merged)
        self.assertIn(("actor", "a"), head.aliased)

    def test_an_unseen_actor_is_drawn_from_its_cold_start_budget(self):
        rows = [row("a", "fa")] * 20
        head = fit(LOGIT, rows, np.ones(20) * 0.8, budget=0.7)
        eta, _ = head.draws([row("new", "fnew")], np.random.default_rng(0), 4000)
        known, _ = head.draws([row("a", "fa")], np.random.default_rng(0), 4000)
        self.assertGreater(eta.std(), known.std())
        self.assertLess(eta.std(), 2.0)
        spreads = _scaled(0.7)
        total = np.sqrt(sum(spreads[t.name] ** 2 for t in TERMS if t.executor))
        self.assertAlmostEqual(total, 0.7)


class EpochDetectorTests(unittest.TestCase):
    def test_a_collapse_in_success_is_detected_and_steady_noise_is_not(self):
        rng = np.random.default_rng(2)
        predicted = 2.0  # log-odds of about 0.88
        steady = [(predicted, float(rng.random() < 0.88)) for _ in range(40)]
        self.assertFalse(epochs.changed("success", steady))
        broken = steady[:25] + [(predicted, 0.0)] * 8
        self.assertTrue(epochs.changed("success", broken))

    def test_a_rise_in_cost_is_detected(self):
        rng = np.random.default_rng(3)
        base = [(0.0, rng.normal(0, 0.6)) for _ in range(25)]
        self.assertFalse(epochs.changed("cost", base))
        self.assertTrue(epochs.changed("cost", base + [(0.0, 1.6 + rng.normal(0, 0.6)) for _ in range(8)]))


if __name__ == "__main__":
    unittest.main()
