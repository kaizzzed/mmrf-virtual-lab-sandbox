"""The tool catalog (orion/contracts/TOOLS.md, orion_agent/tools/schemas.py): every registry entry, the
golden bodies and their substitution pointers, fact pointers into example outputs, the fixtures'
recorded requests and outputs, and the templates that must reproduce the fixtures' text."""

import ast
import json
import math
import os
import re
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import get_args

from pydantic import BaseModel, JsonValue, ValidationError

from orion_agent.models import (
    CODES,
    CallRecord,
    ConfirmAction,
    Fact,
    FactRecord,
    LimitationCode,
    PinnedKind,
    RunView,
    SaveCohortsArgs,
    Segment,
    StateSnapshotFrame,
    ToolCallArgsFrame,
    VizReport,
    WireEvent,
    canonical_json,
    derive_request_pinned,
    fingerprint_sha256,
    format_fact,
    iter_sse_data,
    parse_frame,
    pinned_hash,
    sha256_hex,
)
from orion_agent import catalog
from orion_agent.tools import base, normalize
from orion_agent.tools import schemas as s

AGENT = Path(__file__).resolve().parents[1]
CONTRACTS = Path(os.environ.get("ORION_CONTRACTS_DIR", "/contracts"))
FIXTURES = CONTRACTS / "fixtures"


def _golden_dir() -> Path:
    named = os.environ.get(s.GOLDEN_DIR_ENV)
    if named:
        return Path(named)
    mounted = Path("/golden")
    return mounted if mounted.is_dir() else AGENT.parent / "golden"


GOLDEN = _golden_dir()
NEVER_CALLED = ("/analysis/v0/cohorts/query", "/cohorts/download", "/files/", "/gene_expression/")


def load_json(path: Path) -> JsonValue:
    value: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    return value


def as_dict(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict), value
    return value


def as_list(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list), value
    return value


def dump(model: BaseModel) -> dict[str, JsonValue]:
    return as_dict(model.model_dump(mode="json", by_alias=True))


def stream(name: str) -> list[WireEvent]:
    text = (FIXTURES / f"{name}.events.jsonl").read_text(encoding="utf-8")
    return [parse_frame(data) for data in iter_sse_data(text)]


def last_view(name: str) -> RunView:
    views = [f.snapshot for f in stream(name) if isinstance(f, StateSnapshotFrame)]
    return views[-1]


def save_args(name: str) -> SaveCohortsArgs:
    deltas = "".join(f.delta for f in stream(name) if isinstance(f, ToolCallArgsFrame))
    return SaveCohortsArgs.model_validate_json(deltas)


def segment_parts(sentence: Sequence[Mapping[str, JsonValue]]) -> list[s.TemplatePart]:
    """A fixture's segments as merged (text|fact, text) parts, for comparison with template parts."""
    parts: list[s.TemplatePart] = []
    for seg in sentence:
        fact = seg.get("factId")
        parts.append(("fact", str(fact)) if isinstance(fact, str) else ("text", str(seg["text"])))
    return s.merge_text(parts)


def plain(parts: Sequence[s.TemplatePart], facts: Mapping[str, Fact]) -> str:
    return s.template_text(parts, {fid: format_fact(f.value, f.unit) for fid, f in facts.items()})


def view_parts(view_sentence: Sequence[Segment]) -> list[s.TemplatePart]:
    return segment_parts([dump(seg) for seg in view_sentence])


def view_text(view_sentence: Sequence[Segment]) -> str:
    return "".join(seg.text for seg in view_sentence)


# --- The registry -------------------------------------------------------------------------------------------


class RegistryTest(unittest.TestCase):
    def test_every_tool_appears_once_with_its_kind(self) -> None:
        server, executor, client = (frozenset(get_args(t)) for t in (base.ServerToolName, base.ExecutorFunctionName, base.ClientActionName))
        self.assertEqual(set(s.TOOL_REGISTRY), server | executor | client)
        self.assertEqual(len(s.TOOL_REGISTRY), 19)
        for name, spec in s.TOOL_REGISTRY.items():
            with self.subTest(name):
                self.assertEqual(spec.name, name)
                expected = "server" if name in server else "exec" if name in executor else "client"
                self.assertEqual(spec.kind, expected)
                self.assertEqual(spec.version, f"{name}@1")
        self.assertEqual(s.STEP_TOOLS, server | executor | {"save_cohorts"})

    def test_models_forbid_extra_keys_and_publish_a_schema(self) -> None:
        for name, spec in s.TOOL_REGISTRY.items():
            models: list[type[BaseModel]] = [spec.input_model]
            models += [m for m in (spec.observed_model, spec.reported_model, spec.step_args_model) if m is not None]
            for model in models:
                with self.subTest(tool=name, model=model.__name__):
                    self.assertEqual(model.model_config.get("extra"), "forbid")
                    self.assertIsInstance(model.model_json_schema(by_alias=True), dict)
            schema = s.input_json_schema(name)
            self.assertEqual(schema.get("type"), "object")

    def test_writes_retries_and_clicks(self) -> None:
        writes = {n for n, spec in s.TOOL_REGISTRY.items() if spec.writes}
        self.assertEqual(writes, {"save_cohorts", "set_active_cohort"})
        retryable = {n for n, spec in s.TOOL_REGISTRY.items() if spec.retryable}
        self.assertEqual(retryable, set(s.SERVER_TOOLS) - {"kg_query"})
        for name in writes:
            self.assertTrue(s.TOOL_REGISTRY[name].requires_click, name)
        self.assertIs(s.TOOL_REGISTRY["save_cohorts"].reported_model, ConfirmAction)
        self.assertIs(s.TOOL_REGISTRY["save_cohorts"].input_model, SaveCohortsArgs)
        self.assertIs(s.TOOL_REGISTRY["mount_viz"].reported_model, VizReport)

    def test_server_tools_use_the_gateway_and_the_a4_budget(self) -> None:
        for name in s.SERVER_TOOLS:
            spec = s.TOOL_REGISTRY[name]
            with self.subTest(name):
                self.assertIsNotNone(spec.timeout_s)
                assert spec.timeout_s is not None
                self.assertLessEqual(spec.timeout_s, base.TOOL_HTTP_TIMEOUT_S)
                self.assertEqual(spec.gateway_target, "kg" if name == "kg_query" else "vlab")
                self.assertEqual(spec.gateway_name, s.gateway_tool_name(name))
                self.assertEqual(spec.engine is None, name == "kg_query")
                self.assertEqual(bool(spec.endpoints), name != "kg_query")
                for endpoint in spec.endpoints:
                    self.assertRegex(endpoint, r"^(GET|POST) /(sandbox|analysis/v0|protein-paint|guppy)/")
                    self.assertFalse(any(path in endpoint for path in NEVER_CALLED), endpoint)
        self.assertEqual(s.gateway_tool_name("de_run"), "vlab___de_run")
        self.assertEqual(s.gateway_tool_name("kg_query"), "kg___kg_query")
        for name in s.EXEC_FUNCTIONS | s.CLIENT_ACTIONS:
            spec = s.TOOL_REGISTRY[name]
            self.assertEqual((spec.endpoints, spec.requests, spec.gateway_target, spec.timeout_s), ((), (), None, None), name)

    def test_guards_name_contract_codes(self) -> None:
        limitation_codes = set(get_args(LimitationCode))
        for name, spec in s.TOOL_REGISTRY.items():
            for g in spec.guards:
                with self.subTest(tool=name, rule=g.guard.rule):
                    self.assertIn(g.guard.on_fail, CODES)
                    self.assertIn(g.guard.on_fail, limitation_codes)
                    if g.guard.phase == "pre":
                        self.assertEqual(g.step_status, "Skipped")
                    if g.effect == "report":
                        self.assertEqual(g.guard.phase, "post")
            self.assertEqual(spec.contract_guards(), [g.guard for g in spec.guards])
        rules = {g.guard.rule: g.guard.on_fail for spec in s.TOOL_REGISTRY.values() for g in spec.guards}
        self.assertEqual(rules["group_cases>=10"], "INELIGIBLE_GROUP")
        self.assertEqual(rules["eligible>=10_per_group"], "INELIGIBLE_GROUP")
        self.assertEqual(rules["samples<=4000"], "REQUEST_TOO_LARGE")
        self.assertEqual(rules["case_ids<=10000"], "TOO_MANY_IDS")
        self.assertEqual(rules["kg_target_registered"], "KG_NOT_CONNECTED")

    def test_failure_words_cover_every_step_tool(self) -> None:
        steps = set(s.STEP_TOOLS) - {"save_cohorts"}
        self.assertEqual(set(s.TOOL_PHRASES), steps)
        self.assertEqual(set(s.MISSING_PHRASES), steps)
        self.assertLessEqual(set(get_args(base.ToolErrorCode)) - {"CANCELLED"}, set(s.FAILURE_PHRASES))

    def test_tool_spec_lookup(self) -> None:
        self.assertIs(s.tool_spec("de_run"), s.TOOL_REGISTRY["de_run"])
        with self.assertRaises(KeyError):
            s.tool_spec("vlab___de_run")


# --- Golden bodies ----------------------------------------------------------------------------------------------


GOLDENS: dict[str, str | None] = {
    "fixture_fingerprint": "proteinpaint/termdb-topMutatedGenes.json",
    "cohort_count": None,
    "cases_query": None,
    "gene_cases": None,
    "top_mutated_genes": "proteinpaint/termdb-topMutatedGenes.json",
    "survival_compare": "analysis/survival.json",
    "sample_lists": "proteinpaint/termdb-getsamplelist.json",
    "de_preanalysis": "proteinpaint/termdb-DE-preAnalysis.json",
    "de_run": "proteinpaint/termdb-DE-wilcoxon.json",
    "kg_query": None,
}


class GoldenTest(unittest.TestCase):
    def setUp(self) -> None:
        if not GOLDEN.is_dir():
            self.skipTest(f"{GOLDEN} is not mounted (mount orion/golden at /golden or set {s.GOLDEN_DIR_ENV})")

    def payload(self, template: s.RequestTemplate) -> JsonValue:
        if template.golden is None:
            return template.inline
        golden = as_dict(load_json(GOLDEN / template.golden))
        self.assertEqual(golden["schema"], "orion.golden/v1")
        request = as_dict(as_list(golden["requests"])[template.request_index])
        self.assertEqual(request["path"], template.path)
        return request[template.payload]

    def test_every_server_tool_names_its_golden(self) -> None:
        self.assertEqual({n: s.TOOL_REGISTRY[n].golden for n in s.SERVER_TOOLS}, GOLDENS)
        for name, golden in GOLDENS.items():
            if golden is not None:
                self.assertTrue((GOLDEN / golden).is_file(), golden)

    def test_substituted_and_fixed_pointers_exist(self) -> None:
        for name in s.SERVER_TOOLS:
            for template in s.TOOL_REGISTRY[name].requests:
                with self.subTest(tool=name, path=template.path):
                    if template.payload == "none":
                        self.assertIsNone(template.inline)
                        self.assertEqual((template.substitutions, template.fixed), ((), ()))
                        continue
                    payload = self.payload(template)
                    self.assertIsInstance(payload, dict)
                    for pointer in [sub.pointer for sub in template.substitutions] + list(template.fixed):
                        s.resolve_pointer(payload, pointer)

    def test_de_run_thresholds_are_the_viewer_defaults_as_minus_log10(self) -> None:
        body = self.payload(s.DE_RUN_REQUEST)
        thresholds = as_dict(s.resolve_pointer(body, "/volcanoRender/significanceThresholds"))
        self.assertEqual(thresholds["pValueCutoff"], s.minus_log10(0.05))
        self.assertEqual(thresholds["pValueType"], "adjusted")
        self.assertEqual(thresholds["foldChangeCutoff"], 0.3)
        self.assertEqual(s.resolve_pointer(body, "/method"), "wilcoxon")
        pre = self.payload(s.DE_PREANALYSIS_REQUEST)
        self.assertEqual(s.resolve_pointer(pre, "/samplelst"), s.resolve_pointer(body, "/samplelst"))
        self.assertIs(s.resolve_pointer(pre, "/preAnalysis"), True)

    def test_survival_golden_is_one_filter_per_curve(self) -> None:
        body = as_dict(self.payload(s.SURVIVAL_REQUEST))
        self.assertEqual(set(body), {"filters"})
        self.assertEqual(len(as_list(body["filters"])), 2)

    def test_top_mutated_genes_golden_is_the_all_case_ranking(self) -> None:
        query = as_dict(self.payload(s.TOP_MUTATED_GENES_REQUEST))
        self.assertEqual(query["filter0"], s.ALL_CASES_FILTER)
        self.assertEqual(query["maxGenes"], 50)
        fingerprint = self.payload(s.FINGERPRINT_REQUESTS[3])
        self.assertEqual(fingerprint, query)


# --- Fact pointers into example outputs -------------------------------------------------------------------------


def _rank(source: s.RankSourceTool, rank_by: s.RankBy, rows: list[dict[str, JsonValue]], key: str, value: str, n: int) -> base.RankResult:
    ranked = [
        base.RankedRow(rank=i + 1, key=str(row[key]), value=abs(float(str(row[value]))), row=row)
        for i, row in enumerate(rows)
    ]
    return base.RankResult(
        version="rank_top_n@1", rank_by=rank_by, ascending=rank_by != "cases", n_requested=n, rows=ranked,
        count=len(ranked), ties_at_cutoff=[], total_rows=len(rows),
    )


DE_ROWS: list[dict[str, JsonValue]] = [
    {"gene_name": "BRAF", "fold_change": 1.12, "original_p_value": 2.1e-10, "adjusted_p_value": 2.1e-09},
    {"gene_name": "JUN", "fold_change": -0.87, "original_p_value": 1.4e-09, "adjusted_p_value": 1.4e-08},
]


def example_outputs() -> dict[str, tuple[BaseModel, s.FactContext]]:
    doc = s.FingerprintDocument.model_validate(
        {"schema": "orion.fixture-fingerprint/v1", "sandbox": {"status": "OK", "synthetic": True, "cases": 1000},
         "analysis": {"version": "1.0.3"}, "proteinpaint": {"pkgver": "2.216.0", "codedate": "20261003"},
         "ranking": [{"gene": "KRAS", "cases": 480}]}
    )
    sha = fingerprint_sha256(dump(doc))
    curve = [{"time": 0, "survival": 1.0}, {"time": 30, "survival": 0.4}]
    survival = s.SurvivalCompareObserved.model_validate(
        {"groups": [{"label": "Stage I", "cases": 12, "events": 5, "curve": curve}, {"label": "Stage II", "cases": 14, "events": 6, "curve": curve}],
         "pValue": 0.0342, "engine": s.ENGINE_GEN3_ANALYSIS}
    )
    genes = [s.MutatedGene(gene="KRAS", cases=3), s.MutatedGene(gene="NRAS", cases=2)]
    tmg_rows: list[dict[str, JsonValue]] = [dump(g) for g in genes]
    return {
        "fixture_fingerprint": (s.FixtureFingerprintObserved(document=doc, fingerprint_sha256=sha), s.FactContext()),
        "cohort_count": (s.CohortCountObserved(count=12), s.FactContext(cohort="Stage I")),
        "cases_query": (s.CasesQueryObserved(count=2, case_ids=["SYNTH-CASE-0001", "SYNTH-CASE-0002"], pages=1, complete=True),
                        s.FactContext(cohort="Mutation-tested cases")),
        "gene_cases": (s.GeneCasesObserved(gene="KRAS", count=1, case_ids=["SYNTH-CASE-0001"]), s.FactContext()),
        "survival_compare": (survival, s.FactContext()),
        "sample_lists": (s.SampleListsObserved.model_validate(
            {"groups": [{"key": "control", "count": 2, "sampleIds": [2, 3]}, {"key": "case", "count": 1, "sampleIds": [1]}]}),
            s.FactContext(group_labels=("KRAS not detected", "KRAS mutated"))),
        "de_preanalysis": (s.DePreanalysisObserved.model_validate({"groups": [{"key": "control", "eligible": 2}, {"key": "case", "eligible": 1}]}),
                           s.FactContext(group_labels=("KRAS not detected", "KRAS mutated"))),
        "de_run": (s.DeRunObserved.model_validate(
            {"method": "wilcoxon", "sampleSizes": {"control": 2, "case": 1}, "totals": {"tested": 90, "significant": 2, "up": 1, "down": 1},
             "genes": DE_ROWS, "cacheId": None}), s.FactContext()),
        "set_operation": (base.SetOperationResult(version="set_operation@1", op="difference", a_count=2, b_count=1, count=1,
                                                  case_ids=["SYNTH-CASE-0002"]), s.FactContext(cohort="KRAS not detected (ORION)")),
        "check_subset": (base.SubsetResult(version="check_subset@1", inner_count=1, outer_count=2, is_subset=True, outside_count=0,
                                           outside_sample=[]), s.FactContext(inner="KRAS-mutated cases")),
        "derive_km_median": (base.KmMedianResult(version="derive_km_median@1", unit="days", groups=[
            base.KmMedian(label="Stage I", median=30.0, reached=True), base.KmMedian(label="Stage II", median=None, reached=False)]),
            s.FactContext()),
        "rank_top_n:de_run:adjusted_p_value": (_rank("de_run", "adjusted_p_value", DE_ROWS, "gene_name", "adjusted_p_value", 2),
                                               s.FactContext(rank_source="de_run", rank_by="adjusted_p_value")),
        "rank_top_n:de_run:original_p_value": (_rank("de_run", "original_p_value", DE_ROWS, "gene_name", "original_p_value", 3),
                                               s.FactContext(rank_source="de_run", rank_by="original_p_value")),
        "rank_top_n:de_run:abs(fold_change)": (_rank("de_run", "abs(fold_change)", DE_ROWS, "gene_name", "fold_change", 2),
                                               s.FactContext(rank_source="de_run", rank_by="abs(fold_change)")),
        "rank_top_n:top_mutated_genes:cases": (_rank("top_mutated_genes", "cases", tmg_rows, "gene", "cases", 2),
                                               s.FactContext(rank_source="top_mutated_genes", rank_by="cases")),
    }


class FactPointerTest(unittest.TestCase):
    def test_every_fact_spec_resolves_in_an_example_output(self) -> None:
        covered: set[tuple[str, str]] = set()
        for key, (output, ctx) in example_outputs().items():
            tool = key.split(":")[0]
            observed = dump(output)
            spec = s.tool_spec(tool)
            assert spec.observed_model is not None
            spec.observed_model.model_validate(observed)
            drafts = s.draft_facts(spec.name, "s3", observed, ctx)
            with self.subTest(key):
                self.assertTrue(drafts or not s.fact_specs(spec.name, source=ctx.rank_source, rank_by=ctx.rank_by))
                for draft in drafts:
                    self.assertEqual(s.resolve_pointer({"observed": observed}, draft.json_pointer), draft.value)
                    self.assertEqual(s.split_fact_id(draft.id)[0], "s3")
                    self.assertRegex(draft.id, r"^f\.s[1-9][0-9]*(\.[A-Za-z0-9_-]+)+$")
                    format_fact(draft.value, draft.unit)
                    covered.add((key, draft.id.split(".", 2)[2].split(".")[0]))
            for fs in s.fact_specs(spec.name, source=ctx.rank_source, rank_by=ctx.rank_by):
                head = fs.suffix.split(".")[0]
                if fs.when != "rank_count":
                    self.assertIn((key, head), covered, f"{key}: {fs.suffix} yielded nothing")
        # The original-p ranking asked for 3 of 2 rows, so its count fact (FEWER_THAN_N) is present.
        self.assertIn(("rank_top_n:de_run:original_p_value", "count"), covered)
        self.assertNotIn(("rank_top_n:de_run:adjusted_p_value", "count"), covered)

    def test_fact_specs_cover_the_registry_and_use_format_fact_rules(self) -> None:
        self.assertEqual(set(s.FACT_SPECS), set(s.TOOL_REGISTRY))
        specs = [fs for t in s.FACT_SPECS.values() for fs in t] + [fs for t in s.RANK_FACT_SPECS.values() for fs in t]
        expected_format = {"p": "p", "log2FC": "log2FC", "gene": "string"}
        for fs in specs:
            with self.subTest(fs.suffix):
                self.assertEqual(fs.format, expected_format.get(fs.unit, "number"))
                self.assertEqual(fs.value_type == "str", fs.unit == "gene")
                self.assertTrue(fs.pointer.startswith("/observed/"))
        for source, rank_by in s.RANK_FACT_SPECS:
            self.assertIn(source, s.RANK_SOURCES)
            self.assertIn(rank_by, s.RANK_LABELS)
        self.assertEqual({v[0] for v in s.DE_RANK_BY.values()} | {"cases"}, {rb for _, rb in s.RANK_FACT_SPECS})
        with self.assertRaises(ValueError):
            s.fact_specs("rank_top_n")
        with self.assertRaises(ValueError):
            s.fact_specs("rank_top_n", source="top_mutated_genes", rank_by="adjusted_p_value")

    def test_median_not_reached_and_absent_p_yield_no_fact(self) -> None:
        km, _ = example_outputs()["derive_km_median"]
        drafts = s.draft_facts("derive_km_median", "s5", dump(km))
        self.assertEqual([d.id for d in drafts], ["f.s5.median.i"])
        self.assertEqual(drafts[0].label, "Stage I median overall survival")
        survival, _ = example_outputs()["survival_compare"]
        observed = {**dump(survival), "pValue": None}
        self.assertEqual([d.id for d in s.draft_facts("survival_compare", "s4", observed)], ["f.s4.events.i", "f.s4.events.ii"])

    def test_view_rank_cap_limits_row_facts_and_adds_the_count(self) -> None:
        rows: list[dict[str, JsonValue]] = [
            {"gene_name": f"G{k:03d}", "fold_change": 1.0, "original_p_value": k * 1e-9, "adjusted_p_value": k * 1e-8} for k in range(1, 26)
        ]
        ranked = _rank("de_run", "adjusted_p_value", rows, "gene_name", "adjusted_p_value", 25)
        drafts = s.draft_facts("rank_top_n", "s10", dump(ranked), s.FactContext(rank_source="de_run", rank_by="adjusted_p_value"))
        self.assertEqual(len([d for d in drafts if d.id.endswith(".gene")]), 20)
        self.assertEqual(drafts[-1].id, "f.s10.count")
        self.assertEqual((drafts[-1].value, drafts[-1].label, drafts[-1].unit), (25, "Ranked genes", "genes"))

    def test_count_labels_need_their_cohort(self) -> None:
        with self.assertRaises(ValueError):
            s.draft_facts("cohort_count", "s2", {"count": 3})
        with self.assertRaises(ValueError):
            s.draft_facts("check_subset", "s4", dump(example_outputs()["check_subset"][0]))
        with self.assertRaises(ValueError):
            s.draft_facts("sample_lists", "s6", dump(example_outputs()["sample_lists"][0]))


# --- The fixtures' recorded calls -------------------------------------------------------------------------------


RECORD_CONTEXT: dict[str, s.FactContext] = {
    "s1": s.FactContext(),
    "s2": s.FactContext(cohort="High risk"),
    "s3": s.FactContext(cohort="Standard risk"),
    "s4": s.FactContext(),
    "s5": s.FactContext(),
}


class FixtureRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        if not FIXTURES.is_dir():
            self.skipTest(f"{FIXTURES} is not mounted")

    def records(self) -> list[dict[str, JsonValue]]:
        return [as_dict(load_json(FIXTURES / name)) for name in ("run-record.j1-complete.json", "run-record.j1-retry.json")]

    def test_recorded_requests_and_outputs_validate(self) -> None:
        """Every recorded request and Observed validates against its catalog model and dumps back byte
        for byte, so requestSha256 and responseSha256 are what a tool's own output hashes to (the
        builder writes calls through these models; base.KmPoint.time and KmMedian.median are floats)."""
        for record in self.records():
            for raw in as_list(record["calls"]):
                call = CallRecord.model_validate(raw)
                spec = s.tool_spec(call.tool)
                with self.subTest(call=call.call_id):
                    request = spec.input_model.model_validate(call.request)
                    self.assertEqual(dump(request), call.request)
                    self.assertEqual(sha256_hex(canonical_json(dump(request), portable=False)), call.request_sha256)
                    self.assertEqual(call.transport, "in_process")
                    if call.observed is None:
                        self.assertEqual(call.status, "error")
                        continue
                    assert spec.observed_model is not None
                    observed = spec.observed_model.model_validate(call.observed)
                    self.assertEqual(dump(observed), call.observed)
                    self.assertEqual(sha256_hex(canonical_json(dump(observed), portable=False)), call.response_sha256)
                    expected_hops = {"fixture_fingerprint": 4, "survival_compare": 3}.get(call.tool, 0 if spec.kind == "exec" else 1)
                    self.assertEqual(len(call.provenance), expected_hops)
                    for hop in call.provenance:
                        self.assertIn(f"{hop.method} {hop.path}", spec.endpoints)

    def test_recorded_facts_are_what_the_catalog_yields(self) -> None:
        for record in self.records():
            calls = {str(as_dict(c)["callId"]): CallRecord.model_validate(c) for c in as_list(record["calls"])}
            facts = [FactRecord.model_validate(f) for f in as_list(record["facts"])]
            by_step: dict[str, list[FactRecord]] = {}
            for f in facts:
                by_step.setdefault(f.step_id, []).append(f)
            for step_id, expected in by_step.items():
                call = calls[expected[0].call_id]
                assert isinstance(call.observed, dict)
                drafts = s.draft_facts(s.tool_spec(call.tool).name, step_id, call.observed, RECORD_CONTEXT[step_id])
                with self.subTest(step=step_id):
                    self.assertEqual(
                        [(d.id, d.label, d.value, d.unit, d.json_pointer) for d in drafts],
                        [(f.id, f.label, f.value, f.unit, f.json_pointer) for f in expected],
                    )

    def test_steps_use_catalog_tools_and_kinds(self) -> None:
        for record in self.records():
            for version in as_list(record["planVersions"]):
                for raw in as_list(as_dict(version)["steps"]):
                    step = as_dict(raw)
                    spec = s.tool_spec(str(step["tool"]))
                    self.assertIn(spec.name, s.STEP_TOOLS)
                    self.assertEqual(step["kind"], spec.kind)
                    # Server args here hold no StepRef, so they are the recorded request itself.
                    self.assertFalse(_has_ref(step["args"]))

    def test_fixture_streams_use_catalog_tools_and_fact_shapes(self) -> None:
        for name in ("j1-complete", "j1-partial", "j3-save-pause", "j3-after-confirm", "j3-dont-save", "j3-deinput", "oncomatrix"):
            view = last_view(name)
            for step in view.steps:
                spec = s.tool_spec(step.tool)
                self.assertEqual(step.kind, spec.kind, f"{name} {step.id}")
                self.assertIn(spec.name, s.STEP_TOOLS)
            for fact in view.facts:
                with self.subTest(fixture=name, fact=fact.id):
                    self.assertEqual(s.split_fact_id(fact.id)[0], fact.step_id)
                    self.assertTrue(_matches_a_spec(fact), f"{fact.id} {fact.json_pointer} matches no FactSpec of {fact.tool}")


def _has_ref(value: JsonValue) -> bool:
    if s.is_step_ref(value):
        return True
    if isinstance(value, dict):
        return any(_has_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_ref(v) for v in value)
    return False


def _shape(template: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\$\\\{[a-z_]+\\\}", "[A-Za-z0-9_-]+", re.escape(template)) + "$")


def _matches_a_spec(fact: Fact) -> bool:
    tool = s.tool_spec(fact.tool).name
    specs = list(s.FACT_SPECS[tool]) + ([fs for t in s.RANK_FACT_SPECS.values() for fs in t] if tool == "rank_top_n" else [])
    suffix = s.split_fact_id(fact.id)[1]
    return any(
        _shape(fs.suffix).match(suffix) and _shape(fs.pointer).match(fact.json_pointer) and fs.unit == fact.unit for fs in specs
    )


# --- J3 and J2 outputs rebuilt from the fixtures, then their facts and text -------------------------------------


J3_GENES: list[tuple[str, float, float]] = [
    ("BRAF", 1.12, 2.1e-09), ("ETV4", 0.94, 6.8e-09), ("JUN", -0.87, 1.4e-08), ("DUSP6", 1.31, 3.9e-08),
    ("FOS", -1.02, 7.7e-08), ("TP53", -0.66, 2.6e-07), ("MAPK1", 2.17, 5.0e-07), ("DIS3", 1.88, 1.2e-06),
    ("ETV5", 1.54, 3.3e-06), ("KRAS", 0.71, 8.1e-06),
]
WT_NAME, MUT_NAME = "KRAS not detected (ORION)", "KRAS mutated (ORION)"


def _sample(case_id: str) -> int:
    return int(case_id.rsplit("-", 1)[1])


def j3_outputs() -> dict[str, tuple[str, BaseModel, s.FactContext]]:
    """J3's s2..s10 outputs, consistent with the save pause's case-ID lists and the fixture's values."""
    pending = {c.ref: c for c in save_args("j3-save-pause").cohorts}
    wt_def, mut_def = pending["kras_wt"].definition, pending["kras_mut"].definition
    assert wt_def is not None and mut_def is not None and wt_def.kind == "case_ids" and mut_def.kind == "case_ids"
    wt, mut = list(wt_def.case_ids), list(mut_def.case_ids)
    tested = sorted(wt + mut)
    rows: list[dict[str, JsonValue]] = [
        {"gene_name": g, "fold_change": lfc, "original_p_value": p / 10, "adjusted_p_value": p} for g, lfc, p in J3_GENES
    ]
    rows += [{"gene_name": f"GENE{k}", "fold_change": 0.5, "original_p_value": 1e-4, "adjusted_p_value": 1e-3} for k in range(6)]
    de = s.DeRunObserved.model_validate(
        {"method": "wilcoxon", "sampleSizes": {"control": 520, "case": 480}, "totals": {"tested": 7684, "significant": 16, "up": 8, "down": 8},
         "genes": rows, "cacheId": "2d4c6f02a85da045adaf2b0b709737b7"}
    )
    labels = (s.cohort_label(WT_NAME), s.cohort_label(MUT_NAME))
    return {
        "s2": ("gene_cases", s.GeneCasesObserved(gene="KRAS", count=len(mut), case_ids=mut), s.FactContext()),
        "s3": ("cases_query", s.CasesQueryObserved(count=1000, case_ids=tested, pages=1, complete=True), s.FactContext(cohort="Mutation-tested cases")),
        "s4": ("check_subset", base.SubsetResult(version="check_subset@1", inner_count=480, outer_count=1000, is_subset=True, outside_count=0,
                                                 outside_sample=[]), s.FactContext(inner="KRAS-mutated cases")),
        "s5": ("set_operation", base.SetOperationResult(version="set_operation@1", op="difference", a_count=1000, b_count=480, count=520,
                                                        case_ids=wt), s.FactContext(cohort=WT_NAME)),
        "s6": ("sample_lists", s.SampleListsObserved.model_validate({"groups": [
            {"key": "control", "count": 520, "sampleIds": [_sample(c) for c in wt]},
            {"key": "case", "count": 480, "sampleIds": [_sample(c) for c in mut]}]}), s.FactContext(group_labels=labels)),
        "s7": ("de_preanalysis", s.DePreanalysisObserved.model_validate({"groups": [{"key": "control", "eligible": 520}, {"key": "case", "eligible": 480}]}),
               s.FactContext(group_labels=labels)),
        "s9": ("de_run", de, s.FactContext()),
        "s10": ("rank_top_n", _rank("de_run", "adjusted_p_value", rows[:10], "gene_name", "adjusted_p_value", 10),
                s.FactContext(rank_source="de_run", rank_by="adjusted_p_value")),
    }


class JourneyFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        if not FIXTURES.is_dir():
            self.skipTest(f"{FIXTURES} is not mounted")

    def assert_facts(self, view: RunView, step_id: str, tool: str, output: BaseModel, ctx: s.FactContext) -> None:
        spec = s.tool_spec(tool)
        assert spec.observed_model is not None
        observed = dump(spec.observed_model.model_validate(dump(output)))
        drafts = s.draft_facts(spec.name, step_id, observed, ctx)
        expected = [f for f in view.facts if f.step_id == step_id]
        self.assertTrue(expected, f"the fixture has no facts for {step_id}")
        with self.subTest(step=step_id, tool=tool):
            self.assertEqual(
                [(d.id, d.label, d.value, d.unit, d.json_pointer) for d in drafts],
                [(f.id, f.label, f.value, f.unit, f.json_pointer) for f in expected],
            )
            for f in expected:
                self.assertEqual(f.tool, tool)

    def test_j3_facts(self) -> None:
        view = last_view("j3-after-confirm")
        for step_id, (tool, output, ctx) in j3_outputs().items():
            self.assert_facts(view, step_id, tool, output, ctx)
        # The confirmation card's chips are facts the catalog yields.
        ids = {f.id for f in view.facts}
        for cohort in save_args("j3-save-pause").cohorts:
            self.assertIn(cohort.case_count_fact_id, ids)
            self.assertIn(cohort.eligible_count_fact_id, ids)

    def test_j2_facts(self) -> None:
        view = last_view("oncomatrix")
        pinned = view.viz_specs[0].pinned
        assert pinned is not None and pinned.kind == "oncomatrix" and pinned.filter0 is not None
        clause = as_dict(as_list(pinned.filter0["and"])[0])
        ids = [str(i) for i in as_list(as_dict(clause["in"])["case_id"])]
        self.assert_facts(view, "s2", "cases_query", s.CasesQueryObserved(count=len(ids), case_ids=ids, pages=1, complete=True),
                          s.FactContext(cohort="CGS high-risk cases"))
        genes: list[dict[str, JsonValue]] = [{"gene": "KRAS", "cases": 163}, {"gene": "NRAS", "cases": 121}, {"gene": "DIS3", "cases": 97}]
        self.assert_facts(view, "s4", "rank_top_n", _rank("top_mutated_genes", "cases", genes, "gene", "cases", 3),
                          s.FactContext(rank_source="top_mutated_genes", rank_by="cases"))
        self.assertEqual(pinned.genes, [str(g["gene"]) for g in genes])


# --- Templates reproduce the fixtures' text ------------------------------------------------------------------


class TemplateFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        if not FIXTURES.is_dir():
            self.skipTest(f"{FIXTURES} is not mounted")

    def check(self, view: RunView, index: int, template_id: str, *, steps: Mapping[str, str] | None = None,
              values: Mapping[str, str] | None = None, items: Sequence[tuple[str, Mapping[str, str]]] = ()) -> None:
        parts = s.template_parts(s.TEMPLATES[template_id], steps=steps, values=values, items=items)
        facts = {f.id: f for f in view.facts}
        sentence = view.observed[index].segments
        with self.subTest(template=template_id, index=index):
            self.assertEqual(parts, view_parts(sentence))
            self.assertEqual(plain(parts, facts), view_text(sentence))

    def test_j1_observed(self) -> None:
        view = last_view("j1-complete")
        groups = (("High risk", "high", "s2"), ("Standard risk", "standard", "s3"))
        for i, (label, key, count) in enumerate(groups):
            self.check(view, i, "survival.group", steps={"count": count, "survival": "s4"}, values={"group": label, "key": key})
        self.check(view, 2, "survival.p", steps={"survival": "s4"})
        self.check(view, 3, "survival.median", steps={"median": "s5"},
                   items=[("reached", {"group": label, "key": key}) for label, key, _ in groups])
        self.assertEqual(len(view.observed), 4)
        partial = last_view("j1-partial")
        for i, (label, _, count) in enumerate(groups):
            self.check(partial, i, "survival.group_count", steps={"count": count}, values={"group": label})

    def test_j3_observed(self) -> None:
        view = last_view("j3-after-confirm")
        names = {"control": WT_NAME, "case": MUT_NAME}
        self.check(view, 0, "de.cohorts", steps={"tested": "s3", "mutated": "s2", "difference": "s5"}, values={"gene": "KRAS"})
        self.check(view, 1, "de.groups", steps={"pre": "s7"}, values=names)
        method = {**names, "method": s.DE_METHOD_LABELS["wilcoxon"], "rank": s.RANK_LABELS["adjusted_p_value"]}
        self.check(view, 2, "de.method", values=method)
        self.check(view, 3, "de.totals", steps={"de": "s9"})
        self.check(view, 4, "de.top", steps={"ranking": "s10"}, values={"rank": s.RANK_LABELS["adjusted_p_value"]},
                   items=[(s.DE_TOP_ITEM["adjusted_p_value"], {"pos": str(k)}) for k in range(1, 11)])
        self.assertEqual(len(view.observed), 5)
        self.assertEqual(last_view("j3-dont-save").observed, view.observed)

    def test_j2_observed(self) -> None:
        view = last_view("oncomatrix")
        roles = {"cohort_count": "f.s2.count", "ranking": "s4"}
        self.check(view, 0, "genes.cohort", steps=roles)
        self.check(view, 1, "genes.top", steps=roles, items=[("first", {"pos": "1"}), ("rest", {"pos": "2"}), ("rest", {"pos": "3"})])

    def limitation(self, template_id: str, values: Mapping[str, str] | None = None) -> tuple[str | None, str, str | None]:
        t = s.TEMPLATES[template_id]
        text = s.template_text(s.template_parts(t, values=values), {})
        unblock = None if t.unblock is None else s.fill_text(t.unblock, values or {})
        return t.code, text, unblock

    def test_limitations(self) -> None:
        def lines(name: str) -> list[tuple[str | None, str, str | None]]:
            return [(item.code, item.text, item.unblock) for item in last_view(name).limitations]

        self.assertEqual(lines("j1-complete"), [self.limitation("survival.viewer_p"), self.limitation("survival.no_hazard_ratio")])
        failed = {"tool": s.TOOL_PHRASES["survival_compare"], "failure": s.FAILURE_PHRASES["TIMEOUT"], "missing": s.MISSING_PHRASES["survival_compare"]}
        self.assertEqual(lines("j1-partial"), [self.limitation("tool.failed", failed)])
        self.assertEqual(lines("j3-after-confirm"), [self.limitation("panel.gene", {"gene": "KRAS"})])
        self.assertEqual(lines("j3-dont-save"), [self.limitation("panel.gene", {"gene": "KRAS"}), self.limitation("save.skipped")])
        self.assertEqual(lines("oncomatrix"), [self.limitation("panel.ranking")])

    def test_viz_specs(self) -> None:
        def title(template_id: str, values: Mapping[str, str]) -> str:
            return s.template_text(s.template_parts(s.TEMPLATES[template_id], values=values), {})

        j1 = last_view("j1-complete").viz_specs[0]
        survival = s.VIZ_TEMPLATES["survival"]
        self.assertEqual((j1.app_id, j1.launcher, j1.cohort_refs), (survival.app_id, survival.launcher, ["base"]))
        self.assertEqual(j1.title, title(survival.title, {"split": "CGS risk category"}))
        assert survival.caption is not None and survival.note is not None and j1.frozen is not None and j1.frozen.note is not None
        self.assertEqual(j1.caption, s.TEMPLATES[survival.caption].pattern)
        self.assertEqual(tuple(j1.frozen.columns), survival.columns)
        self.assertEqual(s.template_parts(s.TEMPLATES[survival.note], steps={"survival": "s4"}), view_parts(j1.frozen.note))
        cases: tuple[tuple[str, s.ViewerKind], ...] = (("j3-after-confirm", "deDirect"), ("j3-dont-save", "deDirect"), ("j3-deinput", "deInput"))
        for name, launcher in cases:
            spec = last_view(name).viz_specs[0]
            vt = s.VIZ_TEMPLATES[launcher]
            with self.subTest(name):
                self.assertEqual((spec.app_id, spec.launcher, spec.cohort_refs, spec.cites_steps), (vt.app_id, launcher, ["kras_wt", "kras_mut"], ["s9", "s10"]))
                self.assertEqual(spec.title, title(vt.title, {"case": s.cohort_label(MUT_NAME), "control": s.cohort_label(WT_NAME)}))
                assert vt.caption is not None and vt.note is not None and spec.frozen is not None and spec.frozen.note is not None
                self.assertEqual(spec.caption, s.TEMPLATES[vt.caption].pattern)
                self.assertEqual(tuple(spec.frozen.columns), vt.columns)
                note = s.template_parts(s.TEMPLATES[vt.note], steps={"de": "s9"}, values={"rank_short": s.RANK_SHORT["adjusted_p_value"]})
                self.assertEqual(note, view_parts(spec.frozen.note))
        onco = last_view("oncomatrix").viz_specs[0]
        ot = s.VIZ_TEMPLATES["oncomatrix"]
        self.assertEqual((onco.app_id, onco.launcher, onco.caption, onco.cites_steps), (ot.app_id, "oncomatrix", None, ["s3"]))
        # ${cohort} is the ranked cohort's label: its name without " (ORION)" (TOOLS.md 8).
        high = next(c for c in last_view("oncomatrix").cohorts if c.ref == "high")
        self.assertEqual(onco.title, title(ot.title, {"cohort": s.cohort_label(high.name)}))
        assert onco.frozen is not None
        self.assertEqual(tuple(onco.frozen.columns), ot.columns)

    def test_survival_request_reproduces_the_cards_request_pinned_hash(self) -> None:
        record = as_dict(load_json(FIXTURES / "run-record.j1-complete.json"))
        call = next(as_dict(c) for c in as_list(record["calls"]) if as_dict(c)["tool"] == "survival_compare")
        request = s.SurvivalCompareInput.model_validate(call["request"])
        spec = last_view("j1-complete").viz_specs[0]
        self.assertEqual(pinned_hash(derive_request_pinned("survival", dump(request))), spec.request_pinned_hash)


# --- Vectors: request-pinned derivation and the fingerprint ------------------------------------------------------


class VectorTest(unittest.TestCase):
    def setUp(self) -> None:
        if not FIXTURES.is_dir():
            self.skipTest(f"{FIXTURES} is not mounted")

    def test_request_pinned_vectors_validate_against_the_inputs(self) -> None:
        vectors = as_list(as_dict(load_json(FIXTURES / "request-pinned-vectors.json"))["vectors"])
        models: dict[str, tuple[PinnedKind, type[BaseModel]]] = {
            "survival": ("survival", s.SurvivalCompareInput), "oncomatrix": ("oncomatrix", s.TopMutatedGenesInput),
            "deDirect": ("deDirect", s.DeRunInput), "deInput": ("deInput", s.DeRunInput),
        }
        for raw in vectors:
            vector = as_dict(raw)
            launcher, model = models[str(vector["launcher"])]
            with self.subTest(str(vector["name"])):
                request = model.model_validate(vector["request"])
                self.assertEqual(dump(request), vector["request"])
                observed: JsonValue = None
                if "observed" in vector:
                    observed = dump(s.TopMutatedGenesObserved.model_validate(vector["observed"]))
                    self.assertEqual(observed, vector["observed"])
                pinned = derive_request_pinned(launcher, dump(request), observed)
                self.assertEqual(pinned_hash(pinned), vector["requestPinnedHash"])

    def test_fingerprint_vectors_are_valid_observed_documents(self) -> None:
        vectors = [as_dict(v) for v in as_list(as_dict(load_json(FIXTURES / "fingerprint-vectors.json"))["vectors"])]
        for vector in vectors:
            observed = s.FixtureFingerprintObserved.model_validate({"document": vector["document"], "fingerprintSha256": vector["sha256"]})
            self.assertEqual(dump(observed.document), vector["document"])
        with self.assertRaises(ValidationError):
            s.FixtureFingerprintObserved.model_validate({"document": vectors[0]["document"], "fingerprintSha256": "0" * 64})

    def test_confirm_examples_validate_as_save_cohorts_reports(self) -> None:
        examples = as_list(as_dict(load_json(FIXTURES / "request-examples.json"))["examples"])
        confirms = [as_dict(as_dict(as_dict(e)["body"])["forwardedProps"])["orion"] for e in examples]
        confirms = [c for c in confirms if as_dict(c)["action"] == "confirm"]
        # J3 Create with make-active, J3 Don't save and J2's Create of its one cohort.
        self.assertEqual(len(confirms), 3)
        for c in confirms:
            report = ConfirmAction.model_validate(c)
            if report.decision == "create":
                s.SaveCohortsObserved(decision="create", saved=report.saved, make_active=report.make_active)


# --- TOOLS.md and the code agree ------------------------------------------------------------------------------


class CatalogDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        if not (CONTRACTS / "TOOLS.md").is_file():
            self.skipTest(f"{CONTRACTS / 'TOOLS.md'} is not mounted")
        self.text = (CONTRACTS / "TOOLS.md").read_text(encoding="utf-8")

    def test_every_tool_template_and_guard_is_documented(self) -> None:
        names = list(s.TOOL_REGISTRY) + list(s.TEMPLATES)
        names += [g.guard.rule for spec in s.TOOL_REGISTRY.values() for g in spec.guards]
        for name in names:
            self.assertIn(f"`{name}`", self.text, name)

    def test_documented_golden_files_and_engines_match(self) -> None:
        for golden in {g for g in GOLDENS.values() if g is not None}:
            self.assertIn(golden, self.text)
        for engine in (s.ENGINE_GEN3_ANALYSIS, s.ENGINE_GUPPY, s.ENGINE_PROTEINPAINT, s.ENGINE_FINGERPRINT):
            self.assertIn(f"`{engine}`", self.text)
        self.assertIn(s.COHORT_COUNT_QUERY, self.text)

    def test_documented_fact_suffixes_exist(self) -> None:
        suffixes = {fs.suffix for t in s.FACT_SPECS.values() for fs in t} | {fs.suffix for t in s.RANK_FACT_SPECS.values() for fs in t}
        documented = set(re.findall(r"`(?:f\.sN\.)?((?:top\.\$\{pos\}\.)?[a-z0-9]+(?:\.<(?:key|control\|case)>)?)`", self.text))
        for suffix in suffixes:
            shown = suffix.replace("${key}", "<key>")
            self.assertTrue(shown in documented or shown.replace("<key>", "<control|case>") in documented, suffix)


class SettledContractTest(unittest.TestCase):
    """Settings and strings one track needs from another, settled in the catalog (TOOLS.md 2, 5.6 and 6.3)."""

    def test_tool_reasons_are_the_documented_reasons(self) -> None:
        if not (CONTRACTS / "TOOLS.md").is_file():
            self.skipTest(f"{CONTRACTS / 'TOOLS.md'} is not mounted")
        text = (CONTRACTS / "TOOLS.md").read_text(encoding="utf-8")
        section = text.split("### 6.3 Rows, reasons and names", 1)[1].split("###", 1)[0]
        documented: dict[str, str] = {}
        for tools, reason in re.findall(r"^\| (`[a-z_]+`(?:, `[a-z_]+`)*) \| ([^|]+?) \|$", section, re.M):
            for tool in re.findall(r"`([a-z_]+)`", tools):
                documented[tool] = reason
        self.assertEqual(documented, dict(catalog.TOOL_REASONS))

    def test_every_fixture_row_gives_its_tools_reason(self) -> None:
        if not FIXTURES.is_dir():
            self.skipTest(f"{FIXTURES} is not mounted")
        for path in sorted(FIXTURES.glob("*.events.jsonl")):
            for frame in stream(path.name.removesuffix(".events.jsonl")):
                if isinstance(frame, StateSnapshotFrame) and frame.snapshot.plan is not None:
                    for row in frame.snapshot.plan.rows:
                        with self.subTest(path.name, row=row.row):
                            self.assertTrue(row.reason.startswith(catalog.TOOL_REASONS[row.tool]), row.reason)

    def test_the_de_launcher_is_a_deployment_setting(self) -> None:
        from orion_agent.settings import Settings

        self.assertEqual(Settings().de_launcher, "deDirect")
        self.assertEqual(Settings.model_validate({"de_launcher": "deInput"}).de_launcher, "deInput")
        with self.assertRaises(ValidationError):
            Settings.model_validate({"de_launcher": "deFallback"})

    def test_a_nullable_input_may_be_omitted_and_still_writes_null(self) -> None:
        # Gateway's tool schema cannot say "or null": omitting the key equals sending null, and the
        # request a tool records (and hashes) is the same either way.
        cases: list[tuple[type[BaseModel], dict[str, JsonValue], dict[str, JsonValue]]] = [
            (s.TopMutatedGenesInput, {"geneCount": 3}, {"filter0": None}),
            (s.SurvivalCompareInput,
             {"term": "overall_survival", "groups": [{"label": "a", "filter": {"and": []}}, {"label": "b", "filter": {"and": []}}]},
             {"scope": None, "split": None}),
        ]
        for model, given, nulls in cases:
            with self.subTest(model.__name__):
                omitted, explicit = model.model_validate(given), model.model_validate({**given, **nulls})
                self.assertEqual(dump(omitted), dump(explicit))
                self.assertLessEqual(nulls.items(), dump(omitted).items())
                schema = model.model_json_schema(by_alias=True)
                self.assertFalse(set(nulls) & set(schema.get("required", [])))

    def test_a_gene_without_an_snv_entry_keeps_its_place_with_zero_cases(self) -> None:
        # TOOLS.md 2.5 and 2.1: the gene is kept (cases 0); an entry of another dt is dropped with a warning.
        body: JsonValue = {"genes": [
            {"gene": "KRAS", "mutationStat": [{"dt": 1, "count": 480}]},
            {"gene": "MYC", "mutationStat": [{"dt": 4, "count": 12}]},
            {"gene": "TP53", "mutationStat": [{"dt": 1, "count": 301}, {"dt": 4, "count": 3}]},
        ]}
        genes, warnings = normalize.mutated_genes(body)
        self.assertEqual([(g.gene, g.cases) for g in genes], [("KRAS", 480), ("MYC", 0), ("TP53", 301)])
        self.assertEqual(len(warnings), 2)


# --- Helpers and the template grammar ---------------------------------------------------------------------------


class HelperTest(unittest.TestCase):
    def test_placeholder_grammar_equals_interfaces(self) -> None:
        tree = ast.parse((AGENT / "orion_agent" / "interfaces.py").read_text(encoding="utf-8"))
        found = [
            node.value.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "PLACEHOLDER"
            and isinstance(node.value, ast.Call) and isinstance(node.value.args[0], ast.Constant)
        ]
        self.assertEqual(found, [s.PLACEHOLDER.pattern])

    def test_every_template_binds_to_placeholders(self) -> None:
        for t in s.TEMPLATES.values():
            with self.subTest(t.id):
                source = t.pattern + "".join(t.item_patterns.values())
                steps = {role: "f.s2.count" for role in re.findall(r"\{\{<([a-z_]+)>\}\}", source)}
                steps |= {role: "s2" for role in re.findall(r"\{\{f\.<([a-z_]+)>\.", source)}
                values = {name: "x" for name in re.findall(r"\$\{([a-z_]+)\}", source) if name != "items"}
                items = [(name, values) for name in t.item_patterns]
                parts = s.template_parts(t, steps=steps, values=values, items=items)
                for kind, text in parts:
                    if kind == "fact":
                        self.assertTrue(s.PLACEHOLDER.fullmatch("{{" + text + "}}"), text)
                    else:
                        self.assertNotIn("${", text)
                        self.assertNotIn("{{", text)
                self.assertEqual("${items}" in t.pattern, bool(t.item_patterns))
                self.assertEqual(t.code is not None, t.part == "limitation")
                if t.code is not None:
                    self.assertIn(t.code, CODES)

    def test_template_values_are_never_parsed_as_placeholders(self) -> None:
        parts = s.template_parts(s.TEMPLATES["size.count"], steps={"count": "f.s2.count"}, values={"cohort": "{{f.s1.cases}} ${items}"})
        self.assertEqual(parts, [("text", "{{f.s1.cases}} ${items}: "), ("fact", "f.s2.count"), ("text", " cases.")])
        with self.assertRaises(KeyError):
            s.template_parts(s.TEMPLATES["size.count"], steps={"count": "f.s2.count"})
        with self.assertRaises(ValueError):
            s.template_parts(s.TEMPLATES["survival.group"], steps={"count": "s2", "survival": "s4"}, values={"group": "A", "key": "not a key"})

    def test_group_keys_and_labels(self) -> None:
        self.assertEqual(s.group_keys(["High risk", "Standard risk"]), ["high", "standard"])
        self.assertEqual(s.group_keys(["Stage I", "Stage II", "Stage III"]), ["i", "ii", "iii"])
        self.assertEqual(s.group_keys([MUT_NAME, WT_NAME]), ["mutated", "not_detected"])
        self.assertEqual(s.group_keys(["female", "male"]), ["female", "male"])
        self.assertEqual(s.group_keys(["Same", "Same"]), ["g1", "g2"])
        self.assertEqual(s.group_keys(["Risk", "High risk"]), ["g1", "g2"])
        self.assertEqual(s.cases_label("High risk"), "High risk cases")
        self.assertEqual(s.cases_label(WT_NAME), "KRAS not detected cases")
        self.assertEqual(s.cases_label("Mutation-tested cases"), "Mutation-tested cases")
        self.assertEqual(s.cohort_label(MUT_NAME), "KRAS mutated")

    def test_fact_ids(self) -> None:
        self.assertEqual(s.fact_id("s10", "top.1.gene"), "f.s10.top.1.gene")
        self.assertEqual(s.split_fact_id("f.s4.events.high"), ("s4", "events.high"))
        for bad in ("f.s0.count", "f.s1", "s1.count", "f.s1.a b"):
            with self.assertRaises(ValueError):
                s.split_fact_id(bad)
        with self.assertRaises(ValueError):
            s.fact_id("s1", "not a suffix")

    def test_resolve_pointer(self) -> None:
        doc: JsonValue = {"a/b": {"m~n": [10, 20]}, "x": None}
        self.assertEqual(s.resolve_pointer(doc, "/a~1b/m~0n/1"), 20)
        self.assertIsNone(s.resolve_pointer(doc, "/x"))
        self.assertEqual(s.resolve_pointer(doc, ""), doc)
        for bad in ("/missing", "/a~1b/m~0n/2", "/a~1b/m~0n/01", "/x/y", "nope"):
            with self.assertRaises(s.PointerError):
                s.resolve_pointer(doc, bad)

    def test_resolve_step_refs(self) -> None:
        outputs: dict[str, JsonValue] = {
            "s2": {"observed": {"caseIds": ["C3", "C1", "C9"]}},
            "s3": {"observed": {"caseIds": ["C1", "C2", "C3"]}},
            "s6": {"observed": {"groups": [{"sampleIds": [2, 3]}, {"sampleIds": [1]}]}},
        }

        def read(step: str, pointer: str) -> JsonValue:
            return s.resolve_pointer(outputs[step], pointer)

        args: JsonValue = {
            "filter": {"and": [{"in": {"case_id": {"ref": "s2", "pointer": "/observed/caseIds"}}}, {"in": {"f": ["v"]}}]},
            "groups": [{"key": "case", "caseIds": {"ref": "s2", "pointer": "/observed/caseIds", "intersect": "s3"}}],
            "sampleIds": {"ref": "s6", "pointer": "/observed/groups/1/sampleIds"},
            "notARef": {"ref": "s2", "pointer": "/observed/caseIds", "other": 1},
        }
        resolved = as_dict(s.resolve_step_refs(args, read))
        self.assertEqual(resolved["filter"], {"and": [{"in": {"case_id": ["C3", "C1", "C9"]}}, {"in": {"f": ["v"]}}]})
        self.assertEqual(resolved["groups"], [{"key": "case", "caseIds": ["C1", "C3"]}])
        self.assertEqual(resolved["sampleIds"], [1])
        self.assertEqual(resolved["notARef"], as_dict(args)["notARef"])
        self.assertTrue(s.is_step_ref(s.StepRef(ref="s2", pointer="/observed/caseIds").model_dump(mode="json")))

    def test_de_run_input_reads_the_read_back(self) -> None:
        args = s.DeRunStepArgs.model_validate({
            "groups": [
                {"name": WT_NAME, "cohort": "kras_wt", "sampleIds": {"ref": "s6", "pointer": "/observed/groups/0/sampleIds"}},
                {"name": MUT_NAME, "cohort": "kras_mut", "sampleIds": {"ref": "s6", "pointer": "/observed/groups/1/sampleIds"}},
            ],
            "method": "wilcoxon", "thresholds": {"pValueCutoff": 0.05, "pValueType": "adjusted", "foldChangeCutoff": 0.3},
        })
        groups: dict[str, JsonValue] = {"groups": [{"sampleIds": [2, 5]}, {"sampleIds": [1, 3]}]}

        def read(step: str, pointer: str) -> JsonValue:
            return s.resolve_pointer({"observed": groups}, pointer)

        unsaved = s.de_run_input(args, read, {})
        self.assertEqual(dump(unsaved)["groups"], [{"name": WT_NAME, "sampleIds": [2, 5]}, {"name": MUT_NAME, "sampleIds": [1, 3]}])
        saved = s.de_run_input(args, read, {
            "kras_wt": s.SavedGroup("KRAS not detected (ORION) 2", "c-1", s.case_id_filter(["SYNTH-CASE-0005", "SYNTH-CASE-0002"])),
        })
        first = as_dict(as_list(dump(saved)["groups"])[0])
        self.assertEqual(first["name"], "KRAS not detected (ORION) 2")
        self.assertEqual(first["filter0"], {"and": [{"in": {"case_id": ["SYNTH-CASE-0002", "SYNTH-CASE-0005"]}}]})
        pinned = derive_request_pinned("deInput", {**dump(saved), "groups": [first, first]})
        self.assertEqual(pinned.kind, "deInput")

    def test_minus_log10_matches_the_volcano_pin(self) -> None:
        for cutoff in (0.05, 0.01, 0.001, 0.1, 0.5, 1.0, 1e-6):
            request: dict[str, JsonValue] = {
                "groups": [{"name": "a", "sampleIds": [1]}, {"name": "b", "sampleIds": [2]}], "method": "wilcoxon",
                "thresholds": {"pValueCutoff": cutoff, "pValueType": "adjusted", "foldChangeCutoff": 0.3},
            }
            pinned = derive_request_pinned("deDirect", request)
            assert pinned.kind == "deDirect" and pinned.volcano is not None
            self.assertEqual(pinned.volcano.p_value, s.minus_log10(cutoff), cutoff)
        with self.assertRaises(ValueError):
            s.minus_log10(0)

    def test_input_validators(self) -> None:
        with self.assertRaises(ValidationError):
            s.SetOperationInput.model_validate({"op": "difference", "a": "s3"})
        with self.assertRaises(ValidationError):
            s.SetOperationInput.model_validate({"op": "size", "a": "s3", "b": "s2"})
        self.assertEqual(dump(s.SetOperationInput.model_validate({"op": "size", "a": "s3"})), {"op": "size", "a": "s3"})
        with self.assertRaises(ValidationError):
            s.SampleListsInput.model_validate({"groups": [{"key": "case", "caseIds": []}, {"key": "control", "caseIds": []}]})
        with self.assertRaises(ValidationError):
            s.DeRunGroup.model_validate({"name": "a", "sampleIds": [1], "cohortId": "c-1"})
        with self.assertRaises(ValidationError):
            s.DeRunInput.model_validate({"groups": [{"name": "a", "sampleIds": [1]}, {"name": "a", "sampleIds": [2]}], "method": "wilcoxon"})
        with self.assertRaises(ValidationError):
            s.GeneCasesInput.model_validate({"gene": "OTHER", "cohortFilter": {"and": []}})
        with self.assertRaises(ValidationError):
            s.SurvivalCompareInput.model_validate({"scope": None, "term": "overall_survival", "split": None,
                                                   "groups": [{"label": "A", "filter": {}}, {"label": "A", "filter": {}}]})
        with self.assertRaises(ValidationError):
            s.CasesQueryObserved.model_validate({"count": 2, "caseIds": ["B", "A"], "pages": 1, "complete": True})
        with self.assertRaises(ValidationError):
            s.DeRunObserved.model_validate({"method": "wilcoxon", "sampleSizes": {"control": 1, "case": 1},
                                            "totals": {"tested": 5, "significant": 1, "up": 1, "down": 0}, "genes": [], "cacheId": None})
        with self.assertRaises(ValidationError):
            s.CohortCountInput.model_validate({"filter": {}, "extra": 1})
        self.assertEqual(s.case_id_filter(["B", "A", "B"]), {"and": [{"in": {"case_id": ["A", "B"]}}]})
        self.assertEqual(s.PANEL_GENES, tuple(s.PANEL_GENE_IDS))
        self.assertTrue(math.isclose(s.VIEWER_P_FLOOR, 1e-16))


if __name__ == "__main__":
    unittest.main()
