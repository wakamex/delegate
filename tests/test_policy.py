import unittest
from collections import Counter
from datetime import timedelta

import numpy as np

from delegate import policy
from delegate.config import LEAD, ConfigError
from delegate.ledger import LedgerError, fold
from delegate.policy import SelectError, select
from tests.helpers import BASE, HOST, NOW, Book, env, make_config, world


def choose(config, book=None, kind="tool", cls="bounded", e=None, **options):
    return select(world(config, book), e or env(), kind, cls, NOW, **options)


class ConfigTests(unittest.TestCase):
    def test_resources_take_instances_slots_or_a_capacity(self):
        config = make_config()
        self.assertEqual(config.resources["checkout"].instances, ["a", "b"])
        self.assertTrue(config.resources["memory"].amounts)
        self.assertEqual(config.resources["memory"].capacity, 31)

    def test_actors_come_from_the_host_roster_with_their_family(self):
        config = make_config()
        self.assertEqual(sorted(config.executors), ["cheap", "dear"])
        self.assertEqual((config.executors["dear"].family, config.executors["dear"].price), ("gpt-sol", 3.0))
        self.assertEqual(config.host.lead.pool, "lead-pool")

    def test_a_host_resource_is_shared_and_declared_once(self):
        host = HOST + "\n[resources]\ngpu = 1\n"
        config = make_config(
            BASE.replace(
                'classes = ["bounded"]\ninvestigation', 'resources = ["gpu"]\nclasses = ["bounded"]\ninvestigation'
            ),
            host=host,
        )
        self.assertTrue(config.resource("gpu").shared)
        with self.assertRaises(ConfigError):
            make_config(BASE.replace("[resources]\n", "[resources]\ngpu = 1\n"), host=host)

    def test_rejects_what_the_workflow_does_not_define(self):
        for bad in (
            "surprise = 1\n" + BASE,
            BASE.replace('check = "gated"', 'check = "maybe"'),
            BASE + '\n[kinds.x]\ncheck = "gated"\nmeasure = "pass-fail"\nresources = ["gpu"]\n',
        ):
            with self.assertRaises(ConfigError):
                make_config(bad)
        for host in (HOST + "\n[actors.lead]\nagent = 'a'\nmodel = 'b'\n", HOST + "\n[pools.cheap-pool]\nruns = 0\n"):
            with self.assertRaises(ConfigError):
                make_config(host=host)

    def test_a_removed_key_is_named_with_why_it_went(self):
        for text, key in (
            (BASE + '\n[executors.x]\nagent = "a"\nmodel = "b"\n', "executors"),
            (BASE.replace('check = "gated"', 'check = "gated"\nstate = "on"'), "kinds.tool.state"),
            ("wait_deadline_minutes = 60\n" + BASE, "wait_deadline_minutes"),
        ):
            with self.assertRaisesRegex(ConfigError, f"{key} is no longer used: .*; remove it"):
                make_config(text)


class LedgerTests(unittest.TestCase):
    def test_an_overturn_turns_its_root_corrected_with_zero_score(self):
        book = Book()
        root = book.task("port", "cheap", "held", score=80)
        self.assertTrue(book.state().tasks[root].stood)
        book.add(event="overturn", root=root, redo_cost=12.0, reason="native check")
        task = book.state().tasks[root]
        self.assertEqual((task.outcome, task.score, task.redo_cost, task.stood), ("corrected", 0.0, 12.0, False))
        self.assertFalse(task.integrity)

    def test_an_integrity_overturn_marks_the_root_s_defect(self):
        book = Book()
        root = book.task("tool", "cheap", "held")
        book.add(event="overturn", root=root, redo_cost=3.0, reason="faked the gate", defect="integrity")
        self.assertTrue(book.state().tasks[root].integrity)

    def test_a_deferred_task_has_not_stood_until_its_verdict(self):
        book = Book()
        task = book.task("recon", "cheap", "awaiting")
        self.assertIsNone(book.state().tasks[task].stood)
        book.add(event="verdict", task=task, outcome="held", evidence=["block 1"])
        self.assertTrue(book.state().tasks[task].stood)

    def test_rejects_events_it_does_not_know(self):
        with self.assertRaises(LedgerError):
            fold([{"schema": "other", "event": "open", "at": NOW.isoformat()}])
        book = Book()
        book.task("tool", "cheap", "held")
        book.events.append(dict(book.events[0]))
        with self.assertRaises(LedgerError):
            book.state()


def record(book, kind, actor, n, outcome, cost, **options):
    for _ in range(n):
        book.task(kind, actor, outcome, cost=cost, **options)
    return book


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_an_open_records_the_draw_and_its_selection_probabilities(self):
        answer, events = choose(self.config)
        event = events[0]
        self.assertIn(answer["answer"], ("delegate", "lead"))
        self.assertEqual((event["reason"], event["policy"]), ("draw", policy.__version__))
        self.assertEqual(set(event["predictions"]), {"cheap", "dear", LEAD})
        self.assertAlmostEqual(sum(p["p"] for p in event["predictions"].values()), 1.0, places=2)
        self.assertTrue({"q", "value", "cost", "integrity"} <= set(event["expected"]))

    def test_an_executor_that_succeeds_cheaply_wins_most_draws(self):
        book = record(Book(), "tool", "cheap", 15, "held", 0.4)
        record(book, "tool", "dear", 15, "corrected", 3.0, defect="failed-gate")
        record(book, "tool", LEAD, 5, "held", 4.0)
        chosen = Counter(choose(self.config, book, e=env(seed=s))[0]["actor"] for s in range(20))
        self.assertGreaterEqual(chosen["cheap"], 17)
        self.assertEqual(chosen["dear"], 0)

    def test_a_cheap_executor_that_never_succeeds_loses_without_being_dropped(self):
        # Each failure costs the lead a correction, which is what makes a cheap failing executor dear.
        book = record(Book(), "tool", "cheap", 15, "corrected", 0.4, defect="failed-gate", lead_cost=2.0)
        record(book, "tool", "dear", 15, "held", 3.0, lead_cost=0.2)
        record(book, "tool", LEAD, 5, "held", 8.0)
        answer, events = choose(self.config, book)
        self.assertEqual(answer["actor"], "dear")
        self.assertIn("cheap", events[0]["predictions"])  # still drawn, just rarely chosen

    def test_skill_in_one_kind_carries_to_another_kind(self):
        book = record(Book(), "tool", "cheap", 15, "held", 0.5)
        record(book, "tool", "dear", 15, "corrected", 0.5, defect="failed-gate")
        record(book, "probe", "cheap", 3, "held", 0.5)
        record(book, "probe", "dear", 3, "held", 0.5)
        heads = policy.fit_heads(world(self.config, book))
        fc = policy.forecast(
            world(self.config, book),
            heads,
            self.config.kinds["recon"],
            "bounded",
            ["cheap", "dear"],
            NOW,
            False,
            np.random.default_rng(0),
        )
        self.assertGreater(fc.q[:, 0].mean(), fc.q[:, 1].mean())

    def test_integrity_defects_raise_an_executor_s_expected_damage(self):
        book = Book()
        for i in range(20):
            book.task("port", "cheap", "held", score=70)
            book.task("port", "dear", "held", score=70)
            if i % 4 == 0:
                book.task("port", "cheap", "corrected", defect="integrity", score=0)
        w = world(self.config, book)
        heads = policy.fit_heads(w)
        fc = policy.forecast(
            w, heads, self.config.kinds["port"], "bounded", ["cheap", "dear"], NOW, False, np.random.default_rng(0)
        )
        self.assertGreater(fc.integrity[:, 0].mean(), fc.integrity[:, 1].mean())

    def test_a_suspension_stops_an_actor_everywhere_or_in_one_kind_until_resumed(self):
        book = Book().add(event="suspend", actor="cheap", kind="tool", reason="test")
        _, events = choose(self.config, book)
        self.assertNotIn("cheap", events[0]["predictions"])
        _, events = choose(self.config, book, kind="probe")
        self.assertIn("cheap", events[0]["predictions"])
        book.add(event="resume", actor="cheap", kind="tool", reason="test").add(
            event="suspend", actor="dear", reason="x"
        )
        _, events = choose(self.config, book, kind="probe")
        self.assertEqual(set(events[0]["predictions"]), {"cheap", LEAD})

    def test_assign_opens_for_the_named_actor_with_its_reason_and_cause(self):
        answer, events = choose(self.config, assign="dear", why="user: confirms a fixture", cause="capability")
        self.assertEqual((answer["actor"], events[0]["reason"], events[0]["cause"]), ("dear", "assigned", "capability"))
        _, events = choose(self.config, assign="cheap", why="user: wants it")
        self.assertEqual(events[0]["cause"], "user")
        with self.assertRaises(SelectError):
            choose(self.config, assign="cheap")
        with self.assertRaises(SelectError):
            choose(self.config, assign="nobody", why="x")

    def test_a_kind_needing_a_capability_draws_only_actors_that_have_it(self):
        config = make_config(
            BASE.replace('classes = ["bounded", "hard"]', 'classes = ["bounded", "hard"]\ncapabilities = ["gpu"]'),
            host=HOST.replace("price = 0.5", 'price = 0.5\ncapabilities = ["gpu"]'),
        )
        _, events = choose(config)
        self.assertEqual(set(events[0]["predictions"]), {"cheap", LEAD})

    def test_a_retry_skips_the_actor_that_failed(self):
        book = Book()
        failed = book.task("tool", "cheap", "corrected", defect="failed-gate")
        for seed in range(5):
            answer, events = choose(self.config, book, retry=failed, e=env(seed=seed))
            self.assertNotEqual(answer["actor"], "cheap")
            self.assertEqual(events[0]["retry_of"], failed)

    def test_a_pool_check_carries_the_run_s_cost_and_a_full_pool_is_refused(self):
        e = env(rooms={"cheap-pool": False, "dear-pool": True, "lead-pool": True})
        answer, events = choose(self.config, e=e)
        self.assertNotEqual(answer["actor"], "cheap")
        self.assertIn("cheap", events[0]["refused"])
        self.assertTrue(all(d is not None and d > 0 for _, d in e.pool.calls))

    def test_a_pool_takes_at_most_its_runs_across_the_host(self):
        config = make_config(host=HOST + "\n[pools.dear-pool]\nruns = 1\n")
        book = Book().add(
            event="open",
            task="tool-0001",
            kind="tool",
            **{"class": "bounded"},
            actor="dear",
            reason="draw",
            resources={},
        )
        _, events = choose(config, book)
        self.assertIn("dear", events[0]["refused"])

    def test_with_every_pool_full_the_task_waits_and_a_broken_check_is_an_error(self):
        full = {"cheap-pool": False, "dear-pool": False, "lead-pool": False}
        answer, events = choose(self.config, e=env(rooms=full))
        self.assertEqual((answer["answer"], answer["waiting_for"], events), ("wait", "pool room", []))
        broken = dict.fromkeys(full, "error")
        self.assertEqual(choose(self.config, e=env(rooms=broken))[0]["answer"], "error")

    def test_a_pool_the_check_cannot_price_takes_one_run_at_a_time(self):
        rooms = {"cheap-pool": "unavailable", "dear-pool": False, "lead-pool": False}
        answer, _ = choose(self.config, e=env(rooms=rooms))
        self.assertEqual(answer["actor"], "cheap")
        book = Book().add(
            event="open",
            task="tool-0001",
            kind="tool",
            **{"class": "bounded"},
            actor="cheap",
            reason="draw",
            resources={},
        )
        self.assertEqual(choose(self.config, book, e=env(rooms=rooms))[0]["answer"], "wait")

    def test_the_lead_takes_one_task_at_a_time(self):
        book = Book().add(
            event="open",
            task="tool-0001",
            kind="tool",
            **{"class": "bounded"},
            actor=LEAD,
            reason="draw",
            resources={},
        )
        _, events = choose(self.config, book)
        self.assertIn(LEAD, events[0]["refused"])

    def test_the_open_names_the_account_the_pool_check_chose(self):
        e = env(rooms={"cheap-pool": True, "dear-pool": False, "lead-pool": False}, accounts={"cheap-pool": "b"})
        answer, events = choose(self.config, e=e)
        self.assertEqual((answer["account"], events[0]["account"]), ("b", "b"))

    def test_named_instances_and_amounts_against_capacity(self):
        book = Book()
        for n, resources in enumerate(({"checkout": "a"}, {"checkout": "b", "memory": 20})):
            book.add(
                event="open",
                task=f"tool-{n:04d}",
                kind="tool",
                **{"class": "bounded"},
                actor="cheap",
                reason="draw",
                resources=resources,
            )
        answer, _ = choose(self.config, book, resources=["checkout"])
        self.assertEqual((answer["answer"], answer["waiting_for"]), ("wait", "checkout"))
        self.assertEqual(choose(self.config, book, resources=["memory=12"])[0]["answer"], "wait")
        self.assertEqual(choose(self.config, book, resources=["memory=11"])[0]["resources"], {"memory": 11})
        with self.assertRaises(ConfigError):
            choose(self.config, book, resources=["memory=40"])

    def test_quota_due_to_expire_discounts_a_pool_without_forcing_it(self):
        book = record(Book(), "tool", "cheap", 10, "held", 1.0)
        record(book, "tool", "dear", 10, "held", 1.0)
        free = env(marginal={"dear-pool": 0.0})
        _, events = choose(self.config, book, e=free)
        self.assertEqual(events[0]["scarcity"], {"dear": 0.05})
        self.assertGreater(events[0]["predictions"]["dear"]["p"], events[0]["predictions"]["cheap"]["p"])


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_an_open_records_what_the_actor_is(self):
        _, events = choose(self.config, assign="dear", why="this test")
        self.assertEqual(events[0]["identity"], {"agent": "codex", "model": "gpt-6.1-sol", "effort": None})

    def test_the_lead_is_not_the_executor_running_its_model(self):
        config = make_config(host=HOST + '\n[actors.opus]\nagent = "claude"\nmodel = "claude-opus-5-5"\n')
        self.assertNotEqual(policy.actor_key(config, "opus"), policy.actor_key(config, LEAD))

    def test_an_actors_event_says_who_old_names_meant(self):
        # Before opens recorded identities, this project called the roster's cheap actor "devin".
        book = Book().add(event="actors", actors={"devin": {"agent": "devin", "model": "swe-2-high", "effort": None}})
        task = book.task("tool", "devin", "held")
        w = world(self.config, book)
        self.assertEqual(policy.task_key(w, "test", w.projects["test"][1].tasks[task]), "devin/swe-2-high")
        self.assertEqual(policy.actor_key(self.config, "cheap"), "devin/swe-2-high")
        row = next(r for r in policy.scorecard(w, env(), NOW, "tool") if r["actor"] == "cheap")
        self.assertEqual((row["tasks"], row["names"]), (1, ["devin"]))

    def test_one_name_for_another_model_is_another_actor(self):
        book = Book().add(event="actors", actors={"cheap": {"agent": "devin", "model": "swe-1", "effort": None}})
        book.task("tool", "cheap", "held")
        rows = policy.scorecard(world(self.config, book), env(), NOW, "tool")
        mine = next(r for r in rows if r["actor"] == "cheap" and r["q"] is not None)
        self.assertEqual(mine["tasks"], 0)
        self.assertEqual(sum(r["tasks"] for r in rows), 1)

    def test_a_suspension_under_an_old_name_stops_the_actor_it_meant(self):
        book = Book().add(event="actors", actors={"devin": {"agent": "devin", "model": "swe-2-high", "effort": None}})
        book.add(event="suspend", actor="devin", kind="tool", reason="locked out")
        _, events = choose(self.config, book)
        self.assertNotIn("cheap", events[0]["predictions"])


class EpochTests(unittest.TestCase):
    def test_an_actor_whose_results_collapse_starts_a_new_epoch(self):
        config = make_config()
        book = Book()
        for i in range(40):
            task = book.task("tool", "cheap", "held" if i < 25 else "corrected", defect="failed-gate")
            opened = next(e for e in book.events if e.get("task") == task and e["event"] == "open")
            opened["expected"] = {"q": 0.9, "cost": 1.0}
        events = policy.detect_epochs(world(config, book), "cheap", NOW)
        self.assertEqual([(e["event"], e["actor"], e["signal"]) for e in events], [("epoch", "cheap", "success")])
        book.add(**{k: v for k, v in events[0].items() if k != "at"})
        self.assertEqual(
            policy.row(
                world(config, book),
                "test",
                config.kinds["tool"],
                "bounded",
                policy.actor_key(config, "cheap"),
                config.executors["cheap"].identity,
                NOW + timedelta(1),
                "success",
            ).epoch,
            1,
        )
        self.assertEqual(policy.detect_epochs(world(config, book), "cheap", NOW), [])

    def test_a_steady_actor_does_not(self):
        config = make_config()
        book = Book()
        for i in range(40):
            task = book.task("tool", "cheap", "held" if i % 10 else "corrected", defect="failed-gate")
            opened = next(e for e in book.events if e.get("task") == task and e["event"] == "open")
            opened["expected"] = {"q": 0.9, "cost": 1.0}
        self.assertEqual(policy.detect_epochs(world(config, book), "cheap", NOW), [])


class ScorecardTests(unittest.TestCase):
    def test_spend_per_standing_result_counts_failures_and_aborts(self):
        book = Book()
        book.task("tool", "cheap", "held", cost=1.0, lead_cost=0.5)
        book.task("tool", "cheap", "corrected", cost=1.0, defect="failed-gate")
        book.task("tool", "cheap", "aborted", cost=0.5)
        rows = policy.scorecard(world(make_config(), book), env(), NOW, "tool")
        row = next(r for r in rows if r["actor"] == "cheap")
        self.assertEqual((row["tasks"], row["stood"], row["spend"], row["spend_per_stood"]), (3, 1, 3.0, 3.0))
        self.assertEqual({r["actor"] for r in rows}, {"cheap", "dear", LEAD})


if __name__ == "__main__":
    unittest.main()
