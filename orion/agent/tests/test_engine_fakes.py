"""Test doubles for the engine and controller tests (no TestCase here).

FakeTools is a ToolClient whose Observed follows TOOLS.md section 2 (values echo the fixtures:
339 and 661 CGS cases, 480 KRAS-mutated of 1,000 tested, 16 of 7,684 DE genes). Behaviours can be
scripted per step or tool: two timeouts, a block until cancelled, an empty result, an error.
FakePlanner, FakeComposer and FakeVerifier stand in for the model roles.
"""

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pydantic import JsonValue
from support import context, vocabulary

from orion_agent import ir
from orion_agent.compiler import CompiledPlan, Compiler, SessionLedger
from orion_agent.events import iso_utc, utc_now
from orion_agent.interfaces import (
    Clarification,
    ComposerFollowUp,
    ComposerHypothesis,
    ComposerOutput,
    ComposerResult,
    ComposerStatement,
    ObservedResult,
    PlannerResult,
    RuleFinding,
    VerifierResult,
)
from orion_agent.models import (
    CohortContext,
    FactRecord,
    FollowUp,
    ModelCallRecord,
    NearestApp,
    Segment,
    Sentence,
    canonical_json,
    fingerprint_document,
    fingerprint_sha256,
    format_fact,
    sha256_hex,
)
from orion_agent.tools.base import Attempt, RetryHook, ServerToolName, ToolCallOutcome, ToolError, ToolResult
from orion_agent.vocab import Vocabulary

CGS = "cgs_risk_key_criteria.cgs_risk_category"
J1_QUESTION = "Compare survival of CGS high-risk vs. CGS standard-risk patients"
J3_QUESTION = "Differential expression between patients with and without a KRAS mutation, top 10 genes"
J2_QUESTION = "Top 3 most frequently mutated genes in Black or African American patients"
P_VALUE = 7.1e-60
DE_GENES: list[tuple[str, float, float]] = [
    ("BRAF", 1.12, 2.1e-09), ("ETV4", 0.94, 6.8e-09), ("JUN", -0.87, 1.4e-08), ("DUSP6", 1.31, 3.9e-08),
    ("FOS", -1.02, 7.7e-08), ("TP53", -0.66, 2.6e-07), ("MAPK1", 2.17, 5.0e-07), ("DIS3", 1.88, 1.2e-06),
    ("ETV5", 1.54, 3.3e-06), ("KRAS", 0.71, 8.1e-06), ("SPRY2", 0.61, 1.1e-05), ("EGR1", -0.58, 1.9e-05),
    ("CCND1", 0.52, 2.4e-05), ("MYC", 0.49, 3.0e-05), ("IER3", -0.44, 3.7e-05), ("PHLDA1", 0.41, 4.4e-05),
]
TOP_GENES: list[tuple[str, int]] = [("KRAS", 163), ("NRAS", 121), ("DIS3", 97), ("TP53", 60)]
FP_STATUS: dict[str, JsonValue] = {"status": "OK", "synthetic": True, "cases": 1000, "csrf": "synthetic-only"}
FP_VERSION: dict[str, JsonValue] = {"version": "1.0.3"}
FP_HEALTH: dict[str, JsonValue] = {"status": "ok", "versionInfo": {"pkgver": "2.216.0", "codedate": "20261003.02:08", "launchdate": "x"}}
FP_RANKING: list[dict[str, JsonValue]] = [{"gene": "KRAS", "cases": 480}, {"gene": "NRAS", "cases": 355}]


def case_id(n: int) -> str:
    return f"SYNTH-CASE-{n:04d}"


ALL = [case_id(n) for n in range(1, 1001)]
HIGH = [case_id(n) for n in range(1, 340)]
MUTATED = [case_id(n) for n in range(1, 481)]
BLACK = [case_id(n) for n in range(1, 335)]


def compiler() -> Compiler:
    return Compiler(vocabulary(), "active_cohort")


def j1_ir() -> ir.Plan:
    return ir.Plan(
        kind="plan", analyses=[ir.SurvivalCompare(kind="survival_compare", split_term=CGS, split_values=["high risk", "standard risk"])]
    )


def j3_ir() -> ir.Plan:
    return ir.Plan.model_validate(
        {
            "kind": "plan",
            "cohorts": [
                {"ref": "tested", "def": {"kind": "mutation_tested", "within": "base"}},
                {"ref": "kras_mut", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "tested"}},
                {"ref": "kras_wt", "def": {"kind": "difference", "a": "tested", "b": "kras_mut"}},
            ],
            "analyses": [{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut", "top_n": 10}],
        }
    )


def j2_ir() -> ir.Plan:
    return ir.Plan.model_validate(
        {
            "kind": "plan",
            "cohorts": [{"ref": "black", "def": {"kind": "filter", "clauses": [{"field": "demographic.race", "op": "in", "values": ["black or african american"]}]}}],
            "analyses": [{"kind": "top_mutated_genes", "cohort": "black", "n": 3}],
        }
    )


def compile_plan(plan: ir.Plan, ctx: CohortContext | None = None) -> CompiledPlan:
    compiled = compiler().compile(plan, ctx or context())
    assert isinstance(compiled, CompiledPlan), compiled
    return compiled


# --- the fake tool client -----------------------------------------------------------------------------------

Behaviour = Literal["ok", "timeout", "block", "empty", "error", "small", "ineligible", "too_many", "shape"]


def _filter_values(value: JsonValue) -> list[str]:
    return [str(v) for v in json.dumps(value).split('"') if v]


def _ids_in(filter0: JsonValue) -> list[str]:
    text = json.dumps(filter0)
    return re.findall(r"SYNTH-CASE-\d{4}", text)


def _curve(points: Sequence[tuple[float, float]]) -> list[JsonValue]:
    return [{"time": t, "survival": s} for t, s in points]


class FakeTools:
    """A ToolClient (tools.base) over canned Observed; records every call it was asked to make."""

    def __init__(self, behaviours: Mapping[str, Behaviour] | None = None, *, transport: Literal["in_process", "gateway"] = "in_process") -> None:
        self.behaviours: dict[str, Behaviour] = dict(behaviours or {})
        self._transport = transport
        self.calls: list[tuple[str, str, JsonValue, int]] = []  # (tool, step id, input, first_call)
        self.started = asyncio.Event()
        self.cancelled: list[str] = []
        self.release = asyncio.Event()

    @property
    def transport(self) -> Literal["in_process", "gateway"]:
        return self._transport

    def behaviour(self, name: str, step_id: str) -> Behaviour:
        return self.behaviours.get(step_id) or self.behaviours.get(name) or "ok"

    async def call(
        self,
        name: ServerToolName,
        input: JsonValue,
        *,
        timeout_s: float,
        run_id: str,
        plan_version: int,
        step_id: str,
        first_call: int,
        on_retry: RetryHook | None = None,
    ) -> ToolCallOutcome:
        self.calls.append((name, step_id, input, first_call))
        behaviour = self.behaviour(name, step_id)
        request_sha = sha256_hex(canonical_json(input, portable=False))
        at = iso_utc(utc_now())

        def attempt(n: int, status: Literal["ok", "empty", "error"], error: ToolError | None = None, http: int | None = 200) -> Attempt:
            return Attempt(
                call_id=f"{run_id}.v{plan_version}.{step_id}.c{first_call + n - 1}", attempt=n, started_at=at, elapsed_ms=5,
                status=status, http_status=http, error=error, backoff_ms=0,
            )

        if behaviour == "block":
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.append(step_id)
                raise
            behaviour = "ok"
        if behaviour == "timeout":
            error = ToolError(code="TIMEOUT", message="no response within 55 s", retryable=True)
            if on_retry is not None:
                await on_retry(2, 0)
            result = ToolResult(status="error", observed=None, limitation="TOOL_FAILED", error=error)
            return ToolCallOutcome(
                tool=name, tool_version=f"{name}@1", transport=self._transport, request=input, request_sha256=request_sha, result=result,
                response_sha256=None, attempts=[attempt(1, "error", error, None), attempt(2, "error", error, None)], elapsed_ms=110,
            )
        if behaviour in ("error", "shape"):
            code: Any = "HTTP_ERROR" if behaviour == "error" else "UPSTREAM_SHAPE"
            error = ToolError(code=code, message="the gateway answered 500", http_status=500, retryable=False)
            result = ToolResult(status="error", observed=None, limitation="TOOL_FAILED", error=error)
            return ToolCallOutcome(
                tool=name, tool_version=f"{name}@1", transport=self._transport, request=input, request_sha256=request_sha, result=result,
                response_sha256=None, attempts=[attempt(1, "error", error, 500)], elapsed_ms=5,
            )
        observed, status, limitation = self.observed(name, input, behaviour)
        result = ToolResult(status=status, observed=observed, limitation=limitation)
        return ToolCallOutcome(
            tool=name, tool_version=f"{name}@1", transport=self._transport, request=input, request_sha256=request_sha, result=result,
            response_sha256=sha256_hex(canonical_json(observed, portable=False)), attempts=[attempt(1, status)], elapsed_ms=5,
        )

    def observed(self, name: str, input: JsonValue, behaviour: Behaviour) -> tuple[JsonValue, Literal["ok", "empty"], Any]:
        """Canned Observed per TOOLS.md 2; built as plain JSON (Any) and returned as JsonValue."""
        out, status, limitation = self._observed(name, input, behaviour)
        return cast(JsonValue, out), status, limitation

    def _observed(self, name: str, input: JsonValue, behaviour: Behaviour) -> tuple[Any, Literal["ok", "empty"], Any]:  # Any: canned JSON
        data: dict[str, Any] = dict(input) if isinstance(input, dict) else {}
        text = json.dumps(data)
        if name == "fixture_fingerprint":
            document = fingerprint_document(FP_STATUS, FP_VERSION, FP_HEALTH, FP_RANKING)
            return {"document": document, "fingerprintSha256": fingerprint_sha256(document)}, "ok", None
        if name == "cohort_count":
            count = 0 if behaviour == "empty" else 5 if behaviour == "small" else 339 if "High risk" in text else 661 if "Standard risk" in text else 1000
            return {"count": count}, "empty" if count == 0 else "ok", "EMPTY_COHORT" if count == 0 else None
        if name == "cases_query":
            if behaviour == "too_many":
                return {"count": 12000, "caseIds": [], "pages": 1, "complete": False}, "ok", "TOO_MANY_IDS"
            if behaviour == "empty":
                return {"count": 0, "caseIds": [], "pages": 1, "complete": True}, "empty", "EMPTY_COHORT"
            ids = HIGH if "High risk" in text else BLACK if "black" in text else _ids_in(data.get("filter")) or ALL
            return {"count": len(ids), "caseIds": sorted(ids), "pages": 1, "complete": True}, "ok", None
        if name == "gene_cases":
            mutated: list[str] = [] if behaviour == "empty" else MUTATED
            return {"gene": data.get("gene"), "count": len(mutated), "caseIds": mutated}, "empty" if not mutated else "ok", "EMPTY_COHORT" if not mutated else None
        if name == "survival_compare":
            groups: Any = data.get("groups") or []
            out: list[Any] = []
            for group in groups:
                assert isinstance(group, dict)
                label = str(group.get("label"))
                high = "High" in label
                out.append({
                    "label": label, "cases": 339 if high else 661, "events": 299 if high else 300,
                    "curve": _curve([(0, 1.0), (365, 0.8), (730, 0.6), (1095, 0.5), (1460, 0.3)] if high
                                    else [(0, 1.0), (730, 0.8), (1460, 0.6), (2190, 0.45)]),
                })
            return {"groups": out, "pValue": P_VALUE, "engine": "gen3-analysis@ffa4069"}, "ok", None
        if name == "sample_lists":
            groups = data.get("groups") if isinstance(data.get("groups"), list) else []
            assert isinstance(groups, list)
            result: list[Any] = []
            for group in groups:
                assert isinstance(group, dict)
                listed: Any = group.get("caseIds") or []
                samples: list[Any] = sorted(int(str(i).rsplit("-", 1)[1]) for i in listed)
                result.append({"key": group.get("key"), "count": len(samples), "sampleIds": samples})
            return {"groups": result}, "ok", None
        if name == "de_preanalysis":
            groups = data.get("groups") if isinstance(data.get("groups"), list) else []
            assert isinstance(groups, list)
            eligible: list[Any] = []
            for group in groups:
                assert isinstance(group, dict)
                sample_ids: Any = group.get("sampleIds")
                n = 5 if behaviour == "ineligible" else len(sample_ids) if isinstance(sample_ids, list) else 0
                eligible.append({"key": group.get("key"), "eligible": n})
            limitation = "INELIGIBLE_GROUP" if behaviour == "ineligible" else None
            return {"groups": eligible}, "ok", limitation
        if name == "de_run":
            groups = data.get("groups") if isinstance(data.get("groups"), list) else []
            assert isinstance(groups, list)
            sizes = [len(g.get("sampleIds") or []) if isinstance(g, dict) and isinstance(g.get("sampleIds"), list) else 0 for g in groups]
            genes: list[Any] = [
                {"gene_name": g, "fold_change": fc, "original_p_value": p / 10, "adjusted_p_value": p} for g, fc, p in DE_GENES
            ]
            up = sum(1 for _, fc, _ in DE_GENES if fc > 0)
            return {
                "method": data.get("method"), "sampleSizes": {"control": sizes[0], "case": sizes[1]},
                "totals": {"tested": 7684, "significant": len(genes), "up": up, "down": len(genes) - up}, "genes": genes, "cacheId": "cache-1",
            }, "ok", None
        if name == "top_mutated_genes":
            count = int(data.get("geneCount") or 3)
            rows: list[Any] = [{"gene": g, "cases": c} for g, c in TOP_GENES]
            if behaviour == "empty":
                return {"genes": []}, "empty", "FEWER_THAN_N"
            out_genes: dict[str, Any] = {"genes": rows[:count]}
            if len(rows) > count:
                out_genes["nextGene"] = rows[count]
            return out_genes, "ok", None
        raise AssertionError(f"no canned output for {name}")


# --- model roles --------------------------------------------------------------------------------------------

_DUMMY_RUN = "0" * 32


def model_call(role: Literal["planner", "composer"]) -> ModelCallRecord:
    call = f"{_DUMMY_RUN}.planner.c1" if role == "planner" else f"{_DUMMY_RUN}.v1.composer.c1"
    return ModelCallRecord(
        call_id=call, role=role, model="anthropic.claude-opus-5-5", fallback=False, effort="low" if role == "planner" else "medium",
        attempt=1, started_at=iso_utc(utc_now()), elapsed_ms=10, outcome="ok", stop_reason="end_turn", input_tokens=100, output_tokens=10,
        cache_read_tokens=0, cache_write_tokens=0,
    )


@dataclass
class FakePlanner:
    """Returns `outputs` in turn (the last one repeats); `block` makes each call wait until cancelled or released."""

    outputs: list[ir.PlannerOutput | None]
    block: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    clarifications: list[Clarification | None] = field(default_factory=list)
    warmed: int = 0

    async def plan(
        self, question: str, cohort_context: CohortContext, ledger: SessionLedger | None, *, clarification: Clarification | None = None
    ) -> PlannerResult:
        self.clarifications.append(clarification)
        if self.block:
            self.started.set()
            await self.release.wait()
        output = self.outputs[min(len(self.clarifications) - 1, len(self.outputs) - 1)]
        return PlannerResult(
            output=output, model_calls=[model_call("planner")], serving_model="anthropic.claude-opus-5-5",
            declined=output is None, decline_reason=None if output is not None else "declined", repaired=False,
        )

    async def warm(self) -> None:
        self.warmed += 1

    async def warm_up(self) -> object:
        """orion_agent.model.warmup's Warmable: the warmup turn warms the planner at its own effort (C-41)."""
        self.warmed += 1
        return {"role": "planner", "outcome": "ok"}


@dataclass
class FakeComposer:
    """Cites the first fact of the run; `decline` returns no output; `block` waits until cancelled or released."""

    decline: bool = False
    block: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    feedback: list[Sequence[RuleFinding]] = field(default_factory=list)

    async def compose(
        self, question: str, facts: Sequence[FactRecord], plan: CompiledPlan, observed: ObservedResult, *, allow_list: Sequence[str],
        ledger: SessionLedger | None, feedback: Sequence[RuleFinding] = (),
    ) -> ComposerResult:
        self.feedback.append(list(feedback))
        if self.block:
            self.started.set()
            await self.release.wait()
        if self.decline or not facts:
            return ComposerResult(output=None, model_calls=[model_call("composer")], serving_model="anthropic.claude-opus-5", declined=True,
                                  decline_reason="the model declined to interpret these results")
        cite = facts[-1].id
        output = ComposerOutput(
            interpretation=[ComposerStatement(text=f"In this synthetic cohort the result is {{{{{cite}}}}}.", cites=[cite])],
            hypotheses=[ComposerHypothesis(text="Does the pattern hold within each stage?", cites=[cite], form="question")],
            follow_ups=[ComposerFollowUp(kind="survival_compare", label="Survival by ISS stage", question="Compare survival by ISS stage")],
            open_app_ids=["CohortComparison"],
        )
        return ComposerResult(output=output, model_calls=[model_call("composer")], serving_model="anthropic.claude-opus-5-5",
                              declined=False, decline_reason=None)


_PLACEHOLDER = re.compile(r"\{\{(f\.[^}]+)\}\}")


@dataclass
class FakeVerifier:
    """Resolves placeholders into fact segments; `retry_first` fails attempt 1 once; `drop_all` drops every sentence."""

    retry_first: bool = False
    drop_all: bool = False
    attempts: list[int] = field(default_factory=list)

    def allow_list(self, vocab: Vocabulary, ledger: SessionLedger | None, plan: CompiledPlan) -> list[str]:
        return ["log2FC"]

    def verify(
        self, composer_output: ComposerOutput, facts: Sequence[FactRecord], ledger: SessionLedger | None, vocab: Vocabulary, *,
        plan: CompiledPlan, observed: ObservedResult, attempt: int, allow_list: Sequence[str],
    ) -> VerifierResult:
        self.attempts.append(attempt)
        by_id = {f.id: f for f in facts}
        if self.retry_first and attempt == 1:
            finding = RuleFinding(rule="V-NUM", part="interpretation", index=0, passed=False, detail="a digit outside a placeholder")
            return VerifierResult(passed=False, retry=True, findings=[finding], interpretation=[], hypotheses=[], follow_ups=[],
                                  open_links=[], inserted_observed=[], inserted_limitations=[], observed_only=False)

        def resolve(text: str, cites: list[str]) -> Sentence:
            if self.drop_all:
                return Sentence(segments=[], dropped="V-SEC")
            segments: list[Segment] = []
            position = 0
            for match in _PLACEHOLDER.finditer(text):
                if match.start() > position:
                    segments.append(Segment(text=text[position:match.start()]))
                fact = by_id[match.group(1)]
                segments.append(Segment(text=format_fact(fact.value, fact.unit), fact_id=fact.id))
                position = match.end()
            if position < len(text):
                segments.append(Segment(text=text[position:]))
            return Sentence(segments=segments, cites=cites)

        return VerifierResult(
            passed=not self.drop_all, retry=False, findings=[],
            interpretation=[resolve(s.text, list(s.cites)) for s in composer_output.interpretation],
            hypotheses=[resolve(s.text, list(s.cites)) for s in composer_output.hypotheses],
            follow_ups=[FollowUp(label=f.label, question=f.question, kind=f.kind) for f in composer_output.follow_ups],
            open_links=[NearestApp.of(a) for a in composer_output.open_app_ids],
            inserted_observed=[], inserted_limitations=[], observed_only=self.drop_all,
        )


# --- fixtures as shapes --------------------------------------------------------------------------------------


def fixture_frames(name: str) -> list[dict[str, Any]]:  # Any: frames are walked as JSON
    from support import CONTRACTS

    text = (CONTRACTS / "fixtures" / f"{name}.events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


def shape(frames: Sequence[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    """A stream's shape: event types in order (heartbeats left out), and per snapshot the run status, code, plan
    version, each step's status and code, the fact ids, Observed and Limitations counts and codes, the pending kind,
    whether model text is present, viz launchers, and each cohort's save state and count fact."""
    out: list[tuple[Any, ...]] = []
    for frame in frames:
        kind = frame["type"]
        if kind == "CUSTOM":
            continue
        if kind == "STATE_SNAPSHOT":
            s = frame["snapshot"]
            out.append((
                kind, s["status"], s["code"], s["plan"]["version"] if s["plan"] else None,
                tuple((st["id"], st["status"], st["code"]) for st in s["steps"]),
                tuple(f["id"] for f in s["facts"]),
                len(s["observed"]), tuple(lim["code"] for lim in s["limitations"]),
                s["pending"]["kind"] if s["pending"] else None,
                s["interpretation"] is not None, tuple(v["launcher"] for v in s["vizSpecs"]),
                tuple((c["ref"], c["save"], c["caseCountFactId"]) for c in s["cohorts"]),
                len(s["superseded"]),
            ))
        elif kind in ("STEP_STARTED", "STEP_FINISHED"):
            out.append((kind, frame["stepName"]))
        elif kind == "RUN_FINISHED":
            out.append((kind, frame["result"]["status"], frame["result"]["code"]))
        elif kind == "RUN_ERROR":
            out.append((kind, frame["code"]))
        else:
            out.append((kind,))
    return out


def orion_of(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The `forwardedProps.orion` payload of a request body, to change it in a test."""
    props = body["forwardedProps"]
    assert isinstance(props, dict)
    orion = props["orion"]
    assert isinstance(orion, dict)
    return orion


def stream_frames(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in text.replace("\r\n", "\n").split("\n") if line.startswith("data:")]
