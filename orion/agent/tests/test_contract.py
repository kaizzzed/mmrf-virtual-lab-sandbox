"""The shared wire contract v1.2: every fixture and the real AGUIApp's stream obey the same models."""

import asyncio
import importlib.util
import json
import re
import sys
import unittest
from datetime import timedelta
from types import ModuleType
from typing import Any, get_args

from pydantic import JsonValue, TypeAdapter, ValidationError
from support import (
    CONTRACTS,
    FIXTURE,
    J1_QUESTION,
    PINNED_VECTORS,
    ControllerCase,
    action_body,
    ask_body,
    asgi_call,
    check_stream,
    context,
    snapshots,
)

from orion_agent import events, ir
from orion_agent.catalog import AnalysisKind
from orion_agent.events import iso_utc, utc_now
from orion_agent.ids import new_id
from orion_agent.lease import Lease
from orion_agent.models import (
    ACTION_STATUSES,
    CANONICAL_DEFINITION,
    CODES,
    FROZEN_ROWS_CAP,
    LEASED_STATUSES,
    RENEWED_STATUSES,
    STOP_LEASE_S,
    VIEW_RANK_CAP,
    ORION_ACTION,
    PINNED,
    REJECTION_CODES,
    RUN_STATUSES,
    SNAPSHOT_LIMIT_BYTES,
    STEP_STATUSES,
    SYNTHETIC_NOTICE,
    ActionName,
    AnswerRecord,
    AskAction,
    CallId,
    ClarifyPending,
    ConfirmAction,
    ConfirmPending,
    ConsentPending,
    DeDirectPinned,
    DeInputPinned,
    EditableTopN,
    EditCohortNameAction,
    FilterSet,
    FrozenTable,
    HeartbeatFrame,
    Limitation,
    NearestApp,
    PendingCohort,
    PinnedKind,
    PinnedNumberError,
    PlanView,
    ModelCallId,
    OncomatrixPinned,
    RunAgentInputModel,
    RunFinishedFrame,
    RunStartedFrame,
    RunStatus,
    RunView,
    SampleGroup,
    SaveCohortsArgs,
    ScopeChoice,
    Segment,
    Sentence,
    StateSnapshotFrame,
    StepFinishedFrame,
    StepStartedFrame,
    StepStatus,
    StepView,
    SurvivalPinned,
    TextMessageContentFrame,
    TextMessageEndFrame,
    TextMessageStartFrame,
    ToolCallArgsFrame,
    ToolCallEndFrame,
    ToolCallStartFrame,
    VizSpec,
    Volcano,
    WireEvent,
    canonical_definition,
    canonical_json,
    canonical_pinned,
    def_sha256,
    definition_from_filter_set,
    derive_request_pinned,
    fingerprint_document,
    fingerprint_sha256,
    format_fact,
    iter_sse_data,
    parse_frame,
    pinned_hash,
    sha256_hex,
)
from orion_agent import compiler as compiler_module
from orion_agent import executor as executor_module
from orion_agent import interfaces, models
from orion_agent.model.config import DEFAULT_MODELS
from orion_agent.run_store import Environment, RunRecord, RunStore, ThreadIndex
from orion_agent.settings import Settings, parse_faults
from orion_agent.stubs import STUB_SKIP_REASON, plan_question
from orion_agent.tools import schemas as catalog
from orion_agent.tools.base import STEP_TIMEOUT_S, KmCurve, RetryHook, ServerToolName, ToolCallOutcome, ToolErrorCode
from orion_agent.tools.functions import derive_km_median
from test_engine_fakes import FakeTools, compile_plan, fixture_frames, shape
from test_engine_streams import EngineCase

FIXTURES = CONTRACTS / "fixtures"
STREAMS: dict[str, tuple[str, str | None]] = {
    # fixture: (the status its RUN_FINISHED carries, its code)
    "j1-stub": ("ObservedOnly", None),
    "j1-complete": ("Complete", None),
    "j1-partial": ("Partial", "TOOL_FAILED"),
    "j1-retry": ("Complete", None),
    "j1-stop": ("Cancelled", None),
    "j2-save-pause": ("AwaitingConfirmation", None),
    "j2-after-confirm": ("Complete", None),
    "j2-dont-save": ("Complete", None),
    "j3-save-pause": ("AwaitingConfirmation", None),
    "j3-after-confirm": ("Complete", None),
    "j3-edit-at-pause": ("AwaitingConfirmation", None),
    "j3-dont-save": ("Complete", None),
    "j3-deinput": ("Complete", None),
    "oncomatrix": ("Complete", None),
    "clarify": ("Clarifying", None),
    "consent": ("AwaitingConsent", "NAMED_TOOL_MISMATCH"),
    "interrupted": ("Unsupported", "UNSUPPORTED_ANALYSIS"),
    "cancel-waiting": ("Clarifying", None),
}
# Resume streams: the action's snapshot follows RUN_STARTED directly.
RESUMES = ("j1-retry", "j1-stop", "j2-after-confirm", "j2-dont-save", "j3-after-confirm", "j3-edit-at-pause", "j3-dont-save", "j3-deinput")
RECORDS = ("run-record.j1-complete.json", "run-record.j1-retry.json")
# Words with digits that code-written text may carry outside fact segments: app and term names,
# the panel genes the PANEL_COVERAGE line names, and the viewer's rounded "p = 0" footnote.
DIGIT_ALLOW = re.compile(r"Gen3|log2FC|log2|six-gene|TP53|DIS3|p = 0|Day 1 stub")
NUMBER_WORDS = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|hundred|thousand|million|dozen|half|twice)\b", re.I
)


def read_stream(name: str) -> tuple[str, list[WireEvent]]:
    text = (FIXTURES / f"{name}.events.jsonl").read_text(encoding="utf-8")
    return text, [parse_frame(data) for data in iter_sse_data(text)]


def views(frames: list[WireEvent]) -> list[RunView]:
    return [f.snapshot for f in frames if isinstance(f, StateSnapshotFrame)]


def model_sentences(view: RunView) -> list[Sentence]:
    return [*(view.interpretation or []), *(view.hypotheses or [])]


def all_segments(view: RunView) -> list[Segment]:
    """Every segment on screen: answer sentences, limitation lines and frozen tables (cells and notes)."""
    out = [seg for sentence in [*view.observed, *model_sentences(view)] for seg in sentence.segments]
    out += [seg for item in view.limitations for seg in item.segments or []]
    for spec in view.viz_specs:
        if spec.frozen is not None:
            out += [cell for row in spec.frozen.rows for cell in row] + list(spec.frozen.note or [])
    return out


def cited_fact_ids(view: RunView) -> set[str]:
    ids = {s.fact_id for s in all_segments(view) if s.fact_id is not None}
    ids |= {fid for sentence in model_sentences(view) for fid in sentence.cites or []}
    ids |= {c.case_count_fact_id for c in view.cohorts if c.case_count_fact_id is not None}
    if isinstance(view.pending, ConfirmPending):
        for cohort in view.pending.cohorts:
            ids |= {fid for fid in (cohort.case_count_fact_id, cohort.eligible_count_fact_id) if fid is not None}
    return ids


def screen_strings(view: RunView) -> list[str]:
    """Code- and model-written text a number could hide in, outside fact segments (FR-16, NFR-03).

    Plan texts and step summaries are left out: their numbers are the question's or the plan's
    own settings, and a step row is itself the source its numbers cite.
    """
    out = [s.text for s in all_segments(view) if s.fact_id is None]
    out += [item.text for item in view.limitations if item.segments is None]
    out += [item.unblock for item in view.limitations if item.unblock]
    out += [view.withheld_reason or ""]
    for spec in view.viz_specs:
        out += [spec.title, spec.caption or "", *(spec.frozen.columns if spec.frozen else [])]
    for cohort in view.cohorts:
        out += [cohort.name, cohort.definition_text, cohort.saved_name or ""]
    if isinstance(view.pending, ConfirmPending):
        out += [text for c in view.pending.cohorts for text in (c.name, c.definition_text)]
    return out


def load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("orion_build_fixtures", FIXTURES / "build_fixtures.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module's annotations through sys.modules
    spec.loader.exec_module(module)
    return module


class FixtureTests(unittest.TestCase):
    """`j1-stub.events.jsonl`, the Day 1 reference stream the frontend reducer replays."""

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
        self.assertEqual(len(CODES), 33)
        self.assertEqual(REJECTION_CODES, {"INVALID_INPUT", "RUN_BUSY", "RUN_NOT_FOUND", "NOT_IMPLEMENTED", "STALE_ACTION"})
        resumable = set(get_args(ActionName)) - {"ask", "warmup"}
        self.assertEqual(set(ACTION_STATUSES), resumable)
        self.assertTrue(all(statuses <= RUN_STATUSES for statuses in ACTION_STATUSES.values()))

    def test_fixture_limitations_and_viz_specs_carry_the_amended_fields(self) -> None:
        view = StateSnapshotFrame.model_validate(self._final_frame()).snapshot
        self.assertEqual([lim.nearest_apps for lim in view.limitations], [[]])
        spec = view.viz_specs[0]
        assert spec.pinned is not None
        self.assertEqual((spec.launcher, spec.pinned.kind), ("survival", "survival"))
        self.assertEqual(spec.pinned_hash, pinned_hash(spec.pinned))
        self.assertEqual(spec.request_pinned_hash, spec.pinned_hash)
        self.assertEqual((view.pending, view.notice), (None, SYNTHETIC_NOTICE))

    def test_viz_specs_must_match_their_pinned_inputs(self) -> None:
        spec = self._final_frame()["snapshot"]["vizSpecs"][0]
        changes: list[dict[str, JsonValue]] = [
            {"pinnedHash": "0" * 64},
            {"launcher": "oncomatrix"},
            {"pinned": {**spec["pinned"], "overlay": "iss_stage"}},
            {"pinned": {**spec["pinned"], "extra": 1}},
            {"appId": "Excel"},
            {"citesSteps": []},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                VizSpec.model_validate({**spec, **change})
        # A request-side mismatch is a legitimate state the parity badge shows, not a malformed spec.
        VizSpec.model_validate({**spec, "requestPinnedHash": "0" * 64})

    def test_run_finished_carries_interrupted_and_recorded(self) -> None:
        for status, code in (("Interrupted", "RUN_INTERRUPTED"), ("Recorded", None)):
            frame = parse_frame(
                json.dumps({"type": "RUN_FINISHED", "threadId": new_id(), "runId": new_id(), "result": {"status": status, "code": code}})
            )
            assert isinstance(frame, RunFinishedFrame)
            self.assertEqual(frame.result.status, status)

    def _final_frame(self) -> dict[str, Any]:  # Any: the test edits raw frame JSON.
        value: dict[str, Any] = json.loads(list(iter_sse_data(self.text))[-2])
        return value


class StreamFixtureTests(unittest.TestCase):
    """Every `*.events.jsonl` fixture: validates, keeps the stream invariants, and cites only facts it carries."""

    def setUp(self) -> None:
        if not FIXTURES.exists():
            self.skipTest(f"{FIXTURES} is not mounted")

    def test_every_stream_keeps_the_invariants_and_ends_as_named(self) -> None:
        for name, (status, code) in STREAMS.items():
            with self.subTest(name):
                text, _ = read_stream(name)
                frames = check_stream(self, text)
                for data in iter_sse_data(text):
                    self.assertLessEqual(len(f"data: {data}\n\n".encode()), SNAPSHOT_LIMIT_BYTES)
                    frame = parse_frame(data)
                    if isinstance(frame, StateSnapshotFrame):
                        self.assertEqual(frame.snapshot.model_dump(mode="json"), json.loads(data)["snapshot"])
                last = frames[-1]
                assert isinstance(last, RunFinishedFrame)
                self.assertEqual((last.result.status, last.result.code), (status, code))
                self.assertEqual(views(frames)[-1].status, status)

    def test_every_cited_fact_and_step_exists(self) -> None:
        for name in STREAMS:
            _, frames = read_stream(name)
            for view in views(frames):
                with self.subTest(name, revision=view.revision):
                    self.assertLessEqual(cited_fact_ids(view), {f.id for f in view.facts})
                    step_ids = {s.id for s in view.steps}
                    if view.plan is not None:
                        self.assertEqual({sid for row in view.plan.rows for sid in row.step_ids}, step_ids)
                    self.assertTrue(all(f.step_id in step_ids for f in view.facts))
                    for spec in view.viz_specs:
                        self.assertLessEqual(set(spec.cites_steps), step_ids)
                        self.assertEqual(spec.request_pinned_hash, spec.pinned_hash)
                        if spec.pinned is not None:
                            self.assertEqual(spec.pinned_hash, pinned_hash(spec.pinned))

    def test_every_number_on_screen_is_a_fact_written_by_format_fact(self) -> None:
        for name in STREAMS:
            _, frames = read_stream(name)
            for view in views(frames):
                with self.subTest(name, revision=view.revision):
                    facts = {f.id: f for f in view.facts}
                    for seg in all_segments(view):
                        if seg.fact_id is not None:
                            fact = facts[seg.fact_id]
                            self.assertEqual(seg.text, format_fact(fact.value, fact.unit), seg.fact_id)
                    stray = [text for text in screen_strings(view) if re.search(r"\d", DIGIT_ALLOW.sub("", text))]
                    self.assertEqual(stray, [], "a number outside a fact segment")
                    for follow_up in view.follow_ups:
                        # A follow-up may name the one top N its analysis takes (Verifier docstring).
                        digits = set(re.findall(r"\d+", f"{follow_up.label} {follow_up.question}"))
                        if digits:
                            self.assertIn(follow_up.kind, ("top_mutated_genes", "differential_expression"))
                            self.assertEqual(len(digits), 1)
                            self.assertTrue(1 <= int(digits.pop()) <= 100)

    def test_model_text_cites_facts_and_passes_v_num(self) -> None:
        for name in STREAMS:
            _, frames = read_stream(name)
            for view in views(frames):
                facts = {f.id: f for f in view.facts}
                cohort_names = {c.name for c in view.cohorts} | {c.saved_name for c in view.cohorts if c.saved_name}
                for sentence in model_sentences(view):
                    with self.subTest(name, revision=view.revision):
                        if sentence.dropped is not None:
                            continue
                        assert sentence.cites is not None, "a kept model sentence keeps the composer's cites"
                        self.assertLessEqual(set(sentence.cites), set(facts))
                        prose = "".join(seg.text for seg in sentence.segments if seg.fact_id is None)
                        for allowed in sorted(cohort_names, key=len, reverse=True):
                            prose = prose.replace(allowed, "")
                        self.assertIsNone(NUMBER_WORDS.search(prose), prose)
                        self.assertIsNone(re.search(r"\d", DIGIT_ALLOW.sub("", prose)), prose)
                        genes = {str(f.value) for f in facts.values() if f.unit == "gene"} | {"KRAS", "NRAS", "TP53", "BRAF", "DIS3", "MYC"}
                        self.assertEqual([g for g in genes if re.search(rf"\b{g}\b", prose)], [], "a gene symbol outside a placeholder")

    def test_limitation_segments_say_what_the_text_says(self) -> None:
        for name in STREAMS:
            _, frames = read_stream(name)
            for view in views(frames):
                for item in view.limitations:
                    if item.segments is not None:
                        self.assertEqual("".join(seg.text for seg in item.segments), item.text)

    def test_no_step_waits_for_the_user_outside_the_confirmation_pause(self) -> None:
        for name in STREAMS:
            _, frames = read_stream(name)
            for view in views(frames):
                waiting = [s.id for s in view.steps if s.status == "AwaitingUser"]
                with self.subTest(name, revision=view.revision):
                    self.assertEqual(waiting, [view.pending.step_id] if isinstance(view.pending, ConfirmPending) else [])
                    reused = [s for s in view.steps if s.status == "Reused"]
                    self.assertTrue(all(s.reused_from is not None and s.reused_from.plan_version < (view.plan.version if view.plan else 1) for s in reused))

    def test_resumes_send_the_applied_action_first(self) -> None:
        for name in RESUMES:
            with self.subTest(name):
                _, frames = read_stream(name)
                first = frames[1]
                assert isinstance(first, StateSnapshotFrame), "the run's snapshot with the action applied follows RUN_STARTED"
                self.assertIsNone(first.snapshot.pending)
                self.assertIn(first.snapshot.status, ("Executing", "Stopping"))
        _, partial = read_stream("j1-partial")
        _, retried = read_stream("j1-retry")
        self.assertEqual(views(retried)[0].revision, views(partial)[-1].revision + 1)

    def test_answering_shows_observed_before_the_model_text(self) -> None:
        for name in [n for n, (status, _) in STREAMS.items() if status == "Complete"]:
            with self.subTest(name):
                _, frames = read_stream(name)
                answering = [v for v in views(frames) if v.status == "Answering"]
                complete = views(frames)[-1]
                self.assertEqual(len(answering), 1)
                self.assertTrue(answering[0].observed and answering[0].viz_specs)
                self.assertEqual((answering[0].interpretation, answering[0].hypotheses, answering[0].follow_ups), (None, None, []))
                self.assertTrue(complete.interpretation and complete.hypotheses and complete.follow_ups)
                self.assertTrue(set(f.kind for f in complete.follow_ups) <= set(get_args(AnalysisKind)))
                kinds = [type(f) for f in frames]
                self.assertEqual(kinds[-4:], [TextMessageStartFrame, TextMessageContentFrame, TextMessageEndFrame, RunFinishedFrame])
                self.assertIsInstance(frames[-5], StateSnapshotFrame)

    def test_save_pause_sends_the_pending_payload_as_save_cohorts_args(self) -> None:
        _, frames = read_stream("j3-save-pause")
        pause = views(frames)[-1]
        pending = pause.pending
        assert isinstance(pending, ConfirmPending)
        self.assertEqual(pending.plan_signature, pause.plan.signature if pause.plan else None)
        self.assertEqual([s.status for s in pause.steps if s.id == pending.step_id], ["AwaitingUser"])
        start = next(f for f in frames if isinstance(f, ToolCallStartFrame))
        self.assertEqual((start.tool_call_id, start.tool_call_name), (pending.nonce, "save_cohorts"))
        args_text = "".join(f.delta for f in frames if isinstance(f, ToolCallArgsFrame))
        args = SaveCohortsArgs.model_validate_json(args_text)
        self.assertEqual(ConfirmPending.model_validate(args.model_dump()), pending)
        self.assertEqual([type(f) for f in frames[-2:]], [ToolCallEndFrame, RunFinishedFrame])
        for cohort in args.cohorts:
            assert cohort.definition is not None
            self.assertEqual(cohort.def_sha256, def_sha256(cohort.definition))
            listed = next(c for c in pause.cohorts if c.ref == cohort.ref)
            self.assertEqual((listed.save, listed.def_sha256, listed.case_count), ("proposed", cohort.def_sha256, cohort.case_count))

    def test_confirm_turn_resumes_the_paused_run(self) -> None:
        _, pause_frames = read_stream("j3-save-pause")
        _, frames = read_stream("j3-after-confirm")
        paused, resumed = views(pause_frames)[-1], views(frames)
        self.assertEqual((resumed[0].thread_id, resumed[0].run_id), (paused.thread_id, paused.run_id))
        self.assertEqual(resumed[0].revision, paused.revision + 1)
        self.assertIsInstance(frames[1], StateSnapshotFrame)
        self.assertEqual((resumed[0].status, resumed[0].pending), ("Executing", None))
        self.assertEqual([s.status for s in resumed[0].steps if s.id == "s8"], ["Running"], "the write step runs as the confirm applies")
        final = resumed[-1]
        saved = {c.ref: c for c in final.cohorts if c.save == "saved"}
        self.assertEqual(set(saved), {"kras_wt", "kras_mut"})
        self.assertTrue(all(c.cohort_id for c in saved.values()))
        spec = final.viz_specs[0]
        assert isinstance(spec.pinned, DeDirectPinned)
        self.assertEqual([g.name for g in spec.pinned.groups], [saved["kras_wt"].saved_name, saved["kras_mut"].saved_name])
        self.assertEqual(spec.caption, "Table sorted by fold change. Sort by adjusted p-value to match the answer.")
        self.assertEqual(spec.cohort_refs, ["kras_wt", "kras_mut"])

    def test_clarify_and_consent_pause_before_any_data_call(self) -> None:
        for name, kind in (("clarify", ClarifyPending), ("consent", ConsentPending)):
            with self.subTest(name):
                _, frames = read_stream(name)
                self.assertFalse(any(isinstance(f, StepStartedFrame) for f in frames))
                pending = views(frames)[-1].pending
                self.assertIsInstance(pending, kind)
        _, frames = read_stream("clarify")
        clarify = views(frames)[-1].pending
        assert isinstance(clarify, ClarifyPending)
        self.assertTrue(all(o.field is not None and o.preview for o in clarify.options))
        _, frames = read_stream("consent")
        consent = views(frames)[-1].pending
        assert isinstance(consent, ConsentPending)
        self.assertEqual((consent.named.app_id, consent.proposed.app_id), ("OncoMatrix", "Correlation"))


class BuilderTests(unittest.TestCase):
    """`fixtures/build_fixtures.py` reproduces every fixture byte for byte (it is the only way to change them)."""

    def test_builder_output_equals_the_fixtures_on_disk(self) -> None:
        if not (FIXTURES / "build_fixtures.py").exists():
            self.skipTest(f"{FIXTURES} is not mounted")
        builder = load_builder()
        built: dict[str, str] = builder.build_all()
        self.assertTrue(set(built) >= {f"{name}.events.jsonl" for name in STREAMS})
        for name, text in built.items():
            with self.subTest(name):
                self.assertEqual((FIXTURES / name).read_text(encoding="utf-8"), text)
        mirrored: dict[str, str] = builder.mirror_files(built)
        self.assertEqual(json.loads(mirrored["agui/__fixtures__/j1Stub.ts"].split("=\n  ", 1)[1].rstrip(";\n")), built["j1-stub.events.jsonl"])


class CohortDefinitionTests(unittest.TestCase):
    """`fixtures/cohort-def-vectors.json`: both sides reproduce every canonical definition and defSha256."""

    def setUp(self) -> None:
        path = FIXTURES / "cohort-def-vectors.json"
        if not path.exists():
            self.skipTest(f"{path} is not mounted")
        self.vectors: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["vectors"]

    def test_every_vector_reproduces_its_canonical_json_and_sha256(self) -> None:
        for vector in self.vectors:
            with self.subTest(vector["name"]):
                if "definition" in vector:
                    definition = CANONICAL_DEFINITION.validate_python(vector["definition"])
                    self.assertEqual(canonical_definition(definition), vector["canonical"])
                    self.assertEqual(def_sha256(definition), vector["sha256"])
                else:
                    read = definition_from_filter_set(FilterSet.model_validate(vector["filterSet"]))
                    self.assertEqual(None if read is None else canonical_definition(read), vector["canonical"])
                    self.assertEqual(None if read is None else def_sha256(read), vector["sha256"])
                if vector["canonical"] is not None:
                    self.assertEqual(sha256_hex(vector["canonical"]), vector["sha256"])

    def test_equivalent_definitions_share_a_hash(self) -> None:
        by_hash: dict[str | None, list[str]] = {}
        for vector in self.vectors:
            by_hash.setdefault(vector["sha256"], []).append(vector["name"])
        groups = sorted(len(names) for digest, names in by_hash.items() if digest is not None)
        self.assertEqual(groups[-3:], [2, 2, 4], "case-ID forms, the J2 race forms and = versus includes")
        self.assertEqual(len(by_hash[None]), 3, "OR across fields, excludes, a number outside the canonical range")

    def test_an_out_of_range_number_makes_a_filter_unreadable_instead_of_raising(self) -> None:
        tiny = FilterSet.model_validate({"mode": "and", "root": {"x": {"operator": ">=", "field": "x", "operand": 0.00005}}})
        self.assertIsNone(definition_from_filter_set(tiny))

    def test_the_hash_covers_the_normalized_definition_only(self) -> None:
        a = CANONICAL_DEFINITION.validate_python({"kind": "case_ids", "caseIds": ["B", "A", "A"]})
        b = CANONICAL_DEFINITION.validate_python({"kind": "case_ids", "caseIds": ["A", "B"]})
        c = CANONICAL_DEFINITION.validate_python({"kind": "case_ids", "caseIds": ["A", "C"]})
        self.assertEqual(def_sha256(a), def_sha256(b))
        self.assertNotEqual(def_sha256(a), def_sha256(c))
        with self.assertRaises(ValidationError):
            CANONICAL_DEFINITION.validate_python({"kind": "filter", "clauses": [{"field": "x", "op": "in", "values": ["a", 1]}]})
        with self.assertRaises(ValidationError):
            CANONICAL_DEFINITION.validate_python({"kind": "filter", "clauses": [{"field": "x", "op": ">=", "values": [1, 2]}]})


class RequestExampleTests(unittest.TestCase):
    """`fixtures/request-examples.json`: one RunAgentInput per action, each valid."""

    def test_every_example_validates_and_every_action_is_covered(self) -> None:
        path = FIXTURES / "request-examples.json"
        if not path.exists():
            self.skipTest(f"{path} is not mounted")
        examples: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["examples"]
        actions: set[str] = set()
        edit_fields: set[str] = set()
        for example in examples:
            with self.subTest(example["name"]):
                action = RunAgentInputModel.model_validate(example["body"]).orion()
                actions.add(action.action)
                if action.action == "edit_plan":
                    edit_fields.add(action.field)
        self.assertEqual(actions, set(get_args(ActionName)))
        self.assertEqual(edit_fields, {"scope", "method", "topN", "thresholds", "cohortName"})


class ActionModelTests(unittest.TestCase):
    def test_confirm_rules(self) -> None:
        saved: JsonValue = {"ref": "kras_wt", "cohortId": "c1", "name": "KRAS not detected (ORION)", "defSha256": "a" * 64, "reused": False}
        base: dict[str, JsonValue] = {"action": "confirm", "nonce": "b" * 32}
        ConfirmAction.model_validate({**base, "decision": "create", "saved": [saved], "makeActive": "kras_wt"})
        bad: list[dict[str, JsonValue]] = [
            {**base, "decision": "dont_save", "saved": [saved]},
            {**base, "decision": "dont_save", "saved": [], "makeActive": "kras_wt"},
            {**base, "decision": "create", "saved": []},
            {**base, "decision": "create", "saved": [saved], "makeActive": "kras_mut"},
            {**base, "decision": "create", "saved": [saved, saved]},
            {**base, "nonce": "B" * 32, "decision": "dont_save", "saved": []},
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ORION_ACTION.validate_python(payload)

    def test_edit_plan_validates_the_value_for_its_field(self) -> None:
        ctx = context().model_dump(mode="json")
        good: list[dict[str, JsonValue]] = [
            {"field": "scope", "value": {"source": "saved_cohort", "cohortName": "Black patients"}, "cohortContext": ctx},
            {"field": "method", "value": "limma"},
            {"field": "topN", "value": 5},
            {"field": "thresholds", "value": {"pValueCutoff": 0.05, "pValueType": "original", "foldChangeCutoff": 1}},
            {"field": "cohortName", "value": "Renamed", "ref": "kras_mut"},
        ]
        bad: list[dict[str, JsonValue]] = [
            {"field": "scope", "value": {"source": "all_cases"}},
            {"field": "method", "value": "deseq2"},
            {"field": "topN", "value": 0},
            {"field": "thresholds", "value": {"pValueCutoff": 0, "pValueType": "adjusted", "foldChangeCutoff": 0.3}},
            {"field": "cohortName", "value": "Renamed"},
            {"field": "notes", "value": "x"},
        ]
        for payload in good:
            ORION_ACTION.validate_python({"action": "edit_plan", **payload})
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ORION_ACTION.validate_python({"action": "edit_plan", **payload})
        renamed = ORION_ACTION.validate_python({"action": "edit_plan", **good[-1]})
        self.assertIsInstance(renamed, EditCohortNameAction)

    def test_viz_reports_ride_on_any_action(self) -> None:
        report = {"runId": new_id(), "vizId": "v1", "mounted": True, "error": None, "pinnedHashVerified": True}
        action = ORION_ACTION.validate_python({"action": "cancel", "vizReports": [report]})
        self.assertEqual(len(action.viz_reports), 1)
        with self.assertRaises(ValidationError):
            ORION_ACTION.validate_python({"action": "cancel", "vizReports": [{**report, "vizId": "chart"}]})


class ViewModelTests(unittest.TestCase):
    def setUp(self) -> None:
        if not FIXTURES.exists():
            self.skipTest(f"{FIXTURES} is not mounted")
        _, frames = read_stream("j3-save-pause")
        self.pause = views(frames)[-1]

    def test_pending_must_match_the_status(self) -> None:
        raw = self.pause.model_dump(mode="json")
        for change in ({"status": "Executing"}, {"pending": None}, {"status": "Clarifying"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                RunView.model_validate({**raw, **change})

    def test_pending_cohorts_carry_a_matching_hash(self) -> None:
        pending = self.pause.pending
        assert isinstance(pending, ConfirmPending)
        cohort = pending.cohorts[0].model_dump(mode="json")
        with self.assertRaises(ValidationError):
            PendingCohort.model_validate({**cohort, "defSha256": "0" * 64})
        trimmed = PendingCohort.model_validate({**cohort, "definition": None})
        self.assertIsNone(trimmed.definition)
        with self.assertRaises(ValidationError):
            SaveCohortsArgs.model_validate({**pending.model_dump(mode="json"), "cohorts": [trimmed.model_dump(mode="json")]})

    def test_sentences_tables_and_viz_cohorts_are_consistent(self) -> None:
        Sentence(segments=[], dropped="V-SEC")
        with self.assertRaises(ValidationError):
            Sentence(segments=[Segment(text="x")], dropped="V-NUM")
        with self.assertRaises(ValidationError):
            FrozenTable(columns=["a", "b"], rows=[[Segment(text="1")]], note=None)
        _, frames = read_stream("j3-after-confirm")
        final = views(frames)[-1].model_dump(mode="json")
        with self.assertRaises(ValidationError):
            RunView.model_validate({**final, "cohorts": [c for c in final["cohorts"] if c["ref"] != "kras_mut"]})


class RunRecordTests(unittest.TestCase):
    """`fixtures/run-record.*.json`: the record route's bodies for the finished J1 run and the retried one."""

    def setUp(self) -> None:
        self.records: dict[str, tuple[dict[str, Any], RunRecord]] = {}
        for name in RECORDS:
            path = FIXTURES / name
            if not path.exists():
                self.skipTest(f"{path} is not mounted")
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            self.records[name] = (raw, RunRecord.model_validate(raw))

    def test_record_round_trips_and_projects_to_its_stored_view(self) -> None:
        for name, stream in zip(RECORDS, ("j1-complete", "j1-retry")):
            raw, record = self.records[name]
            with self.subTest(name):
                self.assertEqual(record.model_dump(mode="json"), raw)
                self.assertEqual(record.schema_, "orion.run-record/v1")
                self.assertEqual(record.view(), record.run_view)
                _, frames = read_stream(stream)
                self.assertEqual(record.run_view, views(frames)[-1])
                self.assertEqual(record.environment.fingerprint_sha256, record.view().environment.fingerprint_sha256)

    def test_every_fact_and_call_hash_is_consistent(self) -> None:
        for name, (_, record) in self.records.items():
            calls = {c.call_id: c for c in record.calls}
            self.assertEqual(len(calls), len(record.calls), f"{name}: callIds are unique")
            for call in record.calls:
                with self.subTest(name, call=call.call_id):
                    self.assertEqual(call.request_sha256, sha256_hex(canonical_json(call.request, portable=False)))
                    expected = None if call.observed is None else sha256_hex(canonical_json(call.observed, portable=False))
                    self.assertEqual(call.response_sha256, expected)
                    self.assertTrue(call.call_id.startswith(f"{record.run_id}.v{call.plan_version}.{call.step_id}.c"))
            for fact in record.facts:
                with self.subTest(name, fact=fact.id):
                    call = calls[fact.call_id]
                    self.assertEqual(call.status, "ok")
                    self.assertEqual((fact.step_id, fact.request_sha256, fact.response_sha256), (call.step_id, call.request_sha256, call.response_sha256))
                    pointer = fact.json_pointer.removeprefix("/observed")
                    value: Any = call.observed
                    for part in [p for p in pointer.split("/") if p]:
                        value = value[int(part)] if isinstance(value, list) else value[part]
                    self.assertEqual(value, fact.value)
            self.assertEqual({p.version for p in record.plan_history}, {p.version for p in record.plan_versions})
            for model in record.model_calls:
                TypeAdapter(ModelCallId).validate_python(model.call_id)

    def test_a_retried_step_numbers_every_attempt(self) -> None:
        _, record = self.records["run-record.j1-retry.json"]
        s4 = [(c.call_id.rsplit(".", 1)[1], c.attempt, c.status) for c in record.calls if c.step_id == "s4"]
        self.assertEqual(s4, [("c1", 1, "error"), ("c2", 2, "error"), ("c3", 1, "ok")])
        self.assertEqual([a.action for a in record.actions], ["ask", "retry_step"])

    def test_the_cited_requests_derive_the_pinned_inputs(self) -> None:
        for name, (_, record) in self.records.items():
            for spec in record.answer.viz_specs:
                with self.subTest(name, viz=spec.viz_id):
                    call = next(c for c in reversed(record.calls) if c.step_id == spec.cites_steps[0] and c.status == "ok")
                    assert isinstance(call.request, dict)
                    derived = derive_request_pinned(spec.launcher, call.request, call.observed)
                    self.assertEqual(pinned_hash(derived), spec.request_pinned_hash)

    def test_the_fingerprint_is_the_hash_of_s1s_document(self) -> None:
        for name, (_, record) in self.records.items():
            s1 = next(c for c in record.calls if c.step_id == "s1")
            assert isinstance(s1.observed, dict) and isinstance(s1.observed["document"], dict)
            self.assertEqual(fingerprint_sha256(s1.observed["document"]), record.environment.fingerprint_sha256, name)


class ScenarioFixtureTests(unittest.TestCase):
    """The paths builders most need to agree on (contract "Fixtures")."""

    def setUp(self) -> None:
        if not FIXTURES.exists():
            self.skipTest(f"{FIXTURES} is not mounted")

    def test_edit_at_the_pause_reuses_finished_steps_and_pauses_again_with_a_new_nonce(self) -> None:
        _, pause_frames = read_stream("j3-save-pause")
        _, frames = read_stream("j3-edit-at-pause")
        paused = views(pause_frames)[-1]
        first, last = views(frames)[0], views(frames)[-1]
        assert first.plan is not None and last.plan is not None and paused.plan is not None
        self.assertEqual((first.status, first.plan.version, first.pending), ("Executing", 2, None))
        self.assertEqual([(s.plan_version, s.status, s.had_answer) for s in first.superseded], [(1, "AwaitingConfirmation", False)])
        self.assertNotEqual(first.plan.signature, paused.plan.signature, "a cohort name is an argument of the write step and of de_run")
        reused = {s.id: s for s in first.steps if s.status == "Reused"}
        self.assertEqual(sorted(reused, key=lambda sid: int(sid[1:])), [f"s{i}" for i in range(1, 8)])
        self.assertTrue(all(s.reused_from is not None and (s.reused_from.plan_version, s.reused_from.step_id) == (1, s.id) for s in reused.values()))
        self.assertEqual([f.id for f in first.facts], [f.id for f in paused.facts], "facts carry over with the reused steps")
        pending, old = last.pending, paused.pending
        assert isinstance(pending, ConfirmPending) and isinstance(old, ConfirmPending)
        self.assertNotEqual(pending.nonce, old.nonce)
        self.assertEqual((pending.plan_version, pending.plan_signature), (2, last.plan.signature))
        self.assertEqual([c.name for c in pending.cohorts if c.ref == "kras_wt"], ["KRAS wild-type (ORION)"])
        self.assertFalse(any(isinstance(f, (StepStartedFrame, StepFinishedFrame)) for f in frames), "no step runs: s8 waits again")

    def test_dont_save_skips_the_write_and_runs_on_the_proposed_names(self) -> None:
        _, frames = read_stream("j3-dont-save")
        first, final = views(frames)[0], views(frames)[-1]
        self.assertEqual([(s.status, s.code) for s in first.steps if s.id == "s8"], [("Skipped", "WRITE_SKIPPED")])
        self.assertEqual({c.ref: c.save for c in final.cohorts if c.role == "planned" and c.ref != "tested"}, {"kras_wt": "skipped", "kras_mut": "skipped"})
        self.assertIn("WRITE_SKIPPED", [item.code for item in final.limitations])
        spec = final.viz_specs[0]
        assert isinstance(spec.pinned, DeDirectPinned)
        self.assertEqual([g.name for g in spec.pinned.groups], ["KRAS not detected (ORION)", "KRAS mutated (ORION)"])
        self.assertTrue(all(c.cohort_id is None for c in final.cohorts), "hand-off has no saved cohort to open")

    def test_partial_then_retry_step(self) -> None:
        _, partial = read_stream("j1-partial")
        statuses = [s.status for v in views(partial) for s in v.steps if s.id == "s4"]
        self.assertIn("Retrying", statuses)
        ended = views(partial)[-1]
        self.assertEqual([(s.id, s.status, s.code) for s in ended.steps[3:]], [("s4", "Failed", "TOOL_FAILED"), ("s5", "Skipped", None)])
        self.assertEqual((ended.withheld_reason, ended.interpretation, ended.viz_specs), ("a step failed", None, []))
        _, retried = read_stream("j1-retry")
        first, final = views(retried)[0], views(retried)[-1]
        self.assertEqual([s.status for s in first.steps[3:]], ["Queued", "Queued"])
        self.assertNotIn("TOOL_FAILED", [item.code for item in final.limitations])
        self.assertEqual(final.status, "Complete")

    def test_stop_shows_stopping_then_ends_cancelled_with_finished_steps_kept(self) -> None:
        _, frames = read_stream("j1-stop")
        statuses = [v.status for v in views(frames)]
        self.assertEqual((statuses[0], statuses[-1]), ("Stopping", "Cancelled"))
        final = views(frames)[-1]
        self.assertEqual([s.status for s in final.steps], ["Done", "Done", "Done", "Skipped", "Skipped"])
        self.assertTrue(all(s.output_summary == "Not run: stopped" for s in final.steps[3:]))
        self.assertEqual((final.code, final.withheld_reason), (None, "you stopped the run"))
        self.assertTrue(final.observed, "finished steps keep their Observed")
        self.assertFalse(any(isinstance(f, (StepStartedFrame, StepFinishedFrame)) for f in frames), "a joining stream drops the step it did not see start")

    def test_one_stream_can_report_an_earlier_run_first(self) -> None:
        for name, status in (("interrupted", "Interrupted"), ("cancel-waiting", "Cancelled")):
            with self.subTest(name):
                _, frames = read_stream(name)
                started, report = frames[0], frames[1]
                assert isinstance(started, RunStartedFrame) and isinstance(report, StateSnapshotFrame)
                self.assertNotEqual(report.snapshot.run_id, started.run_id)
                self.assertEqual((report.snapshot.status, report.snapshot.pending), (status, None))
                self.assertTrue(all(f.snapshot.run_id == started.run_id for f in frames[2:] if isinstance(f, StateSnapshotFrame)))
        _, frames = read_stream("interrupted")
        old = views(frames)[0]
        self.assertEqual(old.code, "RUN_INTERRUPTED")
        self.assertEqual([(s.status, s.code) for s in old.steps[3:]], [("Failed", "RUN_INTERRUPTED"), ("Skipped", None)])

    def test_a_trimmed_snapshot_drops_pinned_inputs_but_keeps_their_hashes(self) -> None:
        _, frames = read_stream("j3-deinput")
        specs = [spec for v in views(frames) for spec in v.viz_specs]
        self.assertTrue(specs and all(spec.launcher == "deInput" and spec.pinned is None for spec in specs))
        self.assertTrue(all(spec.pinned_hash == spec.request_pinned_hash for spec in specs))
        with self.assertRaises(ValidationError):
            AnswerRecord(viz_specs=specs[:1])

    def test_the_oncomatrix_card_pins_its_genes_in_display_order(self) -> None:
        _, frames = read_stream("oncomatrix")
        spec = views(frames)[-1].viz_specs[0]
        assert isinstance(spec.pinned, OncomatrixPinned)
        self.assertEqual(spec.pinned.genes, ["KRAS", "NRAS", "DIS3"])
        self.assertEqual(spec.cohort_refs, ["high"])

    def test_counts_on_the_confirmation_card_are_facts(self) -> None:
        _, frames = read_stream("j3-save-pause")
        pause = views(frames)[-1]
        pending = pause.pending
        assert isinstance(pending, ConfirmPending)
        facts = {f.id: f for f in pause.facts}
        for cohort in pending.cohorts:
            assert cohort.case_count_fact_id is not None and cohort.eligible_count_fact_id is not None
            self.assertEqual((facts[cohort.case_count_fact_id].value, facts[cohort.eligible_count_fact_id].value), (cohort.case_count, cohort.eligible_count))
        base = next(c for c in pause.cohorts if c.ref == "base")
        self.assertEqual((base.case_count, base.case_count_fact_id, base.cohort_id), (1000, "f.s1.cases", None))
        self.assertEqual(pause.environment.fingerprint_sha256, fingerprint_sha256(fingerprint_document(
            {"status": "OK", "synthetic": True, "cases": 1000}, {"version": "1.0.3"},
            {"versionInfo": {"pkgver": "2.216.0-20261003.02:08", "codedate": "20261003.02:08"}},
            [{"gene": g, "cases": n} for g, n in (("KRAS", 480), ("NRAS", 355), ("TP53", 301), ("DIS3", 288), ("BRAF", 97), ("MYC", 64))],
        )))


class VectorTests(unittest.TestCase):
    """The small vector files both sides reproduce: fact formats, fingerprints, request-pinned derivation."""

    def _vectors(self, name: str) -> list[dict[str, Any]]:
        path = FIXTURES / name
        if not path.exists():
            self.skipTest(f"{path} is not mounted")
        vectors: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["vectors"]
        return vectors

    def test_fact_format_vectors(self) -> None:
        vectors = self._vectors("fact-format-vectors.json")
        for vector in vectors:
            with self.subTest(vector=vector):
                self.assertEqual(format_fact(vector["value"], vector["unit"]), vector["text"])
        texts = {(v["value"], v["unit"]): v["text"] for v in vectors}
        self.assertEqual((texts[(1095, "days")], texts[(7.1e-60, "p")], texts[(-0.87, "log2FC")]), ("1,095", "7.1e-60", "-0.87"))
        self.assertEqual((texts[(0.125, "log2FC")], texts[(9.96e-05, "p")]), ("0.13", "1.0e-4"), "half away from zero; mantissa carries")

    def test_fingerprint_vectors(self) -> None:
        vectors = self._vectors("fingerprint-vectors.json")
        for vector in vectors:
            with self.subTest(vector["name"]):
                src = vector["sources"]
                document = fingerprint_document(src["sandboxStatus"], src["analysisVersion"], src["healthcheck"], src["ranking"])
                self.assertEqual(document, vector["document"])
                self.assertEqual(canonical_json(document), vector["canonical"])
                self.assertEqual(fingerprint_sha256(document), vector["sha256"])
        self.assertEqual(vectors[0]["sha256"], vectors[1]["sha256"], "a restart keeps the fingerprint")
        self.assertNotEqual(vectors[0]["sha256"], vectors[2]["sha256"])

    def test_request_pinned_vectors(self) -> None:
        vectors = self._vectors("request-pinned-vectors.json")
        self.assertEqual({v["launcher"] for v in vectors}, set(get_args(PinnedKind)))
        for vector in vectors:
            with self.subTest(vector["name"]):
                derived = derive_request_pinned(vector["launcher"], vector["request"], vector.get("observed"))
                self.assertEqual(derived.model_dump(mode="json"), vector["pinned"])
                self.assertEqual(pinned_hash(derived), vector["requestPinnedHash"])
        de_input = next(v for v in vectors if v["launcher"] == "deInput")
        self.assertIsInstance(PINNED.validate_python(de_input["pinned"]), DeInputPinned)


class SizeBudgetTests(unittest.TestCase):
    """The largest view the contract allows still fits one 32 KB frame (contract "Size: 32 KB")."""

    def setUp(self) -> None:
        if not (FIXTURES / "build_fixtures.py").exists():
            self.skipTest(f"{FIXTURES} is not mounted")
        self.builder = load_builder()

    def _fit(self, view: RunView) -> RunView:
        frame = events.encode(events.state_snapshot(view))
        self.assertLessEqual(len(frame.encode()), SNAPSHOT_LIMIT_BYTES)
        return StateSnapshotFrame.model_validate_json(frame[len("data: "):]).snapshot

    def test_top_n_at_its_maximum_with_four_thousand_samples_fits(self) -> None:
        view: RunView = self.builder.largest_complete_view()
        fitted = self._fit(view)
        self.assertEqual([f.id for f in fitted.facts], [f.id for f in view.facts], "facts are never trimmed")
        self.assertEqual(fitted.observed, view.observed)
        self.assertEqual((fitted.interpretation, fitted.hypotheses, fitted.follow_ups), (view.interpretation, view.hypotheses, view.follow_ups))
        ranked = [f for f in fitted.facts if ".top." in f.id and f.id.endswith(".gene")]
        self.assertEqual(len(ranked), VIEW_RANK_CAP)
        spec = fitted.viz_specs[0]
        self.assertIsNone(spec.pinned, "4,000 sample ids are read from the view route")
        self.assertEqual((spec.pinned_hash, spec.request_pinned_hash), (view.viz_specs[0].pinned_hash, view.viz_specs[0].request_pinned_hash))
        assert spec.frozen is not None
        self.assertEqual(len(spec.frozen.rows), FROZEN_ROWS_CAP)

    def test_six_pending_cohorts_of_ten_thousand_ids_fit(self) -> None:
        view: RunView = self.builder.largest_pause_view()
        fitted = self._fit(view)
        pending, original = fitted.pending, view.pending
        assert isinstance(pending, ConfirmPending) and isinstance(original, ConfirmPending)
        self.assertEqual([c.definition for c in pending.cohorts if c.ref.startswith("group_")], [None] * 6)
        self.assertEqual([c.def_sha256 for c in pending.cohorts], [c.def_sha256 for c in original.cohorts], "hashes are never trimmed")


class ContractModelTests(unittest.TestCase):
    def test_stopping_holds_a_short_lease_that_is_never_renewed(self) -> None:
        self.assertIn("Stopping", LEASED_STATUSES)
        self.assertNotIn("Stopping", RENEWED_STATUSES)
        self.assertLess(STOP_LEASE_S, 180)

    def test_call_ids_name_the_version_step_and_attempt(self) -> None:
        good: TypeAdapter[str] = TypeAdapter(CallId)
        run = new_id()
        good.validate_python(f"{run}.v2.s10.c3")
        for bad in (f"{run[:8]}.s2.1", f"{run}.s2.c1", f"{run}.v0.s2.c1", f"{run}.v1.s2.1"):
            with self.subTest(bad), self.assertRaises(ValidationError):
                good.validate_python(bad)
        TypeAdapter(ModelCallId).validate_python(f"{run}.planner.c2")
        TypeAdapter(ModelCallId).validate_python(f"{run}.v3.composer.c1")

    def test_reused_steps_name_their_origin(self) -> None:
        step: dict[str, JsonValue] = {"id": "s3", "row": 1, "tool": "t", "kind": "server", "folded": False, "inputSummary": "x",
                                      "status": "Reused", "outputSummary": "y", "durationMs": 1, "code": None, "reusedFrom": None}
        with self.assertRaises(ValidationError):
            StepView.model_validate(step)
        StepView.model_validate({**step, "reusedFrom": {"planVersion": 1, "stepId": "s3"}})
        with self.assertRaises(ValidationError):
            StepView.model_validate({**step, "status": "Done", "reusedFrom": {"planVersion": 1, "stepId": "s3"}})

    def test_top_n_editables_carry_their_maximum(self) -> None:
        EditableTopN(field="topN", label="Top N", value=50, max=50)
        with self.assertRaises(ValidationError):
            EditableTopN(field="topN", label="Top N", value=51, max=50)

    def test_fault_injection_is_for_test_runs_only(self) -> None:
        self.assertEqual(parse_faults("de_run:timeout, planner:refuse,cohort_count:empty"),
                         {"de_run": "timeout", "planner": "refuse", "cohort_count": "empty"})
        for bad in ("de_run:refuse", "planner:timeout", "nope:timeout", "de_run:timeout,de_run:error"):
            with self.subTest(bad), self.assertRaises(ValueError):
                parse_faults(bad)
        self.assertEqual(Settings.model_validate({"faults": "composer:refuse"}).faults, {"composer": "refuse"})
        with self.assertRaises(ValidationError):
            Settings.model_validate({"mode": "A", "faults": "de_run:timeout"})
        Settings.model_validate({"mode": "A", "test_runtime": True, "faults": "de_run:timeout"})


class ExpiredLeaseContractTests(ControllerCase):
    """Check 4 and the view route's expired-lease rule, as the controller records it."""

    async def _orphan(self, thread_id: str, status: RunStatus) -> tuple[str, Lease]:
        store = RunStore(self.run_dir)
        run_id, now = new_id(), utc_now()
        lease = Lease(owner="dead:1", renewed_at=iso_utc(now - timedelta(seconds=60)), expires_at=iso_utc(now - timedelta(seconds=30)))
        statuses: tuple[tuple[int, StepStatus], ...] = ((1, "Done"), (2, "Running"), (3, "Queued"))
        record = RunRecord(
            thread_id=thread_id, run_id=run_id, revision=5, status=status, question="old", created_at=iso_utc(now - timedelta(seconds=90)),
            updated_at=iso_utc(now - timedelta(seconds=60)), environment=Environment(mode="A", planner="stub", executor="stub", agent_version="test"),
            lease=lease, request=AskAction(action="ask", question="old", cohort_context=context()),
            steps=[
                StepView(id=f"s{i}", row=1, tool="cohort_count", kind="server", folded=False, input_summary="x",
                         status=step, output_summary=None, duration_ms=None, code=None)
                for i, step in statuses
            ],
        )
        await store.save_run(record)
        await store.save_thread(ThreadIndex(thread_id=thread_id, run_ids=[run_id], active_run_id=run_id))
        return run_id, lease

    async def test_an_expired_stopping_run_ends_cancelled_not_interrupted(self) -> None:
        _, app = self.make()
        thread_id = new_id()
        old, lease = await self._orphan(thread_id, "Stopping")
        body = action_body(thread_id, old, {"action": "cancel"})
        frames = check_stream(self, (await asgi_call(app, "POST", "/invocations", body)).text, expect_run_id=old)
        view = snapshots(frames)[0].snapshot
        self.assertEqual((view.status, view.code, view.revision, view.updated_at), ("Cancelled", None, 6, lease.expires_at))
        self.assertEqual([(s.status, s.output_summary) for s in view.steps], [("Done", None), ("Skipped", "Not run: stopped"), ("Skipped", "Not run: stopped")])
        last = frames[-1]
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual(last.result.status, "Cancelled")
        record = self.read_json(self.run_dir / "runs" / f"{old}.json")
        self.assertEqual((record["status"], record["lease"], record["updatedAt"]), ("Cancelled", None, lease.expires_at))

    async def test_the_controller_records_an_interruption_as_of_the_lease_expiry(self) -> None:
        _, app = self.make()
        thread_id = new_id()
        old, lease = await self._orphan(thread_id, "Executing")
        frames = check_stream(self, (await asgi_call(app, "POST", "/invocations", ask_body(thread_id, new_id()))).text)
        report = snapshots(frames)[0].snapshot
        self.assertEqual((report.run_id, report.status, report.updated_at), (old, "Interrupted", lease.expires_at))


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
        spec: dict[str, JsonValue] = {
            "vizId": "v1", "appId": "DE", "launcher": "deDirect", "citesSteps": ["s1"], "title": "DE", "caption": None,
            "requestPinnedHash": absent_hash, "frozen": None, "cohortRefs": [],
        }
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
        views_ = [f.snapshot for f in snapshots(frames)]
        self.assertEqual([v.status for v in views_[:2]], ["Planning", "Executing"])
        self.assertIsNone(views_[0].plan)
        plan = views_[1].plan
        assert plan is not None
        fixture_plan = self._fixture_plan()
        # The whole plan but its signature, the scope edit included: every plan offers it (TOOLS.md 6.4).
        self.assertEqual(plan.model_dump(exclude={"signature"}), fixture_plan.model_dump(exclude={"signature"}))
        self.assertEqual([e.field for e in plan.editable], ["scope"])
        self.assertEqual(
            [s.model_dump(include={"id", "row", "tool", "kind", "folded", "input_summary"}) for s in views_[1].steps],
            [s.model_dump(include={"id", "row", "tool", "kind", "folded", "input_summary"}) for s in self._fixture_steps()],
        )
        final = views_[-1]
        self.assertEqual((final.status, final.code), ("Limited", "NOT_IMPLEMENTED"))
        self.assertTrue(all(s.status == "Skipped" and s.code == "NOT_IMPLEMENTED" for s in final.steps))
        self.assertTrue(all(s.output_summary == STUB_SKIP_REASON for s in final.steps))
        self.assertEqual((final.facts, final.observed, final.viz_specs), ([], [], []))
        self.assertEqual([lim.code for lim in final.limitations], ["NOT_IMPLEMENTED"])
        self.assertEqual((final.pending, final.follow_ups, final.notice), (None, [], SYNTHETIC_NOTICE))
        last = frames[-1]
        assert isinstance(last, RunFinishedFrame)
        self.assertEqual((last.result.status, last.result.code), ("Limited", "NOT_IMPLEMENTED"))

        record = self.read_json(self.run_dir / "runs" / f"{run_id}.json")
        self.assertEqual(record["schema"], "orion.run-record/v1")
        self.assertEqual((record["status"], record["revision"], record["lease"]), ("Limited", final.revision, None))
        # v1.2: the record names both model roles, so a run that never composed still says which composer it had.
        self.assertEqual((record["environment"]["planner"], record["environment"]["composer"]), ("stub", "stub"))
        self.assertEqual(record["question"], J1_QUESTION)
        # The record keeps the view the BFF view route serves, as of the last transition.
        self.assertEqual(RunView.model_validate(record["runView"]), final)
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


ONCO_QUESTION = "Top 3 most frequently mutated genes in CGS high-risk patients"
CGS = "cgs_risk_key_criteria.cgs_risk_category"
HIGH_SAVED: list[dict[str, JsonValue]] = [{
    "id": "c-high", "name": "CGS high-risk (ORION)", "caseCount": 339,
    "filters": {"mode": "and", "root": {CGS: {"operator": "includes", "field": CGS, "operands": ["High risk"]}}},
}]


class EveryPlanTests(unittest.TestCase):
    """Rules every fixture's plan keeps, so the fixtures are what the compiler sends."""

    def setUp(self) -> None:
        if not FIXTURES.exists():
            self.skipTest(f"{FIXTURES} is not mounted")

    def test_every_plan_offers_the_scope_edit_first(self) -> None:
        # TOOLS.md 6.4: scope on every plan. The card offers an edit only while the status accepts edit_plan.
        for name in STREAMS:
            _, frames = read_stream(name)
            for view in views(frames):
                if view.plan is None:
                    continue
                with self.subTest(name, revision=view.revision):
                    self.assertEqual(view.plan.editable[0].field, "scope")
                    self.assertEqual(view.plan.editable[0].value, ScopeChoice(source=view.plan.scope.source))

    def test_a_plan_writes_exactly_when_it_has_a_save_step(self) -> None:
        for name in STREAMS:
            _, frames = read_stream(name)
            for view in views(frames):
                if view.plan is None:
                    continue
                with self.subTest(name, revision=view.revision):
                    saves = [s.id for s in view.steps if s.tool == "save_cohorts"]
                    self.assertEqual(view.plan.writes, bool(saves))
                    # C-SAVE: a planned cohort is saved, reused at compile time, or the plan has no planned cohort to keep.
                    proposed = [c.ref for c in view.cohorts if c.role == "planned" and c.save in ("proposed", "saved", "skipped")]
                    if not saves:
                        self.assertEqual(proposed, [])


class J2FixtureTests(unittest.TestCase):
    """J2 saves its planned cohort once, last (C-SAVE, TOOLS.md 5.2), or reuses an identical saved one (choice 15)."""

    def setUp(self) -> None:
        if not FIXTURES.exists():
            self.skipTest(f"{FIXTURES} is not mounted")

    def test_the_pause_offers_one_cohort_without_an_eligible_count(self) -> None:
        _, frames = read_stream("j2-save-pause")
        pause = views(frames)[-1]
        pending = pause.pending
        assert isinstance(pending, ConfirmPending) and pause.plan is not None
        self.assertEqual((pending.step_id, pause.plan.rows[-1].label, pause.plan.writes), ("s5", "Save 1 cohort", True))
        [cohort] = pending.cohorts
        self.assertEqual((cohort.ref, cohort.name, cohort.eligible_count, cohort.eligible_count_fact_id), ("high", "CGS high-risk (ORION)", None, None))
        assert cohort.definition is not None
        self.assertEqual(cohort.definition.kind, "filter", "a planned filter cohort is saved as its filter definition")
        self.assertEqual((pause.observed, pause.viz_specs), ([], []), "Observed renders after the confirm turn")
        self.assertEqual([e.field for e in pause.plan.editable], ["scope", "topN", "cohortName"])

    def test_create_saves_the_cohort_and_pins_the_card_to_it(self) -> None:
        _, pause_frames = read_stream("j2-save-pause")
        _, frames = read_stream("j2-after-confirm")
        self.assertEqual(views(frames)[0].revision, views(pause_frames)[-1].revision + 1)
        final = views(frames)[-1]
        high = next(c for c in final.cohorts if c.ref == "high")
        self.assertEqual((high.save, high.saved_name), ("saved", "CGS high-risk (ORION)"))
        self.assertIsNotNone(high.cohort_id)
        self.assertEqual([s.output_summary for s in final.steps if s.id == "s5"], ["1 cohort saved and read back"])
        self.assertEqual([spec.cohort_refs for spec in final.viz_specs], [["high"]])

    def test_dont_save_skips_the_write_and_turns_hand_off_off(self) -> None:
        _, frames = read_stream("j2-dont-save")
        first, final = views(frames)[0], views(frames)[-1]
        self.assertEqual([(s.status, s.code) for s in first.steps if s.id == "s5"], [("Skipped", "WRITE_SKIPPED")])
        self.assertEqual([c.save for c in final.cohorts if c.ref == "high"], ["skipped"])
        skipped = [lim for lim in final.limitations if lim.code == "WRITE_SKIPPED"]
        self.assertEqual([lim.text for lim in skipped], [catalog.TEMPLATES["save.skipped_one"].pattern])

    def test_the_follow_up_reuses_an_identical_saved_cohort_and_plans_no_save(self) -> None:
        _, frames = read_stream("oncomatrix")
        final = views(frames)[-1]
        assert final.plan is not None
        self.assertEqual((final.plan.writes, [s.tool for s in final.steps][-1]), (False, "rank_top_n"))
        high = next(c for c in final.cohorts if c.ref == "high")
        self.assertEqual((high.save, high.cohort_id, high.saved_name), ("reused", "3c7a1e9b-5d2f-4b8a-9e6c-1f0d2a4b6c88", "CGS high-risk (ORION)"))
        examples: list[dict[str, Any]] = json.loads((FIXTURES / "request-examples.json").read_text(encoding="utf-8"))["examples"]
        ask = next(e["body"] for e in examples if e["body"]["runId"] == final.run_id)
        saved = ask["forwardedProps"]["orion"]["cohortContext"]["saved"]
        read = definition_from_filter_set(FilterSet.model_validate(saved[0]["filters"]))
        assert read is not None
        self.assertEqual((saved[0]["id"], def_sha256(read)), (high.cohort_id, high.def_sha256), "the ask's saved cohort is the one reused")


class EngineFixtureShapeTests(EngineCase):
    """The production wiring (real compiler, engine and renderer, fake tools) sends the J2 fixtures' shapes."""

    def j2(self) -> ir.Plan:
        output = plan_question(ONCO_QUESTION, context())
        assert isinstance(output, ir.Plan)
        return output

    async def test_j2_pause_create_and_dont_save_have_the_fixture_shapes(self) -> None:
        _, app = self.build([self.j2(), self.j2()])
        for decision, fixture in (("create", "j2-after-confirm"), ("dont_save", "j2-dont-save")):
            with self.subTest(decision):
                thread_id, run_id = new_id(), new_id()
                frames = await self.turn(app, ask_body(thread_id, run_id, question=ONCO_QUESTION), run_id)
                self.assertEqual(shape(frames), shape(fixture_frames("j2-save-pause")))
                args = SaveCohortsArgs.model_validate_json("".join(f["delta"] for f in frames if f["type"] == "TOOL_CALL_ARGS"))
                expected = SaveCohortsArgs.model_validate_json("".join(
                    f["delta"] for f in fixture_frames("j2-save-pause") if f["type"] == "TOOL_CALL_ARGS"))
                self.assertEqual([c.model_dump(exclude={"case_count"}) for c in args.cohorts],
                                 [c.model_dump(exclude={"case_count"}) for c in expected.cohorts])
                pending = ConfirmPending.model_validate(args.model_dump(mode="json"))
                confirm = self.create(pending) if decision == "create" else {
                    "action": "confirm", "nonce": pending.nonce, "decision": "dont_save", "saved": [], "makeActive": None}
                frames = await self.turn(app, action_body(thread_id, run_id, confirm), run_id)
                self.assertEqual(shape(frames), shape(fixture_frames(fixture)))
                final, wanted = self.last_view(frames), views([parse_frame(json.dumps(f)) for f in fixture_frames(fixture)])[-1]
                self.assertEqual(final.limitations, wanted.limitations)
                self.assertEqual([(v.title, v.caption, v.cohort_refs) for v in final.viz_specs],
                                 [(v.title, v.caption, v.cohort_refs) for v in wanted.viz_specs])

    async def test_the_follow_up_with_the_cohort_saved_has_the_oncomatrix_shape(self) -> None:
        _, app = self.build([self.j2()])
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, ask_body(thread_id, run_id, question=ONCO_QUESTION, cohort_context=context(saved=HIGH_SAVED)), run_id)
        self.assertEqual(shape(frames), shape(fixture_frames("oncomatrix")))
        final = self.last_view(frames)
        wanted = views([parse_frame(json.dumps(f)) for f in fixture_frames("oncomatrix")])[-1]
        assert final.plan is not None and wanted.plan is not None
        self.assertEqual(final.plan.model_dump(exclude={"signature"}), wanted.plan.model_dump(exclude={"signature"}))

    async def test_a_stopped_step_keeps_how_long_it_ran(self) -> None:
        # StepSession.skip_step(duration_ms=): the j1-stop fixture shows the aborted step's durationMs.
        tools = FakeTools({"survival_compare": "block"})
        _, app = self.build(tools=tools)
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", ask_body(thread_id, run_id)))
        await asyncio.wait_for(tools.started.wait(), 5)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        await ask
        stopped = [s for s in self.last_view(frames).steps if s.output_summary == "Not run: stopped"]
        self.assertEqual([s.id for s in stopped], ["s4", "s5"])
        self.assertIsInstance(stopped[0].duration_ms, int, "the aborted running step ran for a measured time")
        self.assertIsNone(stopped[1].duration_ms, "a step that never started has no duration")
        fixture = views([parse_frame(json.dumps(f)) for f in fixture_frames("j1-stop")])[-1]
        self.assertEqual([s.duration_ms is None for s in fixture.steps[3:]], [False, True])

    async def test_the_engine_gives_each_attempt_the_step_timeout(self) -> None:
        # tools/base.py ToolClient: timeout_s is one attempt's budget, STEP_TIMEOUT_S; the client caps HTTP at 55 s.
        timeouts: list[float] = []

        class Recording(FakeTools):
            async def call(
                self, name: ServerToolName, input: JsonValue, *, timeout_s: float, run_id: str, plan_version: int, step_id: str,
                first_call: int, on_retry: RetryHook | None = None,
            ) -> ToolCallOutcome:
                timeouts.append(timeout_s)
                return await super().call(name, input, timeout_s=timeout_s, run_id=run_id, plan_version=plan_version,
                                          step_id=step_id, first_call=first_call, on_retry=on_retry)

        _, app = self.build(tools=Recording())
        thread_id, run_id = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, run_id), run_id)
        self.assertTrue(timeouts)
        self.assertEqual(set(timeouts), {STEP_TIMEOUT_S})


class InputHashTests(unittest.TestCase):
    """TOOLS.md 1 "Input hash and Reused": the hash covers dependsOn, in both implementations."""

    def test_a_scope_edit_changes_the_save_step_although_its_args_do_not(self) -> None:
        output = plan_question(ONCO_QUESTION, context())
        assert isinstance(output, ir.Plan)
        filtered = context({"demographic.race": {"operator": "in", "field": "demographic.race", "operands": ["white"]}}, name="White patients", case_count=600)
        before, after = compile_plan(output), compile_plan(output, filtered)
        old_save, new_save = before.write_step(), after.write_step()
        assert old_save is not None and new_save is not None
        self.assertEqual(old_save.args, new_save.args, "the save's own args name only the ref and the name")
        self.assertNotEqual(old_save.depends_on, new_save.depends_on)
        for hashes in (compiler_module.step_input_hashes, executor_module.input_hashes):
            with self.subTest(hashes.__module__):
                old, new = hashes(before), hashes(after)
                self.assertNotEqual(old[old_save.id], new[new_save.id], "a save whose cohort changed is never Reused")
                self.assertEqual(old["s1"], new["s1"], "the fingerprint step is unchanged by a scope edit")


class ContractV12Tests(unittest.TestCase):
    """Contract details settled after the Day 2 builds (contract README, models.py, interfaces.py, base.py)."""

    def test_unauthenticated_is_a_bff_code_the_browser_treats_as_a_rejected_turn(self) -> None:
        codes = set(get_args(models.BffErrorCode))
        self.assertEqual(set(models.BFF_ERROR_HANDLING), codes)
        self.assertEqual(set(models.BFF_ERROR_STATUS), codes)
        self.assertEqual((models.BFF_ERROR_STATUS["UNAUTHENTICATED"], models.BFF_ERROR_HANDLING["UNAUTHENTICATED"]), (401, "reject"))
        models.BffError.model_validate({"error": {"code": "UNAUTHENTICATED", "message": "Sign in again."}})

    def test_an_unreadable_read_back_is_null_and_fails_the_write(self) -> None:
        if not FIXTURES.exists():
            self.skipTest(f"{FIXTURES} is not mounted")
        _, frames = read_stream("j3-save-pause")
        pending = views(frames)[-1].pending
        assert isinstance(pending, ConfirmPending)
        saved: list[JsonValue] = [
            {"ref": c.ref, "cohortId": f"c-{c.ref}", "name": c.name, "defSha256": None if c.ref == "kras_wt" else c.def_sha256, "reused": False}
            for c in pending.cohorts
        ]
        confirm = ORION_ACTION.validate_python({"action": "confirm", "nonce": pending.nonce, "decision": "create", "saved": saved, "makeActive": None})
        assert isinstance(confirm, ConfirmAction)
        self.assertEqual(executor_module.verify_read_back(pending, confirm), ["KRAS not detected (ORION) was saved with a different definition"])
        bad: dict[str, JsonValue] = {"ref": "kras_wt", "cohortId": "c-kras_wt", "name": "x", "defSha256": "x", "reused": False}
        with self.assertRaises(ValidationError):
            ORION_ACTION.validate_python({**confirm.model_dump(mode="json"), "saved": [bad]})

    def test_the_default_model_names_are_the_runtime_clients_defaults(self) -> None:
        self.assertEqual((interfaces.PLANNER_MODEL, interfaces.REFUSAL_FALLBACK_MODEL), DEFAULT_MODELS["runtime"])

    def test_no_action_is_reserved_any_more(self) -> None:
        self.assertFalse(hasattr(models, "RESERVED_ACTIONS"))

    def test_an_executor_function_that_raised_has_its_own_tool_error_code(self) -> None:
        # TOOLS.md 1 "Error mapping": its CallRecord's error starts INTERNAL_ERROR.
        self.assertIn("INTERNAL_ERROR", get_args(ToolErrorCode))

    def test_the_record_names_its_composer_and_reads_older_records_without_one(self) -> None:
        self.assertIsNone(Environment(mode="L", planner="stub", executor="stub", agent_version="test").composer)
        dumped = Environment(mode="L", planner="stub", composer="stub", executor="engine", agent_version="test").model_dump(mode="json")
        self.assertEqual(list(dumped)[:3], ["mode", "planner", "composer"])

    def test_the_j1_record_follows_the_catalog(self) -> None:
        path = FIXTURES / "run-record.j1-complete.json"
        if not path.exists():
            self.skipTest(f"{path} is not mounted")
        record = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        self.assertEqual(record.environment.composer, "bedrock")
        for fact in record.facts:
            with self.subTest(fact=fact.id):
                # FactRecord.engine is the tool's engine (TOOLS.md 1 "Engines"), null for ORION's functions.
                self.assertEqual(fact.engine, catalog.tool_spec(fact.tool).engine)
        for call in record.calls:
            with self.subTest(call=call.call_id):
                spec = catalog.tool_spec(call.tool)
                self.assertEqual(spec.input_model.model_validate(call.request).model_dump(mode="json"), call.request)
                if call.observed is not None and spec.observed_model is not None:
                    self.assertEqual(spec.observed_model.model_validate(call.observed).model_dump(mode="json"), call.observed,
                                     "Observed is what the catalog's model writes, so responseSha256 is reproducible")
        survival = next(c for c in record.calls if c.tool == "survival_compare" and c.status == "ok")
        medians = next(c for c in record.calls if c.tool == "derive_km_median")
        assert isinstance(survival.observed, dict) and isinstance(survival.observed["groups"], list)
        curves = [KmCurve.model_validate({"label": g["label"], "points": g["curve"]}) for g in survival.observed["groups"] if isinstance(g, dict)]
        self.assertEqual(derive_km_median(curves, unit="days").model_dump(mode="json"), medians.observed, "the median is the documented rule's")


if __name__ == "__main__":
    unittest.main()
