"""Trigger-tagged lead cost: which of the lead's turns a task is charged for."""

import json
import unittest
from datetime import UTC, datetime
from unittest import mock

from delegate import cli
from delegate.cli import charged_lead_cost
from delegate.ledger import fold
from tests.helpers import Book, make_config


def at(minute: int) -> datetime:
    return datetime(2026, 10, 30, 12, minute, tzinfo=UTC)


# Turns as clanker-analytics reports them: the time and text of each message that started a lead turn.
TURNS = [
    (at(0), "launch the next port"),  # turn 0: the task is opened here
    (at(10), "what does this map show?"),  # turn 1: the user's own question
    (at(20), "Job job-port-7 succeeded, exited 0"),  # turn 2: names the unit
    (at(30), "Job job-other succeeded"),  # turn 3: another task
]


class ChargedTurns(unittest.TestCase):
    def test_a_brief_turn_names_the_task_before_its_unit_exists(self):
        turns = [(at(0), "open it"), (at(5), "write the brief for tool-0003"), (at(20), "job-port-7 done")]
        costs = [(at(1), 1.0), (at(6), 2.0), (at(21), 4.0)]
        self.assertEqual(charged_lead_cost(turns, costs, ["tool-0003", "job-port-7"], at(0)), 7.0)

    def test_charges_the_opening_turn_and_the_turns_naming_its_runs(self):
        costs = [(at(1), 1.0), (at(12), 4.0), (at(21), 2.0), (at(31), 8.0)]
        self.assertEqual(charged_lead_cost(TURNS, costs, ["job-port-7"], at(0)), 3.0)

    def test_an_aop_run_id_in_the_message_charges_its_turn(self):
        turns = [(at(0), "start"), (at(5), "aop: run_id=abc123 finished")]
        self.assertEqual(charged_lead_cost(turns, [(at(1), 1.0), (at(6), 5.0)], ["abc123"], at(0)), 6.0)

    def test_a_reused_unit_name_charges_only_turns_from_the_task_s_opening_on(self):
        # w3sim ran three tasks on one shop port, each in a unit named m8-shopvision; the third, opened at 40,
        # is charged its own opening turn and the job message after it, and none of the earlier tasks' turns.
        turns = [
            (at(0), "open"),
            (at(10), "Job m8-shopvision exited"),
            (at(40), "open again"),
            (at(50), "Job m8-shopvision exited"),
        ]
        costs = [(at(1), 1.0), (at(11), 2.0), (at(41), 4.0), (at(51), 8.0)]
        self.assertEqual(charged_lead_cost(turns, costs, ["m8-shopvision"], at(40)), 12.0)

    def test_a_task_opened_during_a_user_turn_is_charged_that_turn_only(self):
        costs = [(at(12), 4.0), (at(31), 8.0)]
        self.assertEqual(charged_lead_cost(TURNS, costs, ["job-port-7"], at(15)), 4.0)

    def test_a_unit_name_does_not_match_a_longer_name_it_prefixes(self):
        turns = [(at(0), "start"), (at(5), "Job job-ab2 succeeded"), (at(9), "Job job-ab succeeded")]
        self.assertEqual(charged_lead_cost(turns, [(at(6), 5.0), (at(10), 2.0)], ["job-ab"], at(30)), 2.0)

    def test_tasks_opened_in_one_turn_split_it_without_double_charging(self):
        # One turn writes two briefs and opens two tasks, at 3 and 6 minutes, then does other work.
        turns = [(at(0), "brief two tasks"), (at(10), "next")]
        costs = [(at(1), 1.0), (at(2), 2.0), (at(4), 4.0), (at(5), 8.0), (at(7), 16.0)]
        first = charged_lead_cost(turns, costs, [], at(3), other_opens=[at(6)])
        second = charged_lead_cost(turns, costs, [], at(6), other_opens=[at(3)])
        self.assertEqual((first, second), (3.0, 28.0))  # the second also takes the rest of the turn, its launch

    def test_the_lead_s_own_task_takes_every_turn_while_it_is_alone_except_other_tasks_turns(self):
        # As in w3sim's port-0229: the lead worked its own task in turns that user messages and agent-job notices
        # for other jobs started, which charged it 3.32 of the 9.53 its session spent while it was open.
        turns = [
            (at(0), "do port-0229 yourself"),
            (at(10), "continue"),  # the lead's own work: charged
            (at(20), "Job job-port-0226 succeeded"),  # an executor task's notice: charged to that task
            (at(30), "Job build-7 succeeded"),  # the lead's own build: charged
        ]
        costs = [(at(1), 1.0), (at(11), 2.0), (at(21), 4.0), (at(31), 8.0)]
        own = [(at(0), None)]
        self.assertEqual(
            charged_lead_cost(turns, costs, ["port-0229"], at(0), own=own, other_names=["job-port-0226"]), 11.0
        )
        # While another of the lead's own tasks is open, unnamed turns go to neither.
        self.assertEqual(charged_lead_cost(turns, costs, ["port-0229"], at(0), own=[(at(0), at(5))]), 1.0)
        self.assertEqual(cli._alone(at(0), None, [(at(5), at(25))]), [(at(0), at(5)), (at(25), None)])

    def test_cost_before_the_first_turn_is_charged_to_none(self):
        self.assertEqual(charged_lead_cost(TURNS, [(at(0).replace(hour=11), 9.0)], ["job-port-7"], at(0)), 0.0)


class SessionActivity(unittest.TestCase):
    """clanker-analytics gives the turns and the priced responses; delegate decides which turns are the task's."""

    def task(self):
        book = Book()
        book.add(
            event="open", task="t", kind="tool", **{"class": "bounded"}, actor="cheap", reason="draw", resources={}
        )
        book.events[-1]["at"] = at(0).isoformat()
        book.add(event="started", task="t", units=["job-port-7"])
        book.events[-1]["at"] = at(5).isoformat()
        return fold(book.events).tasks["t"]

    def test_charges_the_task_s_turns_at_clanker_analytics_costs(self):
        config = make_config()
        config.lead_session = "0123abcd"
        costs = [(at(1), 1.0), (at(11), 4.0), (at(21), 2.0), (at(31), 8.0), (at(0).replace(day=1), 50.0)]
        asked = []

        def activity(session):
            asked.append(session)
            return {"turns": TURNS, "costs": costs}

        with mock.patch.dict(cli.ENV, {"session_activity": activity}):
            # The response from weeks earlier, as a fork would copy it in, is left out. The opening turn, before the
            # task started, is its brief.
            self.assertEqual(cli.lead_cost_from_session(config, self.task()), (3.0, 1.0))
            self.assertEqual(asked, ["0123abcd"])
        with mock.patch.dict(cli.ENV, {"session_activity": lambda session: None}):
            self.assertIsNone(cli.lead_cost_from_session(config, self.task()))

    def test_a_closed_task_s_cost_ends_at_its_close(self):
        # Recomputed later, a task is not charged a later task's turns that reuse its unit's name.
        config = make_config()
        config.lead_session = "0123abcd"
        turns = [*TURNS, (at(40), "Job job-port-7 succeeded")]
        costs = [(at(1), 1.0), (at(21), 2.0), (at(41), 8.0)]
        book = Book()
        book.add(
            event="open", task="t", kind="tool", **{"class": "bounded"}, actor="cheap", reason="draw", resources={}
        )
        book.events[-1]["at"] = at(0).isoformat()
        book.add(event="started", task="t", units=["job-port-7"])
        book.events[-1]["at"] = at(5).isoformat()
        book.add(event="close", task="t", outcome="held")
        book.events[-1]["at"] = at(25).isoformat()
        with mock.patch.dict(cli.ENV, {"session_activity": lambda session: {"turns": turns, "costs": costs}}):
            self.assertEqual(cli.lead_cost_from_session(config, fold(book.events).tasks["t"]), (3.0, 1.0))

    def test_an_older_lead_task_left_open_does_not_take_a_later_one_s_turns(self):
        # As w3sim's tool-0052, opened by the lead and never closed, held port-0262 to port-0269 at their brief.
        config = make_config()
        config.lead_session = "0123abcd"
        book = Book()
        for task, minute in (("stale", 0), ("own", 10)):
            book.add(
                event="open",
                task=task,
                kind="tool",
                **{"class": "bounded"},
                actor="lead",
                reason="assigned",
                resources={},
            )
            book.events[-1]["at"] = at(minute).isoformat()
        state = fold(book.events)
        turns = [(at(0), "open stale"), (at(9), "a long turn that opens the next task and works it")]
        costs = [
            (at(1), 1.0),
            (
                at(
                    9,
                ),
                2.0,
            ),
            (at(11), 4.0),
            (at(21), 8.0),
        ]
        with mock.patch.dict(cli.ENV, {"session_activity": lambda session: {"turns": turns, "costs": costs}}):
            total, _ = cli.lead_cost_from_session(config, state.tasks["own"], others=[state.tasks["stale"]])
        self.assertEqual(total, 12.0)  # from its open on, though the stale task opened first and is still open

    def test_reads_turns_from_sch_and_costs_from_clanker_analytics(self):
        costs = {
            "responses": [
                {"timestamp": "2026-10-30T12:21:00Z", "api_cost_usd": 2.0},
                {"timestamp": "2026-10-30T12:22:00Z", "api_cost_usd": None},
            ]
        }
        messages = "\n".join(
            json.dumps(m)
            for m in [
                {"time": "2026-10-30T08:20:00-04:00", "kind": "user", "text": "Job job-port-7 succeeded"},
                {"time": None, "kind": "user", "text": "<system-reminder>"},  # no time: starts no turn
            ]
        )

        def run(command, **kwargs):
            outputs = {"clanker-analytics": json.dumps(costs), "sch": messages + "\n"}
            return mock.Mock(returncode=0, stdout=outputs[command[0]])

        with mock.patch.object(cli.subprocess, "run", side_effect=run) as called:
            activity = cli.session_activity("019a-codex-session")
        self.assertEqual([c[0][0][0] for c in called.call_args_list], ["clanker-analytics", "sch"])
        self.assertEqual(activity, {"turns": [(at(20), "Job job-port-7 succeeded")], "costs": [(at(21), 2.0)]})
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(cli.session_activity("0123abcd"))

    def test_a_session_id_must_be_one(self):
        with self.assertRaises(cli.UsageError):
            cli.session_activity("x'; DROP")


if __name__ == "__main__":
    unittest.main()
