"""Mode L end to end on the live local stack: the real app driven through POST /invocations.

What runs is what `python -m orion_agent.app` builds in mode L (`app.build_controller` from
Settings): the controller, compiler, stub planner, step engine over the InProcessToolClient (real
HTTP to the stack), fact extractor, Observed renderer, stub composer and code verifier. Only the
tool layer's HTTP transport is wrapped (`LiveTransport`): it counts requests, can hold one request
open (Stop during a step) or answer 503 for one path (retry_step); every other response is the
stack's own. Expected values come from raw HTTP written here (`Upstream`), apart from the tool code
and its golden bodies. The browser's side of Create is simulated as the contract specifies
(`SimulatedBrowser`: the FilterSet it writes, a unique name, defSha256 recomputed from the stored
filters). Every stream passes support.check_stream and keeps its fixture's shape; every run record
is read back from the run directory.

Skips cleanly when ORION_VLAB_ORIGIN is unset or the stack does not answer.
"""

import asyncio
import http.client
import json
import os
import tempfile
import unittest
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any

from bedrock_agentcore.runtime import AGUIApp
from pydantic import JsonValue
from support import FIELDS_PATH, action_body, ask_body, asgi_call, check_stream, context
from test_engine_fakes import fixture_frames, shape, stream_frames

from orion_agent import AGENT_VERSION
from orion_agent import controller as controller_module
from orion_agent.app import build_controller, create_app
from orion_agent.controller import Controller
from orion_agent.executor import Engine
from orion_agent.ids import new_id
from orion_agent.model.stub_composer import StubComposer
from orion_agent.model.verifier import CodeVerifier
from orion_agent.models import (
    CanonicalDefinition,
    CaseIdsDefinition,
    CohortContext,
    ConfirmAction,
    ConfirmPending,
    FilterDefinition,
    FilterSet,
    RunView,
    SaveCohortsArgs,
    def_sha256,
    definition_from_filter_set,
)
from orion_agent.run_store import RunRecord
from orion_agent.settings import Settings
from orion_agent.stubs import J1_NAMED, J1_OPEN, StubPlanner
from orion_agent.tools.client import Clock, InProcessToolClient, make_tool_client
from orion_agent.tools.http import H11Transport, HttpResponse

ORIGIN = os.environ.get("ORION_VLAB_ORIGIN", "")
# The reference prompts as the Assistant's starters send them (the fork's REFERENCE_PROMPTS).
J2_OPEN = "Top 3 most frequently mutated genes in Black or African American patients"
J2_NAMED = (
    "Use Cohort Builder to build a cohort of Black or African American patients, then show its top 3 most frequently "
    "mutated genes in OncoMatrix"
)
J3_OPEN = "Differential expression between patients with and without a KRAS mutation, top 10 genes"
J3_NAMED = (
    "Use the Gen3 Analysis API to find patients with and without a KRAS mutation, then run Differential Expression "
    "between them and show the top 10 genes"
)
CGS = "cgs_risk_key_criteria.cgs_risk_category"
RACE = "demographic.race"
KRAS_GENE_ID = "ENSG00000133703"
SURVIVAL = "/analysis/v0/survival/"
_REACHABLE: bool | None = None


# --- The stack, read apart from the tool code ---------------------------------------------------------


class Upstream:
    """Raw HTTP to the stack with its own requests (http.client), for the values a run must report."""

    PAGE = 1000

    def __init__(self, origin: str) -> None:
        parts = urllib.parse.urlsplit(origin)
        self.host, self.port = parts.hostname or "", parts.port or 80

    def request(self, method: str, path: str, body: JsonValue = None, timeout: float = 60.0) -> Any:  # Any: JSON
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            payload = None if body is None else json.dumps(body).encode()
            headers = {"Accept": "application/json", **({"Content-Type": "application/json"} if payload else {})}
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read()
        finally:
            conn.close()
        if response.status != 200:
            raise AssertionError(f"{method} {path}: HTTP {response.status}")
        return json.loads(data)

    def cases(self, flt: JsonValue, fields: Sequence[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.request("POST", "/analysis/v0/cases/", {"filter": flt, "fields": list(fields), "size": self.PAGE, "offset": offset})
            rows.extend(page["data"])
            offset += self.PAGE
            if offset >= int(page["pagination"]["total"]):
                return rows

    def case_ids(self, *clauses: tuple[str, list[str]]) -> set[str]:
        flt: JsonValue = {"and": [{"in": {field: list[JsonValue](values)}} for field, values in clauses]}
        return {str(row["case_id"]) for row in self.cases(flt, ["case_id"])}

    def events(self, field: str, value: str) -> int:
        rows = self.cases({"and": [{"in": {field: [value]}}]}, ["case_id", "demographic.vital_status", "demographic.days_to_death"])
        return sum(1 for r in rows if (r.get("demographic") or {}).get("vital_status") == "Dead"
                   and (r.get("demographic") or {}).get("days_to_death") is not None)

    def gene_cases(self, gene_id: str) -> set[str]:
        body: JsonValue = {"cohort_filter": {"and": []}, "gene_filter": {"and": [{"in": {"gene_id": [gene_id]}}]}, "ssm_filter": {}}
        return {str(i) for i in self.request("POST", "/analysis/v0/genomic/gene_cases", body)}

    def survival_p(self, field: str, values: Sequence[str]) -> float:
        answer = self.request("POST", SURVIVAL, {"filters": [{"and": [{"in": {field: [v]}}]} for v in values]})
        return float(answer["overallStats"]["pValue"])


def stack_reachable() -> bool:
    global _REACHABLE
    if _REACHABLE is None:
        try:
            _REACHABLE = bool(ORIGIN) and isinstance(Upstream(ORIGIN).request("GET", "/analysis/v0/_version", timeout=3.0), dict)
        except (OSError, AssertionError, ValueError, http.client.HTTPException):
            _REACHABLE = False
    return _REACHABLE


# --- The wrapped transport and the simulated browser ----------------------------------------------------


class LiveTransport:
    """tools.http.HttpTransport over the real H11Transport. It records every request, can hold the
    requests `hold` matches open until they are cancelled, and can answer 503 for a path `fail` times."""

    def __init__(self) -> None:
        self.inner = H11Transport()
        self.requests: list[tuple[str, str]] = []
        self.hold: Callable[[str, str], bool] | None = None
        self.held = asyncio.Event()
        self.fail: dict[str, int] = {}

    async def send(
        self, method: str, url: str, headers: Sequence[tuple[str, str]], body: bytes | None, *, timeout_s: float, max_bytes: int
    ) -> HttpResponse:
        path = urllib.parse.urlsplit(url).path
        self.requests.append((method, path))
        if self.hold is not None and self.hold(method, path):
            self.held.set()
            await asyncio.Event().wait()  # a slow upstream: only a cancel ends it
        if self.fail.get(path, 0) > 0:
            self.fail[path] -= 1
            return HttpResponse(status=503, headers=(("content-type", "application/json"),), body=b'{"error": "unavailable"}')
        return await self.inner.send(method, url, headers, body, timeout_s=timeout_s, max_bytes=max_bytes)


def filter_set(definition: CanonicalDefinition) -> FilterSet:
    """The FilterSet the browser writes for a definition (contract "save_cohorts tool call")."""
    if isinstance(definition, CaseIdsDefinition):
        root: dict[str, dict[str, JsonValue]] = {
            "case_id": {"operator": "in", "field": "case_id", "operands": list(sorted(definition.case_ids))}
        }
        return FilterSet(mode="and", root=root)
    assert isinstance(definition, FilterDefinition)
    by_field: dict[str, list[dict[str, JsonValue]]] = {}
    for clause in definition.clauses:
        if clause.op == "in":
            op: dict[str, JsonValue] = {"operator": "includes", "field": clause.field, "operands": list(clause.values)}
        else:
            op = {"operator": clause.op, "field": clause.field, "operand": clause.values[0]}
        by_field.setdefault(clause.field, []).append(op)
    root = {
        field: ops[0] if len(ops) == 1 else {"operator": "and", "operands": list[JsonValue](ops)} for field, ops in by_field.items()
    }
    return FilterSet(mode="and", root=root)


class SimulatedBrowser:
    """The Assistant's cohort store as Create uses it: reuse by defSha256, the FilterSet written for
    each new cohort, a name made unique (" (2)"), and the read-back's defSha256 recomputed from the
    stored filters. Its saved cohorts go into the next ask's cohortContext."""

    def __init__(self) -> None:
        self.saved: list[dict[str, JsonValue]] = []

    def add(self, name: str, definition: CanonicalDefinition) -> str:
        cohort_id = f"cohort-{len(self.saved) + 1:03d}"
        self.saved.append({"id": cohort_id, "name": name, "filters": filter_set(definition).model_dump(mode="json"), "caseCount": None})
        return cohort_id

    def _unique(self, name: str) -> str:
        taken = {str(s["name"]) for s in self.saved}
        candidate, n = name, 2
        while candidate in taken:
            candidate, n = f"{name} ({n})", n + 1
        return candidate

    def stored_sha(self, cohort: Mapping[str, JsonValue]) -> str | None:
        definition = definition_from_filter_set(FilterSet.model_validate(cohort["filters"]))
        return def_sha256(definition) if definition is not None else None

    def create(self, pending: ConfirmPending, make_active: str | None = None) -> dict[str, JsonValue]:
        saved: list[JsonValue] = []
        for cohort in pending.cohorts:
            assert cohort.definition is not None
            match = next((s for s in self.saved if self.stored_sha(s) == cohort.def_sha256), None)
            if match is None:
                self.add(self._unique(cohort.name), cohort.definition)
                match = self.saved[-1]
            saved.append({"ref": cohort.ref, "cohortId": match["id"], "name": match["name"], "defSha256": self.stored_sha(match),
                          "reused": match is not self.saved[-1] or cohort.reuse is not None})
        return {"action": "confirm", "nonce": pending.nonce, "decision": "create", "saved": saved, "makeActive": make_active}

    def context(self) -> dict[str, JsonValue]:
        ctx = context().model_dump(mode="json")
        ctx["saved"] = list(self.saved)
        return ctx


# --- The case ------------------------------------------------------------------------------------------


@unittest.skipUnless(stack_reachable(), f"the local stack is not reachable at ORION_VLAB_ORIGIN={ORIGIN or '(unset)'}")
class LiveCase(unittest.IsolatedAsyncioTestCase):
    """A fresh run directory, an empty run registry and the mode L app per test."""

    def setUp(self) -> None:
        controller_module.reset_registry()
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        self.upstream = Upstream(ORIGIN)
        self.browser = SimulatedBrowser()

    async def asyncTearDown(self) -> None:
        for handle in list(controller_module.RUNS.values()):
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()
        controller_module.reset_registry()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def settings(self) -> Settings:
        return Settings(run_dir=self.run_dir, fields_path=FIELDS_PATH, vlab_origin=ORIGIN)

    def build(self, *, wrapped: bool = True) -> AGUIApp:
        """app.build_controller as `python -m orion_agent.app` runs it; `wrapped` passes LiveTransport to the
        real make_tool_client (no backoff sleep), so requests can be counted, held or failed."""
        self.transport = LiveTransport()

        def importer(name: str) -> ModuleType:
            module = __import__(name, fromlist=["_"])
            if not wrapped or name != "orion_agent.tools.client":
                return module
            proxy = ModuleType(name)
            fast = Clock(sleep=lambda _s: asyncio.sleep(0))
            proxy.make_tool_client = partial(make_tool_client, http=self.transport, clock=fast)  # type: ignore[attr-defined]
            return proxy

        self.controller: Controller = build_controller(self.settings(), importer=importer)
        return create_app(self.controller)

    async def turn(self, app: AGUIApp, body: dict[str, JsonValue], run_id: str) -> list[dict[str, Any]]:
        response = await asgi_call(app, "POST", "/invocations", body)
        self.assertEqual(response.status, 200)
        check_stream(self, response.text, expect_run_id=run_id)
        return stream_frames(response.text)

    def ask(self, thread_id: str, run_id: str, question: str) -> dict[str, JsonValue]:
        body = ask_body(thread_id, run_id, question=question)
        props = body["forwardedProps"]
        assert isinstance(props, dict) and isinstance(props["orion"], dict)
        props["orion"]["cohortContext"] = self.browser.context()
        return body

    def view(self, frames: Sequence[Mapping[str, Any]]) -> RunView:
        return RunView.model_validate([f for f in frames if f["type"] == "STATE_SNAPSHOT"][-1]["snapshot"])

    def facts(self, view: RunView) -> dict[str, JsonValue]:
        return {f.id: f.value for f in view.facts}

    def pending(self, frames: Sequence[Mapping[str, Any]]) -> ConfirmPending:
        """The save pause's payload as the browser reads it: the save_cohorts TOOL_CALL_ARGS, equal to the snapshot's pending."""
        args = SaveCohortsArgs.model_validate_json("".join(f["delta"] for f in frames if f["type"] == "TOOL_CALL_ARGS"))
        view = self.view(frames)
        assert isinstance(view.pending, ConfirmPending)
        self.assertEqual((args.nonce, [c.ref for c in args.cohorts]), (view.pending.nonce, [c.ref for c in view.pending.cohorts]))
        for cohort in args.cohorts:
            assert cohort.definition is not None
            self.assertEqual(self.browser.stored_sha({"filters": filter_set(cohort.definition).model_dump(mode="json")}), cohort.def_sha256,
                             "the browser's defSha256 of the FilterSet it writes equals the agent's")
        return ConfirmPending.model_validate(args.model_dump(mode="json"))

    def record(self, run_id: str, view: RunView | None = None) -> RunRecord:
        """The run record on disk: valid, in its thread's index, written by this mode L wiring; `view` is its last view."""
        path = self.run_dir / "runs" / f"{run_id}.json"
        self.assertTrue(path.is_file(), f"no run record at {path}")
        record = RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        env = record.environment
        self.assertEqual((env.mode, env.planner, env.composer, env.executor, env.tool_transport, env.agent_version, env.faults),
                         ("L", "stub", "stub", "engine", "in_process", AGENT_VERSION, []))
        index = json.loads((self.run_dir / "threads" / f"{record.thread_id}.json").read_text(encoding="utf-8"))
        self.assertIn(run_id, index["runIds"])
        if view is not None:
            self.assertEqual((record.status, record.revision), (view.status, view.revision))
            self.assertEqual(record.run_view, view)
        for call in record.calls:
            if call.transport == "in_process" and call.status in ("ok", "empty") and call.tool not in (
                "set_operation", "check_subset", "rank_top_n", "derive_km_median"
            ):
                self.assertTrue(call.provenance, f"{call.call_id} keeps the HTTP requests it made")
        return record

    def assertComplete(self, frames: Sequence[Mapping[str, Any]], launcher: str) -> RunView:
        """Complete with all four answer parts, the live viz card and the answer's TEXT_MESSAGE."""
        view = self.view(frames)
        self.assertEqual((view.status, view.code, frames[-1]["type"]), ("Complete", None, "RUN_FINISHED"))
        self.assertTrue(view.observed, "Observed")
        self.assertTrue(view.limitations, "Limitations")
        self.assertTrue([s for s in view.interpretation or [] if s.dropped is None], "Interpretation")
        self.assertTrue(view.hypotheses, "Hypotheses")
        self.assertTrue(view.follow_ups or view.open_links, "next steps")
        self.assertIsNone(view.withheld_reason)
        self.assertEqual([v.launcher for v in view.viz_specs], [launcher])
        spec = view.viz_specs[0]
        self.assertEqual(spec.request_pinned_hash, spec.pinned_hash, "the card pins what the cited call sent")
        cited = {s.fact_id for part in (*view.observed, *(view.interpretation or []), *(view.hypotheses or [])) for s in part.segments if s.fact_id}
        self.assertLessEqual(cited, set(self.facts(view)), "every number on screen is a fact")
        message = "".join(f["delta"] for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT")
        self.assertIn("**Observed**", message)
        self.assertIn("**Interpretation**", message)
        return view

    def assertShape(self, frames: Sequence[Mapping[str, Any]], fixture: str, refs: Mapping[str, str] | None = None) -> None:
        """The stream's shape equals the fixture's; `refs` renames this run's cohort refs to the fixture's own
        (the j2 fixtures ask about the CGS high-risk cohort, the reference prompt about the race cohort)."""
        live = shape(frames)
        if refs:
            live = [
                (*item[:11], tuple((refs.get(ref, ref), *rest) for ref, *rest in item[11]), *item[12:]) if item[0] == "STATE_SNAPSHOT" else item
                for item in live
            ]
        self.assertEqual(live, shape(fixture_frames(fixture)), f"the stream keeps the {fixture} fixture's shape")


class WiringTests(unittest.TestCase):
    """What app.build_controller builds in mode L by default (no network: construction makes no call)."""

    def test_mode_l_defaults_are_in_process_tools_the_stubs_and_the_code_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(run_dir=Path(tmp), fields_path=FIELDS_PATH)
            self.assertEqual((settings.mode, settings.planner, settings.composer, settings.executor, settings.tools),
                             ("L", "stub", "stub", "engine", "inprocess"))
            controller = build_controller(settings)
        assert isinstance(controller.executor, Engine)
        self.assertIsInstance(controller.executor.tools, InProcessToolClient)
        self.assertEqual(controller.executor.tools.transport, "in_process")
        self.assertIsInstance(controller.planner, StubPlanner)
        self.assertIsInstance(controller.composer, StubComposer)
        self.assertIsInstance(controller.verifier, CodeVerifier)
        self.assertIs(controller.vocab, controller.executor.vocab)


class JourneyTests(LiveCase):
    async def test_j1_both_phrasings_reach_complete_with_the_live_numbers(self) -> None:
        """E1, E2: the same plan signature; n, events and the log-rank p equal the stack's; the survival card."""
        app = self.build(wrapped=False)
        thread_id = new_id()
        high = await asyncio.to_thread(self.upstream.case_ids, (CGS, ["High risk"]))
        standard = await asyncio.to_thread(self.upstream.case_ids, (CGS, ["Standard risk"]))
        events = (await asyncio.to_thread(self.upstream.events, CGS, "High risk"),
                  await asyncio.to_thread(self.upstream.events, CGS, "Standard risk"))
        p_value = await asyncio.to_thread(self.upstream.survival_p, CGS, ["High risk", "Standard risk"])
        signatures = []
        for question in (J1_OPEN, J1_NAMED):
            run_id = new_id()
            frames = await self.turn(app, self.ask(thread_id, run_id, question), run_id)
            self.assertShape(frames, "j1-complete")
            view = self.assertComplete(frames, "survival")
            facts = self.facts(view)
            self.assertEqual((facts["f.s2.count"], facts["f.s3.count"]), (len(high), len(standard)))
            self.assertEqual((facts["f.s4.events.high"], facts["f.s4.events.standard"]),
                             events)
            self.assertEqual(facts["f.s4.p"], p_value)
            assert view.plan is not None
            signatures.append(view.plan.signature)
            record = self.record(run_id, view)
            self.assertEqual([c.tool for c in record.calls],
                             ["fixture_fingerprint", "cohort_count", "cohort_count", "survival_compare", "derive_km_median"])
            self.assertIsNotNone(record.environment.fingerprint_sha256)
            self.assertEqual([m.model for m in record.model_calls], ["stub"])
        self.assertEqual(signatures[0], signatures[1], "both phrasings sign the same plan")

    async def test_j1_in_the_active_race_cohort_counts_the_resolved_scope(self) -> None:
        """E14: with the J2 race cohort active, the scope line and every count are the race cohort's, and the
        groups, medians and survival card render (the scope's cases_query is not a group)."""
        app = self.build(wrapped=False)
        race = FilterDefinition.model_validate({"kind": "filter", "clauses": [{"field": RACE, "op": "in", "values": ["black or african american"]}]})
        cohort_id = self.browser.add("Black or African American (ORION)", race)
        members = await asyncio.to_thread(self.upstream.case_ids, (RACE, ["black or african american"]))
        high = await asyncio.to_thread(self.upstream.case_ids, (RACE, ["black or african american"]), (CGS, ["High risk"]))
        standard = await asyncio.to_thread(self.upstream.case_ids, (RACE, ["black or african american"]), (CGS, ["Standard risk"]))
        ctx = self.browser.context()
        ctx["active"] = {"id": cohort_id, "name": "Black or African American (ORION)", "filters": self.browser.saved[0]["filters"],
                         "caseCount": len(members)}
        run_id = new_id()
        body = ask_body(new_id(), run_id, question=J1_OPEN, cohort_context=CohortContext.model_validate(ctx))
        frames = await self.turn(app, body, run_id)
        view = self.assertComplete(frames, "survival")
        assert view.plan is not None
        self.assertEqual((view.plan.scope.source, view.plan.scope.name, view.plan.scope.case_count),
                         ("active_cohort", "Black or African American (ORION)", len(members)))
        facts = self.facts(view)
        self.assertEqual((facts["f.s2.count"], facts["f.s3.count"], facts["f.s4.count"]), (len(members), len(high), len(standard)))
        texts = ["".join(seg.text for seg in o.segments) for o in view.observed]
        self.assertTrue(texts[0].startswith(f"High risk: {len(high)} cases"), texts)
        self.record(run_id, view)

    async def test_j2_pauses_for_one_cohort_then_completes_after_create(self) -> None:
        """E3, E4: one confirmation saves the race cohort; the counts equal the stack's; the oncomatrix card."""
        app = self.build()
        signatures = []
        for question in (J2_NAMED, J2_OPEN):
            # A paused run holds its thread (an ask there is RUN_BUSY), so each phrasing gets its own thread.
            thread_id, run_id = new_id(), new_id()
            frames = await self.turn(app, self.ask(thread_id, run_id, question), run_id)
            assert (plan := self.view(frames).plan) is not None
            signatures.append(plan.signature)
        self.assertEqual(signatures[0], signatures[1], "both phrasings sign the same plan")
        self.assertShape(frames, "j2-save-pause", refs={"black": "high"})
        pending = self.pending(frames)
        [cohort] = pending.cohorts
        self.assertEqual((cohort.ref, cohort.name, cohort.eligible_count), ("black", "Black or African American (ORION)", None))
        self.assertEqual(cohort.definition, FilterDefinition.model_validate(
            {"kind": "filter", "clauses": [{"field": RACE, "op": "in", "values": ["black or african american"]}]}))
        members = await asyncio.to_thread(self.upstream.case_ids, (RACE, ["black or african american"]))
        self.assertEqual(cohort.case_count, len(members))
        self.assertEqual(self.record(run_id).status, "AwaitingConfirmation")
        before = len(self.transport.requests)
        frames = await self.turn(app, action_body(thread_id, run_id, self.browser.create(pending)), run_id)
        self.assertShape(frames, "j2-after-confirm", refs={"black": "high"})
        self.assertEqual(len(self.transport.requests), before, "the reads are not run again after Create")
        view = self.assertComplete(frames, "oncomatrix")
        facts = self.facts(view)
        self.assertEqual(facts["f.s2.count"], len(members))
        kras = len(await asyncio.to_thread(self.upstream.gene_cases, KRAS_GENE_ID) & members)
        self.assertEqual((facts["f.s4.top.1.gene"], facts["f.s4.top.1.cases"]), ("KRAS", kras))
        saved = next(c for c in view.cohorts if c.ref == "black")
        self.assertEqual((saved.save, saved.cohort_id, saved.saved_name), ("saved", self.browser.saved[0]["id"], cohort.name))
        record = self.record(run_id, view)
        self.assertEqual([c.outcome for c in record.confirmations], ["accepted"])
        write = next(c for c in record.calls if c.tool == "save_cohorts")
        self.assertEqual(write.transport, "client")

    async def test_j3_pauses_then_runs_de_on_the_read_back_names(self) -> None:
        """E5, E6: group sizes equal the stack's set arithmetic; eligible counts on the card before Create; DE
        runs on the cohorts as the browser saved them (a name made unique) and ends Complete."""
        app = self.build()
        # A cohort of that name already exists with another definition: the browser makes the new name unique.
        self.browser.add("KRAS mutated (ORION)", FilterDefinition.model_validate(
            {"kind": "filter", "clauses": [{"field": RACE, "op": "in", "values": ["asian"]}]}))
        signatures = []
        for question in (J3_NAMED, J3_OPEN):
            thread_id, run_id = new_id(), new_id()
            frames = await self.turn(app, self.ask(thread_id, run_id, question), run_id)
            assert (plan := self.view(frames).plan) is not None
            signatures.append(plan.signature)
        self.assertEqual(signatures[0], signatures[1], "both phrasings sign the same plan")
        self.assertShape(frames, "j3-save-pause")
        pending = self.pending(frames)
        tested = await asyncio.to_thread(self.upstream.case_ids, ("available_variation_data", ["ssm"]))
        mutated = await asyncio.to_thread(self.upstream.gene_cases, KRAS_GENE_ID) & tested
        by_ref = {c.ref: c for c in pending.cohorts}
        self.assertEqual(sorted(by_ref), ["kras_mut", "kras_wt"])
        self.assertEqual(by_ref["kras_mut"].definition, CaseIdsDefinition(kind="case_ids", case_ids=sorted(mutated)))
        self.assertEqual(by_ref["kras_wt"].definition, CaseIdsDefinition(kind="case_ids", case_ids=sorted(tested - mutated)))
        view = self.view(frames)
        facts = self.facts(view)
        self.assertEqual((facts["f.s3.count"], facts["f.s5.count"]), (len(tested), len(tested - mutated)))
        for cohort in pending.cohorts:
            self.assertIsNotNone(cohort.eligible_count, "eligible counts appear on the card before Create")
            self.assertEqual(facts[str(cohort.eligible_count_fact_id)], cohort.eligible_count)
        confirm = self.browser.create(pending)
        read_back = ConfirmAction.model_validate(confirm).saved
        names, ids = {s.ref: s.name for s in read_back}, {s.ref: s.cohort_id for s in read_back}
        self.assertEqual(names["kras_mut"], "KRAS mutated (ORION) (2)")
        frames = await self.turn(app, action_body(thread_id, run_id, confirm), run_id)
        self.assertShape(frames, "j3-after-confirm")
        view = self.assertComplete(frames, "deDirect")
        record = self.record(run_id, view)
        de = next(c for c in record.calls if c.tool == "de_run")
        assert isinstance(de.request, dict) and isinstance(de.request["groups"], list)
        groups = [g for g in de.request["groups"] if isinstance(g, dict)]
        self.assertEqual([(g["name"], g["cohortId"]) for g in groups],
                         [(names["kras_wt"], ids["kras_wt"]), (names["kras_mut"], ids["kras_mut"])])
        observed = " ".join("".join(s.text for s in o.segments) for o in view.observed)
        self.assertIn("KRAS mutated (ORION) (2)", observed, "Observed names the groups as they were saved")
        facts = self.facts(view)
        tested_genes, significant = facts["f.s9.tested"], facts["f.s9.significant"]
        assert isinstance(tested_genes, int) and isinstance(significant, int)
        self.assertTrue(0 < significant <= tested_genes)
        padj = [v for i in range(1, 11) if isinstance(v := facts[f"f.s10.top.{i}.padj"], float)]
        self.assertEqual(len(padj), 10)
        self.assertEqual(padj, sorted(padj), "the top 10 is ranked by adjusted p")


class ActionTests(LiveCase):
    async def test_dont_save_runs_de_on_the_proposed_names_and_saves_nothing(self) -> None:
        app = self.build()
        thread_id, run_id = new_id(), new_id()
        pending = self.pending(await self.turn(app, self.ask(thread_id, run_id, J3_OPEN), run_id))
        body: dict[str, JsonValue] = {"action": "confirm", "nonce": pending.nonce, "decision": "dont_save", "saved": [], "makeActive": None}
        frames = await self.turn(app, action_body(thread_id, run_id, body), run_id)
        self.assertShape(frames, "j3-dont-save")
        view = self.view(frames)
        self.assertIn("WRITE_SKIPPED", [lim.code for lim in view.limitations])
        refs = {c.ref for c in pending.cohorts}
        self.assertEqual({(c.ref, c.save, c.cohort_id) for c in view.cohorts if c.ref in refs},
                         {("kras_mut", "skipped", None), ("kras_wt", "skipped", None)})
        record = self.record(run_id, view)
        de = next(c for c in record.calls if c.tool == "de_run")
        assert isinstance(de.request, dict) and isinstance(de.request["groups"], list)
        self.assertEqual([g["name"] for g in de.request["groups"] if isinstance(g, dict)], ["KRAS not detected (ORION)", "KRAS mutated (ORION)"])
        self.assertTrue(all("cohortId" not in g for g in de.request["groups"] if isinstance(g, dict)))
        self.assertEqual([(c.decision, c.outcome) for c in record.confirmations], [("dont_save", "accepted")])
        self.assertEqual(self.browser.saved, [], "nothing was saved")

    async def test_stop_during_a_step_aborts_the_request_and_ends_cancelled(self) -> None:
        """E24: Stop while survival_compare's request is in flight: Cancelled, the finished steps kept, the
        aborted attempt recorded with the request it had sent."""
        app = self.build()
        self.transport.hold = lambda method, path: path == SURVIVAL
        thread_id, run_id = new_id(), new_id()
        ask = asyncio.create_task(asgi_call(app, "POST", "/invocations", self.ask(thread_id, run_id, J1_OPEN)))
        await asyncio.wait_for(self.transport.held.wait(), 30)
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "cancel"}), run_id)
        self.assertShape(frames, "j1-stop")
        check_stream(self, (await ask).text, expect_run_id=run_id)
        view = self.view(frames)
        self.assertEqual((view.status, view.withheld_reason), ("Cancelled", "you stopped the run"))
        self.assertEqual([s.status for s in view.steps[:3]], ["Done", "Done", "Done"], "finished steps are kept")
        record = self.record(run_id, view)
        [aborted] = [c for c in record.calls if c.step_id == "s4"]
        self.assertEqual((aborted.status, aborted.http_status), ("error", None))
        self.assertTrue(str(aborted.error).startswith("CANCELLED"))
        self.assertEqual([(p.method, p.path, p.http_status) for p in aborted.provenance], [("POST", SURVIVAL, None)],
                         "the in-flight request is in the aborted attempt's provenance")
        self.assertIsNotNone(view.steps[3].duration_ms)
        self.assertIsNone(record.lease)

    async def test_retry_step_reruns_a_failed_step_to_complete(self) -> None:
        """E11's failure path: survival_compare answers 503 on both attempts (Partial); retry_step reruns it to Complete."""
        app = self.build()
        self.transport.fail[SURVIVAL] = 2
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, self.ask(thread_id, run_id, J1_OPEN), run_id)
        self.assertShape(frames, "j1-partial")
        view = self.view(frames)
        self.assertEqual((view.status, view.code, view.withheld_reason), ("Partial", "TOOL_FAILED", "a step failed"))
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "retry_step", "stepId": "s4"}), run_id)
        self.assertShape(frames, "j1-retry")
        view = self.assertComplete(frames, "survival")
        record = self.record(run_id, view)
        s4 = [(c.call_id.rsplit(".", 1)[1], c.attempt, c.status, [p.http_status for p in c.provenance]) for c in record.calls if c.step_id == "s4"]
        self.assertEqual(s4[:2], [("c1", 1, "error", [503]), ("c2", 2, "error", [503])], "both failed attempts keep their request")
        self.assertEqual((s4[2][0], s4[2][1], s4[2][2]), ("c3", 1, "ok"))
        self.assertEqual([a.action for a in record.actions], ["ask", "retry_step"])

    async def test_an_edit_at_the_save_pause_voids_the_nonce_and_create_completes_the_new_version(self) -> None:
        app = self.build()
        thread_id, run_id = new_id(), new_id()
        first = self.pending(await self.turn(app, self.ask(thread_id, run_id, J3_OPEN), run_id))
        reads = len(self.transport.requests)
        edit: dict[str, JsonValue] = {"action": "edit_plan", "field": "cohortName", "value": "KRAS wild-type (ORION)", "ref": "kras_wt"}
        frames = await self.turn(app, action_body(thread_id, run_id, edit), run_id)
        self.assertShape(frames, "j3-edit-at-pause")
        self.assertEqual(len(self.transport.requests), reads, "the finished reads are Reused, not sent again")
        second = self.pending(frames)
        self.assertNotEqual(second.nonce, first.nonce)
        self.assertEqual((second.plan_version, [c.name for c in second.cohorts]), (2, ["KRAS wild-type (ORION)", "KRAS mutated (ORION)"]))
        stale = await asgi_call(app, "POST", "/invocations", action_body(thread_id, run_id, self.browser.create(first)))
        self.assertEqual(stream_frames(stale.text)[-1]["code"], "STALE_ACTION")
        self.browser.saved.clear()
        frames = await self.turn(app, action_body(thread_id, run_id, self.browser.create(second)), run_id)
        view = self.assertComplete(frames, "deDirect")
        record = self.record(run_id, view)
        self.assertEqual([(c.nonce, c.outcome) for c in record.confirmations], [(first.nonce, "voided"), (second.nonce, "accepted")])
        de = next(c for c in record.calls if c.tool == "de_run")
        assert isinstance(de.request, dict) and isinstance(de.request["groups"], list)
        self.assertEqual([g["name"] for g in de.request["groups"] if isinstance(g, dict)], ["KRAS wild-type (ORION)", "KRAS mutated (ORION)"])

    async def test_a_resent_confirm_replays_the_outcome_and_runs_nothing(self) -> None:
        """E19: the confirm is sent again after the run finished: no write, no call, the same Complete."""
        app = self.build()
        thread_id, run_id = new_id(), new_id()
        pending = self.pending(await self.turn(app, self.ask(thread_id, run_id, J2_OPEN), run_id))
        confirm = self.browser.create(pending)
        await self.turn(app, action_body(thread_id, run_id, confirm), run_id)
        before = (len(self.transport.requests), self.record(run_id).revision, len(self.browser.saved))
        again = self.browser.create(pending)
        self.assertEqual(again["saved"], confirm["saved"], "the browser reuses what it saved: no duplicate")
        frames = await self.turn(app, action_body(thread_id, run_id, again), run_id)
        self.assertEqual([f["type"] for f in frames], ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"])
        self.assertEqual(frames[-1]["result"], {"status": "Complete", "code": None})
        record = self.record(run_id)
        self.assertEqual((len(self.transport.requests), record.revision, len(self.browser.saved)), before)
        self.assertEqual(record.actions[-1].outcome, "repeat")

    async def test_clarify_asks_once_then_plans_on_the_answer(self) -> None:
        """E10: one clarifying question with field options and no data call; the answer runs to Complete."""
        app = self.build()
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, self.ask(thread_id, run_id, "Compare survival of high-risk patients with everyone else"), run_id)
        self.assertShape(frames, "clarify")
        view = self.view(frames)
        self.assertEqual(self.transport.requests, [], "no data call before the answer")
        assert view.pending is not None and view.pending.kind == "clarify"
        self.assertIn(CGS, [o.field for o in view.pending.options])
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "clarify_answer", "optionId": CGS}), run_id)
        view = self.assertComplete(frames, "survival")
        self.assertEqual(self.record(run_id, view).plan_history[-1].cause, "clarify_answer")

    async def test_consent_substitutes_the_tool_only_after_the_researcher_accepts(self) -> None:
        """E7 and E24's declined substitution: no data call before consent; accept runs Correlation Plot; decline stops."""
        app = self.build()
        thread_id = new_id()
        for accept in (True, False):
            run_id = new_id()
            frames = await self.turn(app, self.ask(thread_id, run_id, "Use OncoMatrix to plot survival by CGS risk category"), run_id)
            self.assertShape(frames, "consent")
            view = self.view(frames)
            assert view.pending is not None and view.pending.kind == "consent"
            self.assertEqual((view.pending.named.app_id, view.pending.proposed.app_id), ("OncoMatrix", "Correlation"))
            self.assertEqual(self.transport.requests, [], "no data call before consent")
            frames = await self.turn(app, action_body(thread_id, run_id, {"action": "consent", "accept": accept}), run_id)
            if accept:
                self.assertComplete(frames, "survival")
                self.transport.requests.clear()
            else:
                view = self.view(frames)
                self.assertEqual(view.status, "Cancelled")
                self.assertEqual(self.transport.requests, [], "a declined substitution makes no data call")
                self.record(run_id, view)

    async def test_every_follow_up_chip_runs_in_its_thread(self) -> None:
        """C-06 in mode L: each chip the stub composer offers on J1 and J2, sent verbatim as the click does, plans its own
        kind and reaches Complete (J1's "Top mutated genes in this cohort", J2's "Survival by <gene> mutation")."""
        app = self.build()
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, self.ask(thread_id, run_id, J1_OPEN), run_id)
        chips = self.assertComplete(frames, "survival").follow_ups
        genes_chip = next(f for f in chips if f.kind == "top_mutated_genes")
        chip_run = new_id()
        frames = await self.turn(app, self.ask(thread_id, chip_run, genes_chip.question), chip_run)
        self.assertComplete(frames, "oncomatrix")
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, self.ask(thread_id, run_id, J2_OPEN), run_id)
        frames = await self.turn(app, action_body(thread_id, run_id, self.browser.create(self.pending(frames))), run_id)
        chips = self.assertComplete(frames, "oncomatrix").follow_ups
        survival_chip = next(f for f in chips if f.kind == "survival_compare")
        self.assertRegex(survival_chip.question, r"^Compare survival between [A-Z0-9]+-mutated and other mutation-tested cases$")
        chip_run = new_id()
        frames = await self.turn(app, self.ask(thread_id, chip_run, survival_chip.question), chip_run)
        view = self.view(frames)
        self.assertEqual((view.status, [s.tool for s in view.steps if s.tool == "survival_compare"]), ("Complete", ["survival_compare"]))

    async def test_a_gene_population_is_never_dropped_and_high_risk_patients_asks_once(self) -> None:
        """E8's second phrasing ends OUT_OF_PANEL with no data call; FR-04's "High-risk patients" asks one question."""
        app = self.build()
        thread_id, run_id = new_id(), new_id()
        frames = await self.turn(app, self.ask(thread_id, run_id, "Top mutated genes in IDH1-mutated patients"), run_id)
        view = self.view(frames)
        self.assertEqual((view.status, view.code, view.facts), ("Limited", "OUT_OF_PANEL", []))
        self.assertEqual(self.transport.requests, [], "no data call")
        run_id = new_id()
        frames = await self.turn(app, self.ask(thread_id, run_id, "High-risk patients"), run_id)
        view = self.view(frames)
        assert view.pending is not None and view.pending.kind == "clarify"
        self.assertEqual([o.field for o in view.pending.options], [CGS, "diagnoses.iss_stage"])
        self.assertEqual(self.transport.requests, [], "no data call before the answer")
        frames = await self.turn(app, action_body(thread_id, run_id, {"action": "clarify_answer", "optionId": CGS}), run_id)
        self.assertEqual(self.view(frames).status, "Complete")

    async def test_unsupported_and_out_of_panel_questions_make_no_data_call(self) -> None:
        """E17, E18, E23 end Unsupported and E8 Limited (OUT_OF_PANEL), each with no data call and no number."""
        app = self.build()
        thread_id = new_id()
        cases = [
            ("Which drug should a KRAS-mutated patient get?", "Unsupported", "CLINICAL_ADVICE", None),
            ("Show a Venn diagram of the two KRAS cohorts", "Unsupported", "UNSUPPORTED_ANALYSIS", "SetOperations"),
            ("Cluster the samples by their gene expression and show a heatmap", "Unsupported", "UNSUPPORTED_ANALYSIS", None),
            ("Differential expression between patients with and without an IDH1 mutation, top 10 genes", "Limited", "OUT_OF_PANEL", None),
        ]
        for question, status, code, app_id in cases:
            with self.subTest(question):
                run_id = new_id()
                frames = await self.turn(app, self.ask(thread_id, run_id, question), run_id)
                view = self.view(frames)
                self.assertEqual((view.status, view.code), (status, code))
                self.assertEqual(view.facts, [])
                self.assertTrue(all(not any(s.fact_id for s in o.segments) for o in view.observed))
                if app_id is not None:
                    self.assertIn(app_id, [a.app_id for lim in view.limitations for a in lim.nearest_apps])
                self.record(run_id, view)
        self.assertEqual(self.transport.requests, [], "no data call, not even the fingerprint")


if __name__ == "__main__":
    unittest.main()
