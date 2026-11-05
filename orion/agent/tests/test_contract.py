"""The shared wire contract: the reference fixture and the real AGUIApp's stream obey the same models."""

import json
import unittest
from typing import Any, get_args

from pydantic import JsonValue, ValidationError
from support import FIXTURE, J1_QUESTION, PINNED_VECTORS, ControllerCase, ask_body, asgi_call, check_stream, context, snapshots

from orion_agent.ids import new_id
from orion_agent.models import (
    CODES,
    PINNED,
    REJECTION_CODES,
    RUN_STATUSES,
    STEP_STATUSES,
    DeDirectPinned,
    HeartbeatFrame,
    Limitation,
    NearestApp,
    PinnedKind,
    PinnedNumberError,
    PlanView,
    RunFinishedFrame,
    SampleGroup,
    StateSnapshotFrame,
    StepView,
    SurvivalPinned,
    VizSpec,
    Volcano,
    canonical_pinned,
    iter_sse_data,
    parse_frame,
    pinned_hash,
)
from orion_agent.stubs import STUB_SKIP_REASON


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        if not FIXTURE.exists():
            self.skipTest(f"{FIXTURE} is not mounted")
        self.text = FIXTURE.read_text(encoding="utf-8")

    def test_every_fixture_frame_validates_against_the_models(self) -> None:
        frames = [parse_frame(data) for data in iter_sse_data(self.text)]
        self.assertEqual(len(frames), 22)
        for frame in frames:
            if isinstance(frame, StateSnapshotFrame):
                self.assertEqual(frame.snapshot.schema_, "orion.run-view/v1")

    def test_fixture_obeys_the_stream_invariants(self) -> None:
        frames = check_stream(self, self.text)
        self.assertTrue(any(isinstance(f, HeartbeatFrame) for f in frames))
        last = frames[-1]
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual((last.result.status, last.result.code), ("ObservedOnly", None))

    def test_models_round_trip_fixture_snapshots_unchanged(self) -> None:
        for data in iter_sse_data(self.text):
            frame = parse_frame(data)
            if isinstance(frame, StateSnapshotFrame):
                self.assertEqual(frame.snapshot.model_dump(mode="json"), json.loads(data)["snapshot"])

    def test_reader_ignores_other_sse_fields_and_accepts_crlf(self) -> None:
        noisy = ": comment\r\nevent: x\r\nid: 7\r\nretry: 10\r\n" + self.text.replace("\n", "\r\n")
        self.assertEqual(list(iter_sse_data(noisy)), list(iter_sse_data(self.text)))

    def test_contract_enumerations_are_complete(self) -> None:
        self.assertEqual(len(RUN_STATUSES), 15)
        self.assertEqual(len(STEP_STATUSES), 9)
        self.assertEqual(len(CODES), 22)
        self.assertEqual(REJECTION_CODES, {"INVALID_INPUT", "RUN_BUSY", "RUN_NOT_FOUND", "NOT_IMPLEMENTED"})

    def test_fixture_limitations_and_viz_specs_carry_the_amended_fields(self) -> None:
        view = StateSnapshotFrame.model_validate(self._final_frame()).snapshot
        self.assertEqual([lim.nearest_apps for lim in view.limitations], [[]])
        spec = view.viz_specs[0]
        self.assertEqual((spec.launcher, spec.pinned.kind), ("survival", "survival"))
        self.assertEqual(spec.pinned_hash, pinned_hash(spec.pinned))

    def test_viz_specs_must_match_their_pinned_inputs(self) -> None:
        spec = self._final_frame()["snapshot"]["vizSpecs"][0]
        changes: list[dict[str, JsonValue]] = [
            {"pinnedHash": "0" * 64},
            {"launcher": "oncomatrix"},
            {"pinned": {**spec["pinned"], "overlay": "iss_stage"}},
            {"pinned": {**spec["pinned"], "extra": 1}},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                VizSpec.model_validate({**spec, **change})

    def test_run_finished_carries_interrupted(self) -> None:
        frame = parse_frame(
            json.dumps({"type": "RUN_FINISHED", "threadId": new_id(), "runId": new_id(), "result": {"status": "Interrupted", "code": "RUN_INTERRUPTED"}})
        )
        assert isinstance(frame, RunFinishedFrame)
        self.assertEqual(frame.result.status, "Interrupted")

    def _final_frame(self) -> dict[str, Any]:  # Any: the test edits raw frame JSON.
        value: dict[str, Any] = json.loads(list(iter_sse_data(self.text))[-2])
        return value


class LimitationTests(unittest.TestCase):
    def test_nearest_apps_is_always_serialized(self) -> None:
        bare = Limitation(code="OUT_OF_PANEL", text="x", unblock=None)
        self.assertEqual(bare.model_dump(mode="json")["nearestApps"], [])
        named = Limitation(code="UNSUPPORTED_ANALYSIS", text="x", unblock=None, nearest_apps=[NearestApp.of("SetOperations")])
        self.assertEqual(named.model_dump(mode="json")["nearestApps"], [{"appId": "SetOperations", "title": "Set Operations"}])

    def test_nearest_apps_must_be_catalog_apps(self) -> None:
        with self.assertRaises(ValidationError):
            Limitation.model_validate({"code": "OUT_OF_SCOPE", "text": "x", "unblock": None, "nearestApps": [{"appId": "Excel", "title": "Excel"}]})


class PinnedHashTests(unittest.TestCase):
    """`fixtures/viz-pinned-vectors.json`: both sides reproduce every canonical string and sha256."""

    def setUp(self) -> None:
        if not PINNED_VECTORS.exists():
            self.skipTest(f"{PINNED_VECTORS} is not mounted")
        self.vectors: list[dict[str, Any]] = json.loads(PINNED_VECTORS.read_text(encoding="utf-8"))["vectors"]

    def test_every_vector_reproduces_its_canonical_json_and_sha256(self) -> None:
        self.assertEqual({v["pinned"]["kind"] for v in self.vectors}, set(get_args(PinnedKind)))
        for vector in self.vectors:
            with self.subTest(vector["name"]):
                pinned = PINNED.validate_json(json.dumps(vector["pinned"]))
                self.assertEqual(canonical_pinned(pinned), vector["canonical"])
                self.assertEqual(pinned_hash(pinned), vector["sha256"])
                self.assertEqual(pinned.model_dump(mode="json"), vector["pinned"], "the card's inputs are kept as given")

    def test_sample_order_is_normalized_but_group_order_is_not(self) -> None:
        def de(control: list[int], case: list[int]) -> DeDirectPinned:
            groups = [SampleGroup(name="control", sample_ids=control), SampleGroup(name="case", sample_ids=case)]
            return DeDirectPinned(kind="deDirect", groups=groups, method="wilcoxon")

        given = de([3, 1, 2], [9, 4])
        self.assertEqual(pinned_hash(given), pinned_hash(de([1, 2, 3], [4, 9])))
        self.assertEqual(given.groups[0].sample_ids, [3, 1, 2])
        self.assertNotEqual(pinned_hash(given), pinned_hash(de([9, 4], [3, 1, 2])))
        self.assertNotEqual(pinned_hash(given), pinned_hash(de([1, 2, 5], [4, 9])))

    def test_null_is_kept_and_an_absent_optional_is_omitted(self) -> None:
        de = DeDirectPinned(
            kind="deDirect", groups=[SampleGroup(name="a", sample_ids=[1]), SampleGroup(name="b", sample_ids=[2])], method="edgeR"
        )
        self.assertNotIn("volcano", canonical_pinned(de))
        self.assertNotIn("pValue", canonical_pinned(de.model_copy(update={"volcano": Volcano(rank_by="adjustedPValue")})))
        self.assertIn('"filter0":null', canonical_pinned(SurvivalPinned(kind="survival", filter0=None, term="t", overlay="o")))

    def test_an_optional_field_written_as_null_is_refused(self) -> None:
        # The browser would hash these nulls; a dump that dropped them would hash something else.
        de: dict[str, JsonValue] = {
            "kind": "deDirect", "groups": [{"name": "a", "sampleIds": [1]}, {"name": "b", "sampleIds": [2]}], "method": "edgeR"
        }
        nulls: list[dict[str, JsonValue]] = [
            {**de, "volcano": None},
            {**de, "volcano": {"pValue": None}},
            {**de, "volcano": {"foldChangeCutoff": 1.5, "rankBy": None}},
        ]
        for pinned in nulls:
            with self.subTest(pinned=pinned), self.assertRaises(ValidationError):
                PINNED.validate_python(pinned)
        absent_hash = pinned_hash(PINNED.validate_python(de))
        spec = {"vizId": "v1", "appId": "DE", "launcher": "deDirect", "citesSteps": ["s1"]}
        VizSpec.model_validate({**spec, "pinned": de, "pinnedHash": absent_hash})
        with self.assertRaises(ValidationError):
            VizSpec.model_validate({**spec, "pinned": {**de, "volcano": None}, "pinnedHash": absent_hash})

    def test_numbers_follow_the_contract_range(self) -> None:
        def pinned(value: float) -> SurvivalPinned:
            return SurvivalPinned(kind="survival", filter0={"and": [{">=": {"age": value}}]}, term="t", overlay="o")

        printed: list[tuple[float, str]] = [(65, "65"), (0, "0"), (2.0, "2"), (0.3, "0.3"), (0.0001, "0.0001"), (-1.5, "-1.5")]
        for value, text in printed:
            with self.subTest(value=value):
                self.assertIn(f'{{"age":{text}}}', canonical_pinned(pinned(value)))
        for bad in (0.00001, -0.00009, 1e15 + 0.5, 2.0**53 + 2, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(PinnedNumberError):
                pinned_hash(pinned(bad))
        with self.assertRaises(PinnedNumberError):
            pinned_hash(DeDirectPinned(
                kind="deDirect", groups=[SampleGroup(name="a", sample_ids=[2**53]), SampleGroup(name="b", sample_ids=[1])], method="wilcoxon"
            ))


class LiveAppStreamTests(ControllerCase):
    """The stub controller end to end through the real AGUIApp."""

    async def test_stub_ask_stream_obeys_contract_and_fixture_layout(self) -> None:
        _, app = self.make()
        thread_id, run_id = new_id(), new_id()
        response = await asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id))
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        frames = check_stream(self, response.text, expect_run_id=run_id)
        views = [f.snapshot for f in snapshots(frames)]
        self.assertEqual([v.status for v in views[:2]], ["Planning", "Executing"])
        self.assertIsNone(views[0].plan)
        plan = views[1].plan
        assert plan is not None
        fixture_plan = self._fixture_plan()
        self.assertEqual(plan.model_dump(exclude={"signature"}), fixture_plan.model_dump(exclude={"signature"}))
        self.assertEqual(
            [s.model_dump(include={"id", "row", "tool", "kind", "folded", "input_summary"}) for s in views[1].steps],
            [s.model_dump(include={"id", "row", "tool", "kind", "folded", "input_summary"}) for s in self._fixture_steps()],
        )
        final = views[-1]
        self.assertEqual((final.status, final.code), ("Limited", "NOT_IMPLEMENTED"))
        self.assertTrue(all(s.status == "Skipped" and s.code == "NOT_IMPLEMENTED" for s in final.steps))
        self.assertTrue(all(s.output_summary == STUB_SKIP_REASON for s in final.steps))
        self.assertEqual((final.facts, final.observed, final.viz_specs), ([], [], []))
        self.assertEqual([lim.code for lim in final.limitations], ["NOT_IMPLEMENTED"])
        last = frames[-1]
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual((last.result.status, last.result.code), ("Limited", "NOT_IMPLEMENTED"))

        record = self.read_json(self.run_dir / "runs" / f"{run_id}.json")
        self.assertEqual(record["schema"], "orion.run-record/v1")
        self.assertEqual((record["status"], record["revision"], record["lease"]), ("Limited", final.revision, None))
        self.assertEqual(record["question"], J1_QUESTION)
        index = self.read_json(self.run_dir / "threads" / f"{thread_id}.json")
        self.assertEqual((index["runIds"], index["activeRunId"]), ([run_id], None))

    async def test_filtered_active_cohort_adds_one_scope_step_before_the_counts(self) -> None:
        _, app = self.make()
        ctx = context(
            {"demographic.race": {"operator": "in", "field": "demographic.race", "operands": ["Black or African American"]}},
            name="Black patients",
            case_count=334,
        )
        response = await asgi_call(app, "POST", "/invocations", ask_body(new_id(), new_id(), cohort_context=ctx))
        frames = check_stream(self, response.text)
        plan = snapshots(frames)[1].snapshot.plan
        assert plan is not None
        self.assertEqual(plan.scope.model_dump(), {"source": "active_cohort", "name": "Black patients", "caseCount": 334})
        self.assertEqual([(r.row, r.tool, r.step_ids) for r in plan.rows][0], (1, "cases_query", ["s1", "s2"]))

    def _fixture_frames(self) -> list[StateSnapshotFrame]:
        if not FIXTURE.exists():
            self.skipTest(f"{FIXTURE} is not mounted")
        return [f for f in map(parse_frame, iter_sse_data(FIXTURE.read_text())) if isinstance(f, StateSnapshotFrame)]

    def _fixture_plan(self) -> PlanView:
        plan = self._fixture_frames()[1].snapshot.plan
        assert plan is not None
        return plan

    def _fixture_steps(self) -> list[StepView]:
        return self._fixture_frames()[1].snapshot.steps


if __name__ == "__main__":
    unittest.main()
