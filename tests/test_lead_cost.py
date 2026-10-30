"""Trigger-tagged lead cost: which of the lead's turns a task is charged for."""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from delegate import cli
from delegate.cli import charged_lead_cost, lead_turns
from delegate.ledger import fold
from tests.helpers import Book, make_config


def at(minute: int) -> datetime:
    return datetime(2026, 10, 30, 12, minute, tzinfo=UTC)


def user(minute: int, text, tool_result=False, meta=False) -> str:
    content = [{"type": "tool_result", "content": "x"}] if tool_result else text
    entry = {
        "type": "user",
        "timestamp": at(minute).isoformat().replace("+00:00", "Z"),
        "message": {"content": content},
    }
    if meta:
        entry["isMeta"] = True
    return json.dumps(entry)


TRANSCRIPT = [
    user(0, "launch the next port"),  # turn 0: the task is opened here
    json.dumps({"type": "assistant", "timestamp": at(1).isoformat()}),
    user(2, "", tool_result=True),  # a tool result is not a turn
    user(10, "what does this map show?"),  # turn 1: the user's own question
    user(20, [{"type": "text", "text": "Job job-port-7 succeeded, exited 0"}]),  # turn 2: names the unit
    user(25, "reminder", meta=True),  # meta lines are not turns
    user(30, "Job job-other succeeded"),  # turn 3: another task
]


class LeadTurns(unittest.TestCase):
    def test_turns_skip_tool_results_meta_and_assistant_lines(self):
        turns = lead_turns(TRANSCRIPT)
        self.assertEqual([start.minute for start, _ in turns], [0, 10, 20, 30])

    def test_charges_the_opening_turn_and_the_turns_naming_its_runs(self):
        turns = lead_turns(TRANSCRIPT)
        costs = [(at(1), 1.0), (at(12), 4.0), (at(21), 2.0), (at(31), 8.0)]
        self.assertEqual(charged_lead_cost(turns, costs, ["job-port-7"], at(0)), 3.0)

    def test_an_aop_run_id_in_the_message_charges_its_turn(self):
        turns = lead_turns([user(0, "start"), user(5, "aop: run_id=abc123 finished")])
        costs = [(at(1), 1.0), (at(6), 5.0)]
        self.assertEqual(charged_lead_cost(turns, costs, ["abc123"], at(30)), 5.0)

    def test_a_task_opened_during_a_user_turn_is_charged_that_turn_only(self):
        turns = lead_turns(TRANSCRIPT)
        costs = [(at(12), 4.0), (at(31), 8.0)]
        self.assertEqual(charged_lead_cost(turns, costs, ["job-port-7"], at(15)), 4.0)  # opened during turn 1

    def test_a_unit_name_does_not_match_a_longer_name_it_prefixes(self):
        turns = lead_turns([user(0, "start"), user(5, "Job job-ab2 succeeded"), user(9, "Job job-ab succeeded")])
        costs = [(at(6), 5.0), (at(10), 2.0)]
        self.assertEqual(charged_lead_cost(turns, costs, ["job-ab"], at(30)), 2.0)

    def test_cost_before_the_first_turn_is_charged_to_none(self):
        self.assertEqual(
            charged_lead_cost(lead_turns(TRANSCRIPT), [(at(0).replace(hour=11), 9.0)], ["job-port-7"], at(0)), 0.0
        )


class SessionCost(unittest.TestCase):
    """Turn costs come from clanker-analytics; delegate only decides which turns are the task's."""

    def test_charges_the_task_s_turns_at_clanker_analytics_costs(self):
        home = Path(tempfile.mkdtemp())
        (home / ".claude" / "projects" / "sim").mkdir(parents=True)
        (home / ".claude" / "projects" / "sim" / "0123abcd-aaaa.jsonl").write_text("\n".join(TRANSCRIPT))
        config = make_config()
        config.lead_session = "0123abcd"
        book = Book()
        book.add(
            event="open", task="t", kind="tool", **{"class": "bounded"}, actor="cheap", reason="draw", resources={}
        )
        book.events[-1]["at"] = at(0).isoformat()
        book.add(event="started", task="t", units=["job-port-7"])
        task = fold(book.events).tasks["t"]
        costs = [(at(1), 1.0), (at(11), 4.0), (at(21), 2.0), (at(31), 8.0)]
        asked = []

        def session_costs(session, since):
            asked.append(session)
            return costs

        with (
            mock.patch.object(cli.Path, "home", lambda: home),
            mock.patch.dict(cli.ENV, {"session_costs": session_costs}),
        ):
            self.assertEqual(cli.lead_cost_from_session(config, task), 3.0)
            self.assertEqual(asked, ["0123abcd"])
        with (
            mock.patch.object(cli.Path, "home", lambda: home),
            mock.patch.dict(cli.ENV, {"session_costs": lambda s, d: None}),
        ):
            self.assertIsNone(cli.lead_cost_from_session(config, task))

    def test_a_session_id_must_be_one(self):
        with self.assertRaises(cli.UsageError):
            cli.session_costs("x'; DROP", at(0))


if __name__ == "__main__":
    unittest.main()
