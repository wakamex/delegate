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
            BASE + "\n[pools.cheap-pool]\nneed = 40\ndollars_per_point = 2\n",
            BASE + "\n[pools.cheap-pool]\nneeds = 40\n",
            BASE + "\n[pools.cheap-pool]\nneed_short = -1\n",
        ):
            with self.assertRaises(ConfigError):
                make_config(bad)


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

    def test_a_first_run_counts_the_runs_already_going_on_its_pool(self):
        # cheap-pool has 60 short-window points free: a first run asks for 50, and a port already going there adds
        # 25 at the default short need, so dear, with a record, is drawn; at a need_short of 5 the first run fits.
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        book.add(event="open", task="port-0101", kind="port", **{"class": "bounded"}, actor="cheap", reason="draw")
        result, _ = choose(self.config, book, e=env(max_short={"cheap-pool": 60}))
        self.assertEqual((result["actor"], result["reason"]), ("dear", "draw"))
        small_runs = make_config(BASE + "\n[pools.cheap-pool]\nneed_short = 5\n")
        result, _ = choose(small_runs, book, e=env(max_short={"cheap-pool": 60}))
        self.assertEqual((result["actor"], result["reason"]), ("cheap", "warm-up"))

    def test_warm_up_needs_room_for_half_a_short_window(self):
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        pools = env(max_short={"cheap-pool": 40})
        result, _ = choose(self.config, book, e=pools)
        self.assertEqual((result["actor"], result["reason"]), ("dear", "draw"))
        self.assertIn(("cheap-pool", None, policy.WARM_UP_NEED_SHORT), pools.pool.calls)
        self.assertIn(("dear-pool", None, None), pools.pool.calls)

    def test_a_pool_without_a_price_checks_its_fixed_need(self):
        # dear-pool has no dollars_per_point, as for a subscription aop cannot price, so each run asks for its need.
        config = make_config(BASE + "\n[pools.dear-pool]\nneed = 40\n")
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        pools = env()
        result, events = choose(config, book, e=pools, retry=book.task("tool", "cheap", "corrected"))
        self.assertEqual(result["actor"], "dear")
        self.assertIn(("dear-pool", 40.0, None), pools.pool.calls)
        self.assertEqual(events[-1]["expected"]["pool_points"], 40.0)

    def test_runs_already_going_on_a_pool_count_toward_its_check(self):
        # cheap-pool has 60 long-window points free and runs take 40: one run fits, a second waits.
        config = make_config(BASE + "\n[pools.cheap-pool]\nneed = 40\n")
        pools = env(max_need={"cheap-pool": 60})
        book = Book()
        first, events = choose(config, book, kind="port", e=pools, admit="cheap")
        self.assertEqual(first["answer"], "admit")
        book.add(**{k: v for k, v in events[-1].items() if k != "at"})
        second, _ = choose(config, book, kind="probe", e=pools, admit="cheap")
        self.assertEqual((second["answer"], second["waiting_for"]), ("wait", "cheap-pool"))
        self.assertEqual(pools.pool.calls[-1], ("cheap-pool", 80.0, 50.0))

    def test_a_pool_s_short_need_counts_for_each_run(self):
        # Runs on dear-pool take 5 points of its 5-hour window, so two going and a third ask for 15, not 75.
        config = make_config(BASE + "\n[pools.dear-pool]\nneed_short = 5\n")
        book = Book()
        book.task("tool", "dear", "held", cost=5)
        book.task("tool", "cheap", "held", cost=5)
        for n in (1, 2):
            book.add(
                event="open", task=f"port-010{n}", kind="port", **{"class": "bounded"}, actor="dear", reason="draw"
            )
        pools = env(rooms={"cheap-pool": False, "dear-pool": True, "lead-pool": True})
        result, _ = choose(config, book, e=pools)
        self.assertEqual(result["actor"], "dear")
        self.assertEqual(pools.pool.calls[-1], ("dear-pool", 15.0, 15.0))
        # With nothing going a run asks for the pool's 5; a first run in a kind still asks for the warm-up's 50.
        idle = env(rooms={"cheap-pool": False, "dear-pool": True, "lead-pool": True})
        idle_book = Book()
        idle_book.task("tool", "dear", "held", cost=5)
        choose(config, idle_book, e=idle)
        self.assertIn(("dear-pool", None, 5.0), idle.pool.calls)
        idle.pool.calls.clear()
        choose(config, Book(), e=idle)
        self.assertIn(("dear-pool", None, policy.WARM_UP_NEED_SHORT), idle.pool.calls)

    def test_with_only_untried_executors_on_busy_pools_the_task_waits(self):
        book = Book()
        book.add(event="open", task="port-0101", kind="port", **{"class": "bounded"}, actor="dear", reason="draw")
        result, _ = choose(self.config, book, e=env(max_short={"cheap-pool": 40, "dear-pool": 60}))
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
        self.assertFalse(events)
        later = choose(self.config, Book(), e=full, since=NOW - timedelta(minutes=61))[0]
        self.assertEqual(later["answer"], "lead")
        no_lead = env(rooms={"cheap-pool": False, "dear-pool": False, "lead-pool": False})
        self.assertEqual(choose(self.config, Book(), e=no_lead, since=NOW - timedelta(minutes=61))[0]["answer"], "wait")

    def test_the_lead_fallback_counts_the_runs_already_going_on_the_lead_s_pool(self):
        # dear runs on the lead's pool and has a port going there, so the lead's own run asks for 25 plus 25.
        config = make_config(BASE.replace('pool = "dear-pool"', 'pool = "lead-pool"'))
        book = Book()
        book.add(event="open", task="port-0101", kind="port", **{"class": "bounded"}, actor="dear", reason="draw")
        pools = env(rooms={"cheap-pool": False, "lead-pool": True}, max_short={"lead-pool": 40})
        later = choose(config, book, e=pools, since=NOW - timedelta(minutes=61))[0]
        self.assertEqual((later["answer"], later["waiting_for"]), ("wait", "cheap-pool"))
        self.assertEqual(pools.pool.calls[-1], ("lead-pool", 10.0, 50.0))

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

    def test_catch_up_sends_gated_work_to_an_expiring_pool(self):
        book = Book()
        for _ in range(3):
            book.task("tool", "cheap", "held", cost=0.1)
            book.task("tool", "dear", "held", cost=5.0)
        result, _ = choose(self.config, book, e=env(catch_up={"dear-pool"}))
        self.assertEqual((result["actor"], result["reason"]), ("dear", "catch-up"))

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

    def test_the_lead_is_charged_its_own_cost_once(self):
        book = Book()
        book.task("tool", "cheap", "held", cost=1.0, lead_cost=4.0)
        book.task("tool", LEAD, "held", cost=3.0, lead_cost=None)
        stats = Stats(self.config, book.state(), self.config.kinds["tool"], env())
        self.assertAlmostEqual(stats.expected_cost(LEAD, "bounded"), 3.0)


if __name__ == "__main__":
    unittest.main()
