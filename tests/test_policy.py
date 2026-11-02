import unittest
from collections import Counter
from datetime import timedelta

from delegate import policy
from delegate.config import ConfigError
from delegate.ledger import LedgerError, fold
from delegate.policy import LEAD, Stats, select, standing
from tests.helpers import BASE, NOW, Book, env, make_config


def choose(config, book, kind="tool", cls="bounded", e=None, **options):
    return select(config, book.state(), kind, cls, NOW, e or env(), **options)


class ConfigTests(unittest.TestCase):
    def test_resources_take_instances_slots_or_a_capacity(self):
        config = make_config()
        self.assertEqual(config.resources["checkout"].instances, ["a", "b"])
        self.assertTrue(config.resources["memory"].amounts)
        self.assertEqual(config.resources["memory"].capacity, 31)

    def test_rejects_what_the_workflow_does_not_define(self):
        for bad in (
            "surprise = 1\n" + BASE,
            BASE + "\nawaitng_cap = 2\n",
            BASE.replace('check = "gated"', 'check = "maybe"'),
            BASE.replace(
                'measure = "pass-fail"\nclasses = ["bounded"]\n\n[kinds.probe]',
                'measure = "pass-fail"\nclasses = []\n\n[kinds.probe]',
            ),
            BASE + '\n[kinds.x]\ncheck = "gated"\nstate = "on"\nmeasure = "pass-fail"\nclasses = ["a"]\n'
            'resources = ["gpu"]\n',
            BASE + "\n[pools.cheap-pool]\n",
            BASE + "\n[pools.cheap-pool]\ndollars_per_point = 2\n",
            BASE + "\n[pools.cheap-pool]\nneeds = 40\n",
            BASE + "\n[pools.cheap-pool]\nneed = -1\n",
            BASE + '\n[pools.cheap-pool]\nneed = { "1w" = 25 }\n',
            BASE + '\n[pools.cheap-pool]\nneed = { "7d" = -1 }\n',
            BASE + "\n[pools.cheap-pool]\nneed = {}\n",
        ):
            with self.assertRaises(ConfigError):
                make_config(bad)

    def test_a_removed_key_is_named_with_why_it_went(self):
        for text, key in (
            (BASE.replace("[lead]\n", '[lead]\ngrain = "task"\n'), "lead.grain"),
            (
                BASE.replace(
                    'classes = ["bounded"]\n\n[kinds.probe]', 'classes = ["bounded"]\nreferences = {}\n\n[kinds.probe]'
                ),
                "kinds.recon.references",
            ),
            (BASE + "\n[pools.cheap-pool]\nneed_short = 5\n", "pools.cheap-pool.need_short"),
        ):
            with self.assertRaisesRegex(ConfigError, f"{key} is no longer used: .*; remove it"):
                make_config(text)


class LedgerTests(unittest.TestCase):
    def test_an_overturn_turns_its_root_corrected_with_zero_score(self):
        book = Book()
        root = book.task("port", "cheap", "held", score=80)
        book.add(event="overturn", root=root, redo_cost=12.0, reason="native check")
        task = book.state().tasks[root]
        self.assertEqual((task.outcome, task.score, task.redo_cost), ("corrected", 0.0, 12.0))

    def test_rejects_events_it_does_not_know(self):
        with self.assertRaises(LedgerError):
            fold([{"schema": "other", "event": "open", "at": NOW.isoformat()}])
        book = Book()
        book.task("tool", "cheap", "held")
        book.events.append(dict(book.events[0]))
        with self.assertRaises(LedgerError):
            book.state()


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_warm_up_takes_an_untried_executor_first(self):
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        result, events = choose(self.config, book)
        self.assertEqual((result["answer"], result["actor"], result["reason"]), ("delegate", "cheap", "warm-up"))
        self.assertEqual(events[-1]["event"], "open")

    def test_a_pool_check_carries_the_run_s_expected_dollars(self):
        # cheap has no record, so its run takes the kind's prior, here dear's $5.
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        pools = env()
        choose(self.config, book, e=pools)
        self.assertIn(("cheap-pool", 5.0, None), pools.pool.calls)
        self.assertIn(("dear-pool", 5.0, None), pools.pool.calls)

    def going(self, book, actor="cheap", cost=4.0, lead_cost=0.0):
        book.add(
            event="open",
            task="port-0101",
            kind="port",
            **{"class": "bounded"},
            actor=actor,
            reason="draw",
            expected={"actor_cost": cost, "lead_cost": lead_cost},
        )

    def test_runs_already_going_on_a_pool_count_toward_its_check(self):
        # A $4 port is going on cheap-pool, which admits $8: a $5 run makes $9, so dear is drawn.
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        self.going(book)
        pools = env(budget={"cheap-pool": 8})
        result, _ = choose(self.config, book, e=pools)
        self.assertEqual(result["actor"], "dear")
        self.assertIn(("cheap-pool", 9.0, None), pools.pool.calls)

    def test_a_pool_without_a_price_also_asks_its_need_for_each_run(self):
        config = make_config(BASE + "\n[pools.cheap-pool]\nneed = 40\n")
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        self.going(book)
        pools = env(max_need={"cheap-pool": 60})
        result, _ = choose(config, book, e=pools)
        self.assertEqual(result["actor"], "dear")
        self.assertIn(("cheap-pool", 9.0, 80.0), pools.pool.calls)

    def test_a_need_per_window_is_asked_for_each_run_in_each_window(self):
        config = make_config(BASE + '\n[pools.cheap-pool]\nneed = { "7d" = 25, "30d" = 6 }\n')
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        self.going(book)
        pools = env()
        choose(config, book, e=pools)
        self.assertIn(("cheap-pool", 9.0, {"7d": 50.0, "30d": 12.0}), pools.pool.calls)

    def test_a_run_on_the_lead_s_pool_carries_its_lead_cost_too(self):
        config = make_config(BASE.replace('pool = "dear-pool"', 'pool = "lead-pool"'))
        book = Book()
        book.task("tool", "dear", "held", cost=5, lead_cost=3)
        pools = env(rooms={"cheap-pool": False, "lead-pool": True})
        self.assertEqual(choose(config, book, e=pools)[0]["actor"], "dear")
        self.assertIn(("lead-pool", 8.0, None), pools.pool.calls)

    def test_with_only_untried_executors_on_busy_pools_the_task_waits(self):
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        result, _ = choose(self.config, book, e=env(budget={"cheap-pool": 4, "dear-pool": 4}))
        self.assertEqual((result["answer"], result["waiting_for"]), ("wait", "cheap-pool"))

    def test_no_second_task_before_a_first_verdict(self):
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        book.add(
            event="open",
            task="tool-0100",
            kind="tool",
            **{"class": "bounded"},
            actor="cheap",
            reason="warm-up",
            resources={},
        )
        result, _ = choose(self.config, book)
        self.assertEqual(result["actor"], "dear")

    def test_draw_prefers_the_cheaper_cost_per_success(self):
        book = Book()
        for _ in range(6):
            book.task("tool", "cheap", "held", cost=0.5)
            book.task("tool", "dear", "held", cost=5.0)
        picks = Counter(choose(self.config, book, e=env(seed=s))[0]["actor"] for s in range(40))
        self.assertGreater(picks["cheap"], picks["dear"])

    def test_a_cheap_executor_that_never_succeeds_is_dropped(self):
        # Its success rate is 94% likely below one half at 0 of 3 (Beta(1, 4)), and 88% at 0 of 2.
        book = Book()
        for _ in range(10):
            book.task("tool", "dear", "held", cost=0.5)
        for _ in range(2):
            book.task("tool", "cheap", "corrected", cost=0.2)
        _, events = choose(self.config, book)
        self.assertFalse([e for e in events if e["event"] == "drop"])
        book.task("tool", "cheap", "corrected", cost=0.2)
        result, events = choose(self.config, book)
        self.assertEqual(result["actor"], "dear")
        self.assertEqual([e["actor"] for e in events if e["event"] == "drop"], ["cheap"])

    def test_two_executors_of_similar_cost_per_success_are_both_kept(self):
        book = Book()
        for _ in range(6):
            book.task("tool", "cheap", "held", cost=1.0)
            book.task("tool", "dear", "held", cost=1.1)
        _, events = choose(self.config, book)
        self.assertFalse([e for e in events if e["event"] == "drop"])

    def test_reviewed_fallback_counts_only_tasks_after_admission(self):
        book = Book()
        for _ in range(2):
            book.task("port", "cheap", "held", reason="admit", score=70)
        for _ in range(3):
            book.task("port", "cheap", "corrected")
        _, events = choose(self.config, book, kind="port")
        self.assertIn("cheap", [e["actor"] for e in events if e["event"] == "drop"])

    def test_a_reliable_executor_is_not_dropped_for_costing_more_than_another(self):
        # concord's record: Devin held 12 of 12 diagnoses at about $2 each beside Luna at $0.17. Dropping Devin
        # would leave only Luna, whose Codex pool can be exhausted; the draw already prefers the cheaper one.
        book = Book()
        for _ in range(12):
            book.task("tool", "dear", "held", cost=2.0)
            book.task("tool", "cheap", "held", cost=0.17)
        _, events = choose(self.config, book)
        self.assertFalse([e for e in events if e["event"] == "drop"])

    def test_an_executor_is_dropped_when_the_lead_is_probably_cheaper(self):
        book = Book()
        for _ in range(6):
            book.task("tool", LEAD, "held", cost=0.5, lead_cost=None)
            book.task("tool", "dear", "held", cost=10.0)
        _, events = choose(self.config, book)
        self.assertEqual([e["actor"] for e in events if e["event"] == "drop"], ["dear"])

    def test_one_integrity_correction_suspends_in_reviewed_kinds_only(self):
        book = Book()
        for _ in range(2):
            book.task("port", "cheap", "held", reason="admit", score=70)
        book.task("port", "cheap", "corrected", defect="integrity")
        book.task("tool", "cheap", "corrected", defect="integrity")
        state = book.state()
        self.assertEqual(standing(Stats(self.config, state, self.config.kinds["port"], env()), "cheap"), "suspended")
        self.assertEqual(standing(Stats(self.config, state, self.config.kinds["tool"], env()), "cheap"), "admitted")
        book.add(event="resume", actor="cheap", kind="port", reason="reviewed")
        self.assertEqual(
            standing(Stats(self.config, book.state(), self.config.kinds["port"], env()), "cheap"), "admitted"
        )

    def test_reviewed_admission_passes_at_two_of_three_and_fails_at_two_failures(self):
        port = self.config.kinds["port"]
        book = Book()
        book.task("port", "cheap", "held", reason="admit", score=60)
        self.assertEqual(standing(Stats(self.config, book.state(), port, env()), "cheap"), "trial")
        book.task("port", "cheap", "steered", reason="admit", score=60)
        self.assertEqual(standing(Stats(self.config, book.state(), port, env()), "cheap"), "admitted")
        book.task("port", "dear", "corrected", reason="admit")
        book.task("port", "dear", "corrected", reason="admit")
        self.assertEqual(standing(Stats(self.config, book.state(), port, env()), "dear"), "failed")
        book.add(event="resume", actor="dear", kind="port", reason="lead decision")
        self.assertEqual(standing(Stats(self.config, book.state(), port, env()), "dear"), "admitted")

    def test_class_rule_and_lead_only_class(self):
        book = Book()
        for _ in range(2):
            book.task("port", "cheap", "held", reason="admit", score=60)
        self.assertEqual(choose(self.config, book, kind="port", cls="first-reading")[0]["actor"], "cheap")
        book2 = Book()
        for _ in range(2):
            book2.task("port", "cheap", "held", reason="admit", score=60, cls="bounded")
        result, _ = choose(self.config, book2, kind="port", cls="redesign")
        self.assertEqual((result["answer"], result["actor"]), ("lead", LEAD))

    def test_a_reviewed_kind_with_nobody_admitted_goes_to_the_lead(self):
        result, _ = choose(self.config, Book(), kind="port")
        self.assertEqual(result["answer"], "lead")

    def test_admit_runs_a_trial_on_the_lowest_class(self):
        result, _ = choose(self.config, Book(), kind="port", admit="cheap")
        self.assertEqual((result["answer"], result["reason"]), ("admit", "admit"))
        with self.assertRaises(policy.SelectError):
            choose(self.config, Book(), kind="port", cls="first-reading", admit="cheap")

    def test_assign_opens_for_a_named_admitted_executor_with_the_reason_recorded(self):
        # The pool is full and the draw would warm up another executor; the assignment holds and says why.
        full = env(rooms={"cheap-pool": False, "dear-pool": False, "lead-pool": True})
        result, events = choose(self.config, Book(), kind="tool", e=full, assign="dear", why="user: dear confirms it")
        self.assertEqual((result["answer"], result["actor"], result["reason"]), ("delegate", "dear", "assigned"))
        self.assertEqual(events[-1]["why"], "user: dear confirms it")
        self.assertFalse(full.pool.calls)
        with self.assertRaisesRegex(policy.SelectError, "needs --why"):
            choose(self.config, Book(), kind="tool", assign="dear")
        with self.assertRaisesRegex(policy.SelectError, "candidate in port"):
            choose(self.config, Book(), kind="port", assign="dear", why="lead")

    def test_wait_while_pools_are_full_then_the_lead_after_the_deadline(self):
        full = env(rooms={"cheap-pool": False, "dear-pool": False, "lead-pool": True})
        result, events = choose(self.config, Book(), e=full)
        self.assertEqual((result["answer"], result["waiting_for"]), ("wait", "cheap-pool"))
        # A wait names each executor its pool check refused, with what was asked and why.
        self.assertEqual(
            result["refused"]["dear"],
            {"pool": "dear-pool", "dollars": None, "state": "refused", "reasons": ["dear-pool has no room"]},
        )
        self.assertEqual(set(result["refused"]), {"cheap", "dear"})
        self.assertFalse(events)
        later = choose(self.config, Book(), e=full, since=NOW - timedelta(minutes=61))[0]
        self.assertEqual(later["answer"], "lead")
        no_lead = env(rooms={"cheap-pool": False, "dear-pool": False, "lead-pool": False})
        self.assertEqual(choose(self.config, Book(), e=no_lead, since=NOW - timedelta(minutes=61))[0]["answer"], "wait")

    def test_the_lead_fallback_counts_the_runs_already_going_on_the_lead_s_pool(self):
        # dear runs on the lead's pool and has a $4 port going there, with $1 of lead cost, which the lead's own
        # run must leave room for.
        config = make_config(BASE.replace('pool = "dear-pool"', 'pool = "lead-pool"'))
        book = Book()
        self.going(book, actor="dear", cost=4.0, lead_cost=1.0)
        pools = env(rooms={"cheap-pool": False, "lead-pool": True}, budget={"lead-pool": 4})
        later = choose(config, book, e=pools, since=NOW - timedelta(minutes=61))[0]
        self.assertEqual((later["answer"], later["waiting_for"]), ("wait", "cheap-pool"))
        self.assertEqual(pools.pool.calls[-1][0], "lead-pool")
        self.assertGreaterEqual(pools.pool.calls[-1][1], 5.0)

    def test_named_instances_and_amounts_against_capacity(self):
        book = Book()
        first, _ = choose(self.config, book, resources=["checkout", "memory=16"])
        self.assertEqual(first["resources"], {"checkout": "a", "memory": 16})
        book.add(
            event="open",
            task="tool-0101",
            kind="tool",
            **{"class": "bounded"},
            actor="dear",
            reason="draw",
            resources={"checkout": "a", "memory": 16},
        )
        second, _ = choose(self.config, book, resources=["checkout", "memory=16"])
        self.assertEqual((second["answer"], second["waiting_for"]), ("wait", "memory"))
        third, _ = choose(self.config, book, resources=["checkout", "memory=15"])
        self.assertEqual(third["resources"]["checkout"], "b")

    def test_a_counted_resource_reserves_the_slots_asked_for(self):
        # A task that relaunches the live client holds all of it, since a relaunch ends every job on it.
        config = make_config(BASE.replace("[resources]\n", "[resources]\nlive-client = 2\n"))
        book = Book()
        first, _ = choose(config, book, resources=["live-client"])
        self.assertEqual(first["resources"], {"live-client": 1})
        book.add(
            event="open",
            task="tool-0101",
            kind="tool",
            **{"class": "bounded"},
            actor="dear",
            reason="draw",
            resources={"live-client": 1},
        )
        whole, _ = choose(config, book, resources=["live-client=2"])
        self.assertEqual((whole["answer"], whole["waiting_for"]), ("wait", "live-client"))
        self.assertEqual(choose(config, Book(), resources=["live-client=2"])[0]["resources"], {"live-client": 2})
        for bad in ("live-client=3", "live-client=1.5", "checkout=2"):
            with self.assertRaises(ConfigError):
                choose(config, Book(), resources=[bad])

    def test_quota_due_to_expire_discounts_a_pool_s_runs_without_forcing_them(self):
        # Both hold 3 of 3; dear costs $5 a run against cheap's $1, plus $1 of the lead's cost each. With dear's
        # pool far behind its pace its run costs only the lead's dollar, so it wins most draws but not all.
        def wins(config, marginal, kind="tool"):
            book = Book()
            for actor in ("cheap", "dear"):
                book.add(event="resume", actor=actor, kind=kind, reason="admitted")
            for _ in range(3):
                book.task(kind, "cheap", "held", cost=1, lead_cost=1, score=80 if kind == "port" else None)
                book.task(kind, "dear", "held", cost=5, lead_cost=1, score=80 if kind == "port" else None)
            results = [choose(config, book, kind=kind, e=env(marginal=marginal, seed=i)) for i in range(60)]
            return sum(r["actor"] == "dear" for r, _ in results), results[0][1][-1]

        full, _ = wins(self.config, None)
        spare, event = wins(self.config, {"dear-pool": 0.0})
        self.assertLess(full, 12)
        self.assertTrue(30 < spare < 60, spare)
        self.assertEqual(event["marginal"], {"dear": 0.0})
        reviewed, _ = wins(self.config, {"dear-pool": 0.0}, kind="port")
        self.assertGreater(reviewed, 30)
        # The lead's own pool is never discounted: the lead needs its quota.
        lead_pool = make_config(BASE.replace('pool = "dear-pool"', 'pool = "lead-pool"'))
        on_lead, event = wins(lead_pool, {"lead-pool": 0.0})
        self.assertLess(on_lead, 12)
        self.assertNotIn("marginal", event)

    def test_deferred_kind_takes_the_cheapest_and_holds_one_task_before_a_verdict(self):
        book = Book()
        for actor in ("cheap", "dear"):
            book.add(event="resume", actor=actor, kind="recon", reason="passed calibration and live task")
        book.task("recon", "cheap", "awaiting", cost=0.5)
        book.task("recon", "dear", "awaiting", cost=5.0, verdict="held")
        result, _ = choose(self.config, book, kind="recon")
        self.assertEqual((result["actor"], result["reason"]), ("dear", "cheapest"))

    def test_a_retry_skips_the_actor_that_failed(self):
        book = Book()
        failed = book.task("tool", "cheap", "corrected", cost=0.1)
        book.task("tool", "dear", "held", cost=5.0)
        result, _ = choose(self.config, book, retry=failed)
        self.assertEqual(result["actor"], "dear")

    def test_lead_score_counts_as_a_fraction_of_a_success(self):
        book = Book()
        book.task("port", "cheap", "held", score=30)
        stats = Stats(self.config, book.state(), self.config.kinds["port"], env())
        self.assertEqual(policy.counts(stats.final("cheap"), stats.kind), (1.3, 1.7))

    def test_redo_cost_enters_expected_cost(self):
        book = Book()
        root = book.task("tool", "cheap", "held", cost=1.0)
        before = Stats(self.config, book.state(), self.config.kinds["tool"], env()).expected_cost("cheap", "bounded")
        book.add(event="overturn", root=root, redo_cost=10.0, reason="later")
        after = Stats(self.config, book.state(), self.config.kinds["tool"], env()).expected_cost("cheap", "bounded")
        self.assertGreater(after, before + 4)

    def test_an_open_records_the_expected_actor_and_lead_costs_apart(self):
        # Redo cost is spent later, not by this run, so neither recorded part carries it.
        book = Book()
        root = book.task("tool", "dear", "held", cost=2, lead_cost=1)
        book.add(event="overturn", root=root, redo_cost=3, reason="test")
        book.task("tool", "cheap", "held", cost=2, lead_cost=1)
        result, events = choose(self.config, book)
        self.assertEqual(events[-1]["expected"], {"actor_cost": 2.0, "lead_cost": 1.0})

    def test_the_lead_is_charged_its_own_cost_once(self):
        book = Book()
        book.task("tool", "cheap", "held", cost=1.0, lead_cost=4.0)
        book.task("tool", LEAD, "held", cost=3.0, lead_cost=None)
        stats = Stats(self.config, book.state(), self.config.kinds["tool"], env())
        self.assertAlmostEqual(stats.expected_cost(LEAD, "bounded"), 3.0)


if __name__ == "__main__":
    unittest.main()
