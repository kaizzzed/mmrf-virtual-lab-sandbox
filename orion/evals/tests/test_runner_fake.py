"""The scenarios and the runner end to end against a fake agent that answers with the frozen fixtures.

This runs today, without the engine: the harness asks, waits at the save pause, clicks Create in
the simulated browser, sends the read-back, follows the stream to the end, checks it and writes the
report. Every request the harness sends is checked against the contract (and against the frozen
`orion_agent.models` when the agent source is on the path).
"""

import json
import os
import re
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helpers import FixtureTransport, fixture_events, fixture_text
from test_fixture_checks import j1_expected, j3_expected

from orion_evals import report
from orion_evals.cli import load_prompts
from orion_evals.client import AgentClient, DirRecordSource
from orion_evals.evals import BY_ID
from orion_evals.evidence import AgentTarget, EvalContext
from orion_evals.oracle import Gateway, J1Expected, J3Expected, Oracle
from orion_evals.runner import environment, gates, run_evals
from orion_evals.sse import parse_text

HEX32 = re.compile(r"^[0-9a-f]{32}$")
PROMPTS = load_prompts()
REF = PROMPTS["reference"]


class FixtureOracle(Oracle):
    """The oracle's interface with the fixtures' own (illustrative) numbers."""

    def __init__(self) -> None:
        super().__init__(Gateway())

    def j1(self, *args: Any, **kwargs: Any) -> J1Expected:
        return j1_expected()

    def j3(self, *args: Any, **kwargs: Any) -> J3Expected:
        return j3_expected()

    def count(self, clauses: Any) -> int:
        return 1000

    def fingerprint(self) -> dict[str, Any]:
        return {"document": {}, "sha256": "0f27e0bdbb28a9357689a6137a8db7a9419dc59e5ec6a2a0e09b826bade7d71d"}


def declined_stream() -> str:
    """The consent run after `consent accept=false`: Cancelled, every step Skipped, no data call."""
    events = fixture_events("consent.events.jsonl")
    view = json.loads(json.dumps(events[-2]["snapshot"]))
    view.update({"status": "Cancelled", "code": None, "pending": None, "revision": view["revision"] + 1})
    for step in view["steps"]:
        step.update({"status": "Skipped", "outputSummary": "Not run: you declined the substitution"})
    frames = [events[0], {"type": "STATE_SNAPSHOT", "snapshot": view}, {"type": "RUN_FINISHED", "threadId": view["threadId"], "runId": view["runId"], "result": {"status": "Cancelled", "code": None}}]
    return "".join(f"data: {json.dumps(f, separators=(',', ':'))}\n\n" for f in frames)


def after_confirm(body: dict[str, Any]) -> str:
    """The agent echoes the browser's read-back: the fixture's cohort ids become the harness's."""
    orion = body["forwardedProps"]["orion"]
    if orion["decision"] == "dont_save":
        return "j3-dont-save.events.jsonl"
    text = fixture_text("j3-after-confirm.events.jsonl")
    fixture_ids = {"kras_wt": "5f2c9a7e-1b3d-4e6f-8a0c-2d4b6e8f0a13", "kras_mut": "9a1e3c5b-7d2f-4a6e-b8c0-4e6a8c0e2b57"}
    for saved in orion["saved"]:
        text = text.replace(fixture_ids[saved["ref"]], saved["cohortId"])
    return text


def routes() -> dict[str, Any]:
    return {
        f"ask:{REF['J1']['open-ended']}": "j1-complete.events.jsonl",
        f"ask:{REF['J1']['tool-named']}": "j1-complete.events.jsonl",
        f"ask:{REF['J3']['open-ended']}": "j3-save-pause.events.jsonl",
        f"ask:{REF['J3']['tool-named']}": "j3-save-pause.events.jsonl",
        f"ask:{PROMPTS['edge']['E7']}": "consent.events.jsonl",
        f"ask:{PROMPTS['edge']['E10']}": "clarify.events.jsonl",
        **{f"ask:{q}": "clarify.events.jsonl" for q in PROMPTS["edgeAlternates"]["E10"]},
        f"ask:{PROMPTS['edge']['E18']}": "interrupted.events.jsonl",
        "confirm": after_confirm,
        "consent": lambda body: declined_stream() if not body["forwardedProps"]["orion"]["accept"] else "j1-complete.events.jsonl",
        "clarify_answer": "j1-complete.events.jsonl",
    }


class RefusingTransport:
    def post(self, body: Any) -> Any:
        raise ConnectionRefusedError("connection refused")


def contract_problems(body: dict[str, Any]) -> list[str]:
    problems = []
    if set(body) != {"threadId", "runId", "state", "messages", "tools", "context", "forwardedProps"}:
        problems.append(f"fields {sorted(body)}")
    if not HEX32.match(body["threadId"]) or not HEX32.match(body["runId"]):
        problems.append("ids")
    orion = body["forwardedProps"]["orion"]
    if (orion["action"] == "ask") != bool(body["messages"]):
        problems.append("messages carry the question for ask only")
    return problems


class Runner(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "runs-volume"
        self.transport = FixtureTransport(routes(), records={f"ask:{REF['J1']['open-ended']}": "run-record.j1-complete.json", f"ask:{REF['J1']['tool-named']}": "run-record.j1-complete.json"}, run_dir=self.run_dir)
        agent = AgentTarget("fake agent", AgentClient(self.transport), DirRecordSource(self.run_dir))
        self.frontend = Path(self.tmp.name) / "fork"
        card_test = self.frontend / "src/features/orion/cards/ConfirmationCard.unit.test.tsx"
        card_test.parent.mkdir(parents=True)
        card_test.write_text("it('leaves focus in a composer holding text, announces itself, and Enter never saves', ...)")
        self.ctx = EvalContext(agent=agent, fault_agents={}, oracle=FixtureOracle(), repeats=2, prompts=PROMPTS, frontend_root=self.frontend, planner="stub")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_ids(self, *ids: str) -> dict[str, Any]:
        results, evidence = run_evals([BY_ID[i] for i in ids], self.ctx)
        self.evidence = evidence
        self.results = results
        return {r.id: r for r in results}

    def test_j1_pair(self) -> None:
        results = self.run_ids("E1", "E2")
        for eid in ("E1", "E2"):
            failed = [f"{c.name}: {c.detail}" for c in results[eid].checks if c.gate and c.status == "fail"]
            self.assertEqual(failed, [], eid)
            # "the viewer mounts" is browser-only: with no frontend test for it in this fake fork, not covered.
            self.assertEqual(results[eid].status, "not-run")
            self.assertEqual(next(c for c in results[eid].checks if c.name == "the viewer mounts in the card").status, "not-covered")
        extras = {c.name: c.status for c in results["E1"].checks if not c.gate}
        self.assertEqual(extras["each fact traces to its call"], "pass")
        self.assertEqual(extras["every counted call is a visible step"], "pass")
        self.assertTrue(all(r["recordAvailable"] for r in results["E1"].to_json()["runs"]))
        # Both phrasings of each run were asked twice (repeats = 2), and only once each across E1 and E2.
        asks = [b for b in self.transport.bodies if b["forwardedProps"]["orion"]["action"] == "ask"]
        self.assertEqual(len(asks), 4)
        self.assertEqual([contract_problems(b) for b in self.transport.bodies], [[] for _ in self.transport.bodies])

    def test_j3_with_create_in_the_simulated_browser(self) -> None:
        results = self.run_ids("E5")
        failed = [f"{c.name}: {c.detail}" for c in results["E5"].checks if c.gate and c.status == "fail"]
        self.assertEqual(failed, [])
        self.assertEqual(results["E5"].status, "pass")
        confirms = [b["forwardedProps"]["orion"] for b in self.transport.bodies if b["forwardedProps"]["orion"]["action"] == "confirm"]
        self.assertEqual(len(confirms), 4)
        pending = json.loads("".join(e["delta"] for e in fixture_events("j3-save-pause.events.jsonl") if e["type"] == "TOOL_CALL_ARGS"))
        for confirm in confirms:
            self.assertEqual(confirm["nonce"], pending["nonce"])
            self.assertEqual(confirm["decision"], "create")
            self.assertEqual([s["ref"] for s in confirm["saved"]], [c["ref"] for c in pending["cohorts"]])
            self.assertEqual([s["defSha256"] for s in confirm["saved"]], [c["defSha256"] for c in pending["cohorts"]])
            self.assertIsNone(confirm["makeActive"])

    def test_edges_browser_and_planner_gated_evals(self) -> None:
        results = self.run_ids("E7", "E10", "E18", "E21", "E22")
        self.assertEqual([c.name for c in results["E7"].checks if c.gate and c.status != "pass"], [])
        self.assertEqual(results["E7"].status, "pass")
        self.assertEqual(results["E10"].status, "pass")
        # The design's own wording ("High-risk patients") is asked too, with the same checks.
        alternate = [c for c in results["E10"].checks if c.name.startswith('"High-risk patients": ')]
        self.assertTrue(alternate and all(c.status == "pass" for c in alternate if c.gate), [(c.name, c.status) for c in alternate])
        e18 = {c.name: c.status for c in results["E18"].checks}
        self.assertEqual(e18["names SetOperations as the nearest app"], "pass")
        self.assertEqual(results["E21"].status, "pass")
        self.assertEqual(results["E21"].checks[0].detail, "covered by frontend test src/features/orion/cards/ConfirmationCard.unit.test.tsx")
        self.assertEqual(results["E22"].status, "not-run")
        self.assertTrue(all("needs Bedrock planner" in c.detail for c in results["E22"].checks))

    def test_e19_a_lost_confirm_creates_nothing_twice(self) -> None:
        self.ctx.settle_timeout_s = 0.5
        results = self.run_ids("E19")
        checks = {c.name: c for c in results["E19"].checks}
        for variant in ("confirm never sent", "confirm sent, response lost"):
            self.assertEqual(checks[f"{variant}: no duplicate cohort"].status, "pass", checks[f"{variant}: no duplicate cohort"].detail)
            self.assertEqual(checks[f"{variant}: the second Create creates nothing"].status, "pass")
            self.assertEqual(checks[f"{variant}: run ends Complete/ObservedOnly"].status, "pass")
            # This fake agent writes no J3 record, so the record half cannot run.
            self.assertEqual(checks[f"{variant}: the run finishes in its record"].status, "not-run")
        confirms = [b for b in self.transport.bodies if b["forwardedProps"]["orion"]["action"] == "confirm"]
        self.assertEqual(len(confirms), 3)  # variant A sends one confirm, variant B sends it twice
        self.assertEqual(confirms[1]["forwardedProps"]["orion"], confirms[2]["forwardedProps"]["orion"])

    def test_e24_declined_and_dont_save(self) -> None:
        results = self.run_ids("E24")
        statuses = {c.name: c.status for c in results["E24"].checks}
        for name in ("declined substitution: run ends Cancelled with code None", "declined substitution: no data call", "Don't save: the save step is Skipped WRITE_SKIPPED", "Don't save: nothing written"):
            self.assertEqual(statuses[name], "pass", name)

    def test_an_unreachable_agent_is_not_run_and_the_report_is_written(self) -> None:
        self.ctx.agent = AgentTarget("nowhere", AgentClient(RefusingTransport()), DirRecordSource(self.run_dir))
        results = self.run_ids("E8", "E17", "E21")
        self.assertEqual(results["E8"].status, "not-run")
        self.assertIn("agent unavailable", results["E8"].checks[0].detail)
        self.assertEqual(results["E21"].status, "pass")
        doc = report.document(self.results, gates(self.results, self.evidence), environment(self.ctx, self.evidence, None, "fake"), None, datetime.now(UTC), datetime.now(UTC))
        json_path, md_path = report.write(doc, Path(self.tmp.name) / "reports")
        written = json.loads(json_path.read_text())
        self.assertEqual(written["schema"], "orion.eval-report/v1")
        self.assertIn("simulated browser", written["environment"]["cohortStore"])
        text = md_path.read_text()
        self.assertIn("## E21: Enter pressed in the composer", text)
        self.assertIn("**simulated browser**", text)
        self.assertEqual([g["gate"] for g in written["gates"]][:4], ["Reference prompts completed", "Phrasing parity", "Interpretation present", "Honest limits"])

    def test_requests_validate_against_the_frozen_agent_models(self) -> None:
        agent_src = os.environ.get("ORION_AGENT_SRC")
        if not agent_src or not Path(agent_src, "orion_agent", "models.py").is_file():
            self.skipTest("set ORION_AGENT_SRC to the agent source to validate against orion_agent.models")
        sys.path.insert(0, agent_src)
        try:
            from orion_agent.models import RunAgentInputModel  # type: ignore[import-not-found,unused-ignore]
        except ImportError as error:
            self.skipTest(f"orion_agent.models does not import today: {error}")
        self.run_ids("E5", "E7", "E10", "E24")
        actions = set()
        for body in self.transport.bodies:
            model = RunAgentInputModel.model_validate(body)
            actions.add(model.orion().action)
        self.assertTrue({"ask", "confirm", "consent", "clarify_answer", "edit_plan"} <= actions, actions)


class Fixtures(unittest.TestCase):
    def test_rewriting_ids_keeps_every_frame_parsable(self) -> None:
        from helpers import rewrite_ids

        text = rewrite_ids(fixture_text("j3-save-pause.events.jsonl"), "a" * 32, "b" * 32)
        events = parse_text(text)
        self.assertEqual(events[0]["runId"], "b" * 32)
        self.assertTrue(all(e["snapshot"]["runId"] == "b" * 32 for e in events if e["type"] == "STATE_SNAPSHOT"))


if __name__ == "__main__":
    unittest.main()
