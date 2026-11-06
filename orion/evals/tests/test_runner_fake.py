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

from helpers import FixtureTransport, fixture_events, fixture_evidence, fixture_text
from test_fixture_checks import j1_expected, j3_expected

from orion_evals import report
from orion_evals.checks import Check
from orion_evals.cli import load_prompts
from orion_evals.client import AgentClient, DirRecordSource
from orion_evals.evals import BY_ID
from orion_evals.evidence import AgentTarget, EvalContext, EvalResult, EvalStatus
from orion_evals.oracle import Gateway, J1Expected, J3Expected, Oracle
from orion_evals.runner import environment, gates, run_evals
from orion_evals.sse import parse_text

HEX32 = re.compile(r"^[0-9a-f]{32}$")
WARMED = (
    'data: {"type":"RUN_STARTED","threadId":"warmthread0000000000000000000000","runId":"warmrun0000000000000000000000000"}\n\n'
    'data: {"type":"RUN_FINISHED","threadId":"warmthread0000000000000000000000","runId":"warmrun0000000000000000000000000",'
    '"result":{"status":"Warmed","code":null}}\n\n'
)
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


class SettledViews(DirRecordSource):
    """The view route of a run that went on after the harness dropped its stream, and ended Complete."""

    def view(self, run_id: str, thread_id: str | None = None) -> dict[str, Any] | None:
        return {"runId": run_id, "threadId": thread_id, "status": "Complete", "revision": 10**6}


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

    def test_threads_are_not_warmed_unless_asked(self) -> None:
        self.run_ids("E1")
        self.assertNotIn("warmup", [b["forwardedProps"]["orion"]["action"] for b in self.transport.bodies])
        plan = next(g for g in gates(self.results, self.evidence, self.ctx.repeats) if g["gate"].startswith("Plan visible"))
        self.assertTrue(plan["measured"].endswith("; no thread warm-up"), plan["measured"])
        self.assertFalse(environment(self.ctx, self.evidence, None, "fake")["threadWarmup"])

    def test_warmup_threads_warms_each_thread_before_its_first_turn_and_times_only_asks(self) -> None:
        self.transport.routes["warmup"] = WARMED
        self.ctx.warmup_threads = True
        results = self.run_ids("E1", "E2", "E5")
        self.assertEqual(results["E5"].status, "pass")
        by_thread: dict[str, list[str]] = {}
        for body in self.transport.bodies:
            by_thread.setdefault(body["threadId"], []).append(body["forwardedProps"]["orion"]["action"])
        # One warm-up per thread, first, on its own run id; the turns after it are what they were without it.
        self.assertEqual(len(by_thread), 8)  # J1 and J3, two phrasings x 2 repeats each (E2 reuses E1's runs)
        for actions in by_thread.values():
            self.assertEqual(actions[0], "warmup")
            self.assertNotIn("warmup", actions[1:])
        warm_runs = {b["runId"] for b in self.transport.bodies if b["forwardedProps"]["orion"]["action"] == "warmup"}
        self.assertFalse(warm_runs & {ev.run_id for ev in self.evidence})
        # The warm-up is in the thread's turns but never in a run's: the plan gate times asks only, with the
        # same target, and says the threads were warmed.
        self.assertTrue(all(any(t.action == "warmup" for t in ev.turns) for ev in self.evidence))
        self.assertNotIn("warmup", {t.action for ev in self.evidence for t in ev.run_turns})
        plan = next(g for g in gates(self.results, self.evidence, self.ctx.repeats) if g["gate"].startswith("Plan visible"))
        # Review F3: the harness waited for each warm-up to end before it asked, which the browser does not do
        # for an ask sent while the warm-up is in flight, so the gate says what was measured and claims no parity.
        self.assertIn("asks; each thread's warm-up completed before its first ask", plan["measured"])
        self.assertNotIn("browser", plan["measured"])
        # A warmed-only sample has no cold first ask, so it can never pass NFR-01 (warm and cold runs).
        self.assertNotEqual(plan["status"], "pass")
        self.assertEqual(plan["target"], "<= 5 s over at least 20 runs")
        self.assertTrue(environment(self.ctx, self.evidence, None, "fake")["threadWarmup"])
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

    def cache_reference_runs(self, runs_per_prompt: int) -> None:
        """Reference runs already made (the memo E1-E6 fill), each a Complete J1 stream with its signature."""
        for journey in ("J1", "J2", "J3"):
            for phrasing in ("tool-named", "open-ended"):
                self.ctx.cache[f"runs:{journey}:{phrasing}"] = [
                    fixture_evidence(f"{journey} {phrasing} run {i + 1}", ("j1-complete.events.jsonl", "ask")) for i in range(runs_per_prompt)
                ]

    def test_e16_needs_five_repeats_to_pass(self) -> None:
        """Review F2: every prompt Complete in 2 of 2 runs is no pass of "in 5 of 5 runs per prompt"."""
        self.cache_reference_runs(2)
        results = self.run_ids("E16")
        self.assertEqual([c.status for c in results["E16"].checks if c.name != "5 of 5 runs per prompt"], ["pass"] * 6)
        self.assertEqual(results["E16"].status, "not-run")
        self.assertEqual(next(c for c in results["E16"].checks if c.name == "5 of 5 runs per prompt").detail, "needs 5 repeats, ran 2")
        by_gate = {g["gate"]: g for g in gates(self.results, self.evidence, self.ctx.repeats)}
        self.assertEqual(by_gate["Interpretation present"]["status"], "not-run")
        self.assertIn("needs 5 repeats, ran 2", by_gate["Interpretation present"]["measured"])
        # "6 of 6 reference prompts" is a scripted run end to end, not a 5-of-5 gate: it still reads the prompts.
        self.assertEqual((by_gate["Reference prompts completed"]["status"], by_gate["Reference prompts completed"]["measured"]), ("pass", "6 of 6 prompts Complete in every run"))
        self.ctx.cache.clear()
        self.ctx.repeats = 5
        self.cache_reference_runs(5)
        results = self.run_ids("E16")
        self.assertEqual(results["E16"].status, "pass")
        self.assertEqual(next(g for g in gates(self.results, self.evidence, self.ctx.repeats) if g["gate"] == "Interpretation present")["status"], "pass")
        self.assertEqual(self.transport.bodies, [], "the cached runs were used")

    def test_e22_needs_five_repeats_to_pass(self) -> None:
        """Review F2: each paraphrase on its journey's signature in 2 of 2 runs is no pass of "in 5 of 5 runs"."""
        self.cache_reference_runs(2)
        for questions in PROMPTS["heldOut"].values():
            for question in questions:
                self.transport.routes[f"ask:{question}"] = "j1-complete.events.jsonl"
        self.ctx.planner = "bedrock"
        results = self.run_ids("E22")
        paraphrases = [c for c in results["E22"].checks if c.name != "5 of 5 runs per prompt"]
        self.assertEqual([(c.status, c.detail) for c in paraphrases], [("pass", "2 of 2 runs")] * 9)
        self.assertEqual(results["E22"].status, "not-run")
        self.assertEqual(next(c for c in results["E22"].checks if c.name == "5 of 5 runs per prompt").detail, "needs 5 repeats, ran 2")
        self.assertEqual(len([b for b in self.transport.bodies if b["forwardedProps"]["orion"]["action"] == "ask"]), 18)

    def test_e20_without_a_restart_command_leaves_the_lease_case_not_run(self) -> None:
        """Review F1: with no --restart-cmd nothing killed the controller, so the lease case is not run, with the
        reason, and E20 is not run although its closed-tab and save-pause parts pass. An agent test that names
        RUN_INTERRUPTED is no live run (the old fallback reported it "covered", and E20 passed)."""
        assert self.ctx.agent is not None
        self.ctx.agent = AgentTarget("fake agent", self.ctx.agent.client, SettledViews(self.run_dir))
        (self.frontend / "src/features/orion/AssistantApp.unit.test.tsx").write_text("it('restores the thread from the view route', ...)")
        agent_tests = Path(self.tmp.name) / "agent-tests"
        agent_tests.mkdir()
        (agent_tests / "test_contract.py").write_text("def test_run_finished_carries_interrupted(self) -> None:\n    frame = 'RUN_INTERRUPTED'\n")
        (agent_tests / "test_controller.py").write_text("async def test_expired_lease_is_marked_interrupted_by_the_next_turn(self) -> None:\n    ...\n")
        self.ctx.agent_tests_root = agent_tests
        self.ctx.pause_wait_s = 0
        self.ctx.settle_timeout_s = 5
        self.assertIsNone(self.ctx.restart_cmd)
        results = self.run_ids("E20")
        checks = {c.name: c for c in results["E20"].checks}
        lease = checks["lease expiry ends Interrupted"]
        self.assertEqual((lease.status, lease.gate, lease.source), ("not-run", True, "harness"))
        self.assertIn("no --restart-cmd", lease.detail)
        live = [c for c in results["E20"].checks if c.gate and c.name != lease.name]
        self.assertEqual([(c.name, c.status) for c in live if not c.ok], [])
        self.assertEqual(results["E20"].status, "not-run")
        unit = checks["lease expiry: the agent's unit test of the rule (not a live run)"]
        self.assertEqual((unit.status, unit.gate, unit.evidence.get("file")), ("covered", False, "test_controller.py"))

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
        doc = report.document(self.results, gates(self.results, self.evidence, self.ctx.repeats), environment(self.ctx, self.evidence, None, "fake"), None, datetime.now(UTC), datetime.now(UTC))
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


class FiveOfFiveGates(unittest.TestCase):
    """Review F2: the gates the design scores "in 5 of 5 runs" never pass from fewer repeats."""

    def results(self, e16_status: EvalStatus = "pass") -> list[EvalResult]:
        def result(eid: str, status: EvalStatus = "pass", checks: list[Check] | None = None) -> EvalResult:
            return EvalResult(eid, eid, "", [], status, checks or [Check(eid, "pass", "")], [], None, [], False, "reference")

        prompts = [Check(f"{j} {p}: ends Complete with all four parts", "pass" if e16_status == "pass" else "fail", "") for j in ("J1", "J2", "J3") for p in ("tool-named", "open-ended")]
        return [*(result(f"E{i}") for i in range(1, 7)), result("E16", e16_status, prompts), result("E22")]

    def by_gate(self, results: list[EvalResult], repeats: int) -> dict[str, dict[str, Any]]:
        return {g["gate"]: g for g in gates(results, [], repeats)}

    def test_below_five_repeats_a_pass_is_not_run_with_the_count(self) -> None:
        for repeats in (1, 4):
            found = self.by_gate(self.results(), repeats)
            for name in ("Phrasing parity", "Interpretation present"):
                self.assertEqual(found[name]["status"], "not-run", (name, repeats))
                self.assertTrue(found[name]["measured"].endswith(f"; needs 5 repeats, ran {repeats}"), found[name]["measured"])
            self.assertEqual(found["Reference prompts completed"]["status"], "pass")
        found = self.by_gate(self.results(), 5)
        self.assertEqual((found["Phrasing parity"]["status"], found["Phrasing parity"]["measured"]), ("pass", "E1-E6 signature checks and E22"))
        self.assertEqual((found["Interpretation present"]["status"], found["Interpretation present"]["measured"]), ("pass", "E16"))

    def test_a_failure_at_fewer_repeats_still_fails(self) -> None:
        found = self.by_gate(self.results("fail"), 1)
        self.assertEqual(found["Interpretation present"]["status"], "fail")
        self.assertEqual(found["Reference prompts completed"]["status"], "fail")


class Fixtures(unittest.TestCase):
    def test_rewriting_ids_keeps_every_frame_parsable(self) -> None:
        from helpers import rewrite_ids

        text = rewrite_ids(fixture_text("j3-save-pause.events.jsonl"), "a" * 32, "b" * 32)
        events = parse_text(text)
        self.assertEqual(events[0]["runId"], "b" * 32)
        self.assertTrue(all(e["snapshot"]["runId"] == "b" * 32 for e in events if e["type"] == "STATE_SNAPSHOT"))


if __name__ == "__main__":
    unittest.main()
