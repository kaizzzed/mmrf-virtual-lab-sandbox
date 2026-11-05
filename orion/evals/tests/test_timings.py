"""Stage 2 measurements (orion_evals.timings): NFR-01 timings through a fake agent, and a report's model usage."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from helpers import FixtureTransport, fixture_json, fixture_turn

from orion_evals import timings
from orion_evals.client import AgentClient, DirRecordSource, TurnResult
from orion_evals.runner import _p95

J1 = "Compare survival of CGS high-risk vs. CGS standard-risk patients"
WARMED = (
    'data: {"type":"RUN_STARTED","threadId":"warmthread000000000000000000000000","runId":"warmrun0000000000000000000000000"}\n\n'
    'data: {"type":"RUN_FINISHED","threadId":"warmthread000000000000000000000000","runId":"warmrun0000000000000000000000000",'
    '"result":{"status":"Warmed","code":null}}\n\n'
)


def snapshot_turn(run_id: str, answers: list[tuple[float, dict[str, Any]]]) -> TurnResult:
    turn = TurnResult("ask", {}, "t", run_id, time.time())
    for at, answer in answers:
        turn.events.append({"type": "STATE_SNAPSHOT", "snapshot": {"runId": run_id, **answer}})
        turn.event_times.append(at)
    return turn


class TimingsTest(unittest.TestCase):
    def test_p95_is_the_gate_s(self) -> None:
        for values in ([4.0], [1.0, 2.0, 3.0], [float(v) for v in range(1, 95)], [5.5, 3.1, 4.2, 2.9, 6.0] * 5):
            self.assertEqual(timings.p95(values), _p95(values))
        self.assertIsNone(timings.p95([]))
        self.assertEqual(timings.stats([]), {"n": 0})

    def test_the_interpretation_wait_runs_from_observed_to_interpretation(self) -> None:
        run = "r" * 32
        turn = snapshot_turn(run, [(1.0, {}), (2.5, {"observed": [{"segments": []}]}), (3.0, {"observed": [{}]}),
                                   (12.0, {"observed": [{}], "interpretation": [{}]})])
        self.assertEqual(timings.interpretation_wait([turn], run), 9.5)
        withheld = snapshot_turn(run, [(2.0, {"observed": [{}]}), (4.0, {"observed": [{}], "withheldReason": "x"})])
        self.assertEqual(timings.interpretation_wait([withheld], run), 2.0)
        self.assertIsNone(timings.interpretation_wait([snapshot_turn(run, [(1.0, {})])], run))
        # Another run's snapshots do not count.
        self.assertIsNone(timings.interpretation_wait([snapshot_turn("o" * 32, [(1.0, {"observed": [{}]})])], run))

    def test_a_warm_run_asks_after_one_warm_up_and_reads_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = FixtureTransport({"ask": "j1-complete.events.jsonl", "warmup": WARMED},
                                         records={f"ask:{J1}": "run-record.j1-complete.json"}, run_dir=Path(tmp))
            logs: list[str] = []
            rows = timings.run_timings(AgentClient(transport), DirRecordSource(Path(tmp)), [("J1", J1)], mode="warm", rounds=2,
                                       idle_s=999.0, log=logs.append, sleep=lambda s: self.fail("a warm run never idles"))
            self.assertEqual([b["forwardedProps"]["orion"]["action"] for b in transport.bodies], ["warmup", "ask", "ask"])
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["status"], "Complete")
                self.assertIsNotNone(row["planS"])
                # The fixture's Answering view shows Observed without Interpretation; Complete follows at once here.
                self.assertIsNotNone(row["interpretationWaitS"])
                self.assertGreaterEqual(row["interpretationWaitS"], 0.0)
                self.assertIsNone(row["warmup"])
                recorded = fixture_json("run-record.j1-complete.json")["modelCalls"]
                self.assertEqual([{**c, "callId": None} for c in row["modelCalls"]], [{**timings._call(c), "callId": None} for c in recorded])
                self.assertTrue(all(str(c["callId"]).startswith(row["runId"]) for c in row["modelCalls"]))
            summary = timings.summarise(rows)
            self.assertEqual((summary["asks"], summary["statuses"], summary["planS"]["n"]), (2, {"Complete": 2}, 2))
            self.assertEqual(len(logs), 2)

    def test_a_cold_warm_up_run_idles_then_warms_each_new_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = FixtureTransport({"ask": "j1-complete.events.jsonl", "warmup": WARMED}, run_dir=Path(tmp))
            slept: list[float] = []
            rows = timings.run_timings(AgentClient(transport), DirRecordSource(Path(tmp)), [("J1", J1)], mode="cold-warmup", rounds=2,
                                       idle_s=330.0, log=lambda _m: None, sleep=slept.append, record_wait_s=0.0)
            self.assertEqual(slept, [330.0, 330.0])
            self.assertEqual([b["forwardedProps"]["orion"]["action"] for b in transport.bodies], ["warmup", "ask"] * 2)
            self.assertEqual({b["threadId"] for b in transport.bodies[:2]}, {transport.bodies[0]["threadId"]})
            self.assertNotEqual(transport.bodies[0]["threadId"], transport.bodies[2]["threadId"])
            self.assertEqual([r["warmup"]["status"] for r in rows], ["Warmed", "Warmed"])
            with self.assertRaises(ValueError):
                timings.run_timings(AgentClient(transport), DirRecordSource(Path(tmp)), [], mode="hot", rounds=1, idle_s=0, log=print)

    def test_usage_sums_each_run_once_from_its_record(self) -> None:
        def call(run: str, role: str, n: int, elapsed: int, output: int, write: int = 0, model: str = "us.anthropic.claude-opus-5-5") -> dict[str, Any]:
            return {"callId": f"{run}.{role}.c{n}" if role == "planner" else f"{run}.v1.{role}.c{n}", "role": role, "model": model,
                    "fallback": model.endswith("opus-5"), "effort": "low" if role == "planner" else "medium", "attempt": 1,
                    "elapsedMs": elapsed, "outcome": "ok", "inputTokens": 100, "outputTokens": output, "cacheReadTokens": 12000 - write,
                    "cacheWriteTokens": write}

        a, b, c = "a" * 32, "b" * 32, "c" * 32
        records = {
            a: [call(a, "planner", 1, 3000, 200, write=12000), call(a, "composer", 1, 9000, 700)],
            b: [call(b, "planner", 1, 4000, 250), call(b, "composer", 1, 20000, 1700), call(b, "composer", 2, 18000, 1600)],
        }
        report = {"startedAt": "2026-11-05T00:00:00+00:00", "evals": [
            {"id": "E1", "runs": [{"runId": a, "turns": [{"action": "ask", "runId": a, "firstPlanS": 3.1}]},
                                  {"runId": b, "turns": [{"action": "ask", "runId": b, "firstPlanS": 4.2}, {"action": "confirm", "runId": b}]}]},
            {"id": "E16", "runs": [{"runId": a, "turns": [{"action": "ask", "runId": a, "firstPlanS": 3.1}]}]},
            {"id": "E11", "runs": [{"runId": c, "turns": []}]},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "runs").mkdir()
            for run_id, calls in records.items():
                (Path(tmp) / "runs" / f"{run_id}.json").write_text(json.dumps({"runId": run_id, "modelCalls": calls}))
            used = timings.usage(report, Path(tmp))
        self.assertEqual((used["runs"], used["runsWithoutRecord"], used["runsWithModelCalls"], used["modelCalls"]), (3, 1, 2, 5))
        self.assertEqual((used["planner"]["calls"], used["planner"]["laterCalls"], used["planner"]["cacheWriteCalls"]), (2, 0, 1))
        self.assertEqual((used["composer"]["calls"], used["composer"]["laterCalls"]), (3, 1))
        self.assertEqual(used["composer"]["laterCallElapsedS"], {"n": 1, "p50": 18.0, "p95": 18.0, "max": 18.0})
        self.assertEqual(used["tokens"]["outputTokens"], 200 + 700 + 250 + 1700 + 1600)
        self.assertEqual(used["planVisibleS"]["n"], 2)
        self.assertEqual(used["planner"]["efforts"], ["low"])


if __name__ == "__main__":
    unittest.main()
