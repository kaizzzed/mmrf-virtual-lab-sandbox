"""Each server tool against a scripted stack: normalisation into Observed, pre- and post-guards with
their limitation codes, the error mapping and the ToolResult envelope with provenance."""

import asyncio
import json
import os
import unittest
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar, cast
from unittest import mock

from test_tools_fakes import (
    CASES,
    MUTATED,
    RUN_ID,
    FakeTransport,
    cases_pages,
    de_body,
    donors,
    fake_stack,
    fixed,
    goldens,
    guppy,
    json_response,
    obj,
    raises,
    top_genes,
    vlab,
)

from orion_agent.models import canonical_json, sha256_hex
from orion_agent.tools import server
from orion_agent.tools.base import TOOL_HTTP_TIMEOUT_S, ServerToolName, ToolContext, ToolResult
from orion_agent.tools.guards import body_within_limit
from orion_agent.tools.http import HttpBodyTooLarge, HttpConnectError, HttpResponse, HttpTimeout
from orion_agent.tools.normalize import survival_curve
from orion_agent.tools.schemas import (
    ENGINE_GEN3_ANALYSIS,
    SERVER_TOOLS,
    TOOL_REGISTRY,
    FixtureFingerprintObserved,
    case_id_filter,
)
from orion_agent.tools.server import TOOL_CLASSES, VLAB_TOOLS, build_server_tools

T = TypeVar("T")
CONTRACTS = Path(os.environ.get("ORION_CONTRACTS_DIR", "/contracts"))
CTX = ToolContext(run_id=RUN_ID, step_id="s2", call_id=f"{RUN_ID}.v1.s2.c1", attempt=1, timeout_s=TOOL_HTTP_TIMEOUT_S)
HIGH: dict[str, Any] = {"and": [{"in": {"cgs_risk_key_criteria.cgs_risk_category": ["High risk"]}}]}


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def error_code(result: ToolResult) -> str | None:
    return result.error.code if result.error else None


def invoke(name: str, input: Any, transport: FakeTransport | None = None, ctx: ToolContext = CTX) -> tuple[ToolResult, FakeTransport]:
    transport = transport or fake_stack()
    tools = build_server_tools(vlab(transport), goldens())
    return run(tools[cast(ServerToolName, name)].invoke(input, ctx)), transport


class ContractTests(unittest.TestCase):
    def test_every_server_tool_has_a_handler_with_the_contract_shape(self) -> None:
        self.assertEqual(set(TOOL_CLASSES), SERVER_TOOLS)
        self.assertEqual(VLAB_TOOLS, SERVER_TOOLS - {"kg_query"})
        tools = build_server_tools(vlab(FakeTransport()), goldens())
        for name, tool in tools.items():
            spec = TOOL_REGISTRY[name]
            self.assertEqual(tool.version, f"{name}@1")
            self.assertEqual(list(tool.guards), spec.contract_guards())
            self.assertEqual(tool.timeout_s, TOOL_HTTP_TIMEOUT_S)
            self.assertEqual(tool.input_schema["type"], "object")
            self.assertEqual(tool.output_schema["type"], "object")
            self.assertEqual(tool.endpoint, ", ".join(spec.endpoints))
            self.assertEqual(tool.golden_template is None, spec.golden is None, name)


class NormalisationTests(unittest.TestCase):
    def test_fixture_fingerprint_reads_four_sources_in_order(self) -> None:
        result, transport = invoke("fixture_fingerprint", {})
        self.assertEqual(result.status, "ok")
        self.assertEqual(transport.paths(), ["/sandbox/status", "/analysis/v0/_version", "/protein-paint/healthcheck", "/protein-paint/termdb/topMutatedGenes"])
        self.assertEqual([p.method for p in result.provenance], ["GET", "GET", "GET", "POST"])
        self.assertEqual(json.loads(transport.sent[3].body or b"")["maxGenes"], 50)
        observed = FixtureFingerprintObserved.model_validate(result.observed)
        self.assertEqual(observed.document.sandbox.cases, 12)
        self.assertEqual([g.gene for g in observed.document.ranking], ["KRAS", "NRAS", "TP53", "DIS3"])

    @unittest.skipUnless((CONTRACTS / "fixtures" / "fingerprint-vectors.json").is_file(), "contracts are not mounted")
    def test_fixture_fingerprint_reproduces_the_fingerprint_vectors(self) -> None:
        vectors = json.loads((CONTRACTS / "fixtures" / "fingerprint-vectors.json").read_text())["vectors"]
        for vector in vectors:
            sources = vector["sources"]
            transport = FakeTransport({
                "/sandbox/status": fixed(200, sources["sandboxStatus"]),
                "/analysis/v0/_version": fixed(200, sources["analysisVersion"]),
                "/protein-paint/healthcheck": fixed(200, sources["healthcheck"]),
                "/protein-paint/termdb/topMutatedGenes": top_genes([(r["gene"], r["cases"]) for r in sources["ranking"]]),
            })
            result, _ = invoke("fixture_fingerprint", {}, transport)
            self.assertEqual(obj(result.observed)["document"], vector["document"], vector["name"])
            self.assertEqual(obj(result.observed)["fingerprintSha256"], vector["sha256"], vector["name"])

    def test_fixture_fingerprint_without_version_info_is_an_upstream_shape_error(self) -> None:
        transport = fake_stack()
        transport.routes["/protein-paint/healthcheck"] = fixed(200, {"status": "ok"})
        result, _ = invoke("fixture_fingerprint", {}, transport)
        self.assertEqual((result.status, result.error and result.error.code), ("error", "UPSTREAM_SHAPE"))
        self.assertEqual(len(result.provenance), 3)

    def test_cohort_count_reads_the_total_and_reports_a_zero_as_empty(self) -> None:
        result, transport = invoke("cohort_count", {"filter": HIGH})
        self.assertEqual((result.status, result.observed, result.limitation), ("ok", {"count": 7}, None))
        body = transport.sent[0].json
        assert isinstance(body, dict)
        self.assertEqual(body["variables"], {"filter": HIGH})
        zero = fake_stack()
        zero.routes["/guppy/graphql"] = guppy(0)
        result, _ = invoke("cohort_count", {"filter": HIGH}, zero)
        self.assertEqual((result.status, result.observed, result.limitation), ("empty", {"count": 0}, "EMPTY_COHORT"))

    def test_a_graphql_error_at_200_is_never_a_zero(self) -> None:
        transport = fake_stack()
        transport.routes["/guppy/graphql"] = fixed(200, {"errors": [{"message": "Field path not found: nope.field"}]})
        result, _ = invoke("cohort_count", {"filter": {"and": [{"in": {"nope.field": ["x"]}}]}}, transport)
        self.assertEqual((result.status, result.error and result.error.code), ("error", "UPSTREAM_SHAPE"))
        self.assertIn("Field path not found", result.error.message if result.error else "")
        self.assertEqual(result.provenance[0].http_status, 200)

    def test_cases_query_pages_until_every_id_is_read(self) -> None:
        ids = [f"SYNTH-CASE-{i:04d}" for i in range(1, 2501)]
        transport = FakeTransport({"/analysis/v0/cases/": cases_pages(list(reversed(ids)))})
        result, _ = invoke("cases_query", {"filter": {"and": []}, "pageSize": 1000, "maxIds": 10000}, transport)
        self.assertEqual(result.status, "ok")
        self.assertEqual((obj(result.observed)["count"], obj(result.observed)["pages"], obj(result.observed)["complete"]), (2500, 3, True))
        self.assertEqual(obj(result.observed)["caseIds"], ids)
        self.assertEqual([(b["offset"], b["size"]) for b in (r.json for r in transport.sent) if isinstance(b, dict)], [(0, 1000), (1000, 1000), (2000, 1000)])
        self.assertEqual(len(result.provenance), 3)

    def test_cases_query_never_sends_offset_plus_size_over_max_ids(self) -> None:
        ids = [f"SYNTH-CASE-{i:04d}" for i in range(1, 2501)]
        transport = FakeTransport({"/analysis/v0/cases/": cases_pages(ids)})
        result, _ = invoke("cases_query", {"filter": {"and": []}, "pageSize": 1000, "maxIds": 2500}, transport)
        self.assertEqual(result.status, "ok")
        self.assertEqual([(b["offset"], b["size"]) for b in (r.json for r in transport.sent) if isinstance(b, dict)], [(0, 1000), (1000, 1000), (2000, 500)])

    def test_cases_query_over_max_ids_is_too_many_ids_without_paging(self) -> None:
        ids = [f"SYNTH-CASE-{i:04d}" for i in range(1, 13)]
        result, transport = invoke("cases_query", {"filter": {"and": []}, "pageSize": 5, "maxIds": 10}, FakeTransport({"/analysis/v0/cases/": cases_pages(ids)}))
        self.assertEqual((result.status, result.limitation), ("ok", "TOO_MANY_IDS"))
        self.assertEqual(result.observed, {"count": 12, "caseIds": [], "pages": 1, "complete": False})
        self.assertEqual(len(transport.sent), 1)

    def test_cases_query_zero_and_inconsistent_pages(self) -> None:
        result, _ = invoke("cases_query", {"filter": {"and": []}}, FakeTransport({"/analysis/v0/cases/": cases_pages([])}))
        self.assertEqual((result.status, result.limitation, result.observed), ("empty", "EMPTY_COHORT", {"count": 0, "caseIds": [], "pages": 1, "complete": True}))
        pages = [
            fixed(200, {"data": [{"case_id": "SYNTH-CASE-0001"}], "pagination": {"total": 3}}),
            fixed(200, {"data": [], "pagination": {"total": 3}}),
        ]
        result, _ = invoke("cases_query", {"filter": {"and": []}, "pageSize": 1, "maxIds": 10}, FakeTransport({"/analysis/v0/cases/": pages}))
        self.assertEqual((result.status, result.error and result.error.code), ("error", "UPSTREAM_SHAPE"))

    def test_gene_cases_sorts_and_deduplicates_and_checks_the_panel(self) -> None:
        result, transport = invoke("gene_cases", {"gene": "KRAS", "cohortFilter": {"and": []}})
        self.assertEqual(result.observed, {"gene": "KRAS", "count": 4, "caseIds": sorted(MUTATED)})
        body = transport.sent[0].json
        assert isinstance(body, dict)
        self.assertEqual(body["gene_filter"], {"and": [{"in": {"gene_id": ["ENSG00000133703"]}}]})
        result, transport = invoke("gene_cases", {"gene": "EGFR", "cohortFilter": {"and": []}})
        self.assertEqual((result.status, result.error and result.error.code, result.limitation), ("error", "GUARD_FAILED", "OUT_OF_PANEL"))
        self.assertEqual(transport.sent, [])
        empty = fake_stack()
        empty.routes["/analysis/v0/genomic/gene_cases"] = fixed(200, [])
        result, _ = invoke("gene_cases", {"gene": "MYC", "cohortFilter": {"and": []}}, empty)
        self.assertEqual((result.status, result.limitation), ("empty", "EMPTY_COHORT"))

    def test_top_mutated_genes_keeps_the_extra_row_as_next_gene(self) -> None:
        result, transport = invoke("top_mutated_genes", {"filter0": case_id_filter(CASES), "geneCount": 2})
        self.assertEqual(result.observed, {"genes": [{"gene": "KRAS", "cases": 4}, {"gene": "NRAS", "cases": 3}], "nextGene": {"gene": "TP53", "cases": 3}})
        body = transport.sent[0].json
        assert isinstance(body, dict)
        self.assertEqual((body["maxGenes"], body["filter0"]), (3, case_id_filter(CASES)))
        result, _ = invoke("top_mutated_genes", {"filter0": None, "geneCount": 10})
        self.assertNotIn("nextGene", obj(result.observed))
        self.assertEqual(len(obj(result.observed)["genes"]), 4)

    def test_top_mutated_genes_counts_snv_indel_only_and_reports_no_genes_as_empty(self) -> None:
        transport = fake_stack()
        transport.routes["/protein-paint/termdb/topMutatedGenes"] = fixed(
            200, {"genes": [{"gene": "KRAS", "mutationStat": [{"dt": 1, "count": 5}, {"dt": 4, "count": 2}]}, {"gene": "MYC", "mutationStat": [{"dt": 4, "count": 9}]}]}
        )
        result, _ = invoke("top_mutated_genes", {"filter0": None, "geneCount": 3}, transport)
        self.assertEqual(result.observed, {"genes": [{"gene": "KRAS", "cases": 5}, {"gene": "MYC", "cases": 0}]})
        self.assertEqual(len(result.warnings), 2)
        transport.routes["/protein-paint/termdb/topMutatedGenes"] = fixed(200, {"genes": []})
        result, _ = invoke("top_mutated_genes", {"filter0": None, "geneCount": 3}, transport)
        self.assertEqual((result.status, result.limitation, result.observed), ("empty", "FEWER_THAN_N", {"genes": []}))

    def test_top_mutated_genes_refuses_a_filter_the_bridge_cannot_read(self) -> None:
        for filter0 in (
            {"and": [{"in": {"gene.symbol": ["KRAS"]}}]},
            {"and": [{"in": {"available_variation_data": ["ssm"]}}]},
            {"and": [{"not": {"in": {"demographic.gender": ["female"]}}}]},
            {"and": [{"excludes": {"demographic.gender": ["female"]}}]},
        ):
            result, transport = invoke("top_mutated_genes", {"filter0": filter0, "geneCount": 3})
            self.assertEqual((result.error and result.error.code, result.limitation), ("GUARD_FAILED", "UNSUPPORTED_FIELD"), filter0)
            self.assertEqual(transport.sent, [])

    def test_survival_compare_calls_each_group_then_all_groups(self) -> None:
        groups = [{"label": "High risk", "filter": HIGH}, {"label": "Standard risk", "filter": {"and": [{"in": {"cgs_risk_key_criteria.cgs_risk_category": ["Standard risk"]}}]}}]
        request = {"scope": None, "term": "overall_survival", "split": {"field": "cgs_risk_key_criteria.cgs_risk_category", "overlay": "cgs_risk_category"}, "groups": groups}
        result, transport = invoke("survival_compare", request)
        self.assertEqual(result.status, "ok")
        self.assertEqual([len(r.json["filters"]) for r in transport.sent], [1, 1, 2])
        self.assertEqual((obj(result.observed)["pValue"], obj(result.observed)["engine"]), (0.0123, ENGINE_GEN3_ANALYSIS))
        first = obj(result.observed)["groups"][0]
        self.assertEqual((first["label"], first["cases"], first["events"]), ("High risk", 4, 2))
        self.assertEqual(len(result.provenance), 3)

    def test_the_curve_is_the_engines_estimate_just_after_each_time(self) -> None:
        # Gen3 Analysis: an event donor carries the estimate before its event, a censored donor the one after.
        spec = [(10, False, 1.0), (10, False, 1.0), (20, True, 0.6), (30, False, 0.6), (40, True, 0.3), (50, False, 0.3)]
        curve = [(p.time, p.survival) for p in survival_curve(donors(spec))]
        self.assertEqual(curve, [(0.0, 1.0), (10.0, 0.6), (20.0, 0.6), (30.0, 0.3), (40.0, 0.3), (50.0, 0.0)])
        self.assertEqual([(p.time, p.survival) for p in survival_curve(donors([(5, True, 1.0), (9, True, 1.0)]))], [(0.0, 1.0), (5.0, 1.0), (9.0, 1.0)])

    def test_a_group_without_survival_data_is_empty_and_skips_the_combined_call(self) -> None:
        answers = [
            fixed(200, {"results": [{"meta": {}, "donors": donors([(10, False, 1.0), (20, True, 0.5)])}], "overallStats": {}}),
            fixed(200, {"results": [], "overallStats": {}}),
        ]
        groups = [{"label": "A", "filter": HIGH}, {"label": "B", "filter": HIGH}]
        result, transport = invoke(
            "survival_compare", {"scope": None, "term": "overall_survival", "split": None, "groups": groups},
            FakeTransport({"/analysis/v0/survival/": answers}),
        )
        self.assertEqual((result.status, result.limitation), ("empty", "EMPTY_COHORT"))
        self.assertIsNone(obj(result.observed)["pValue"])
        self.assertEqual(obj(result.observed)["groups"][1], {"label": "B", "cases": 0, "events": 0, "curve": []})
        self.assertEqual(len(transport.sent), 2)

    def test_sample_lists_maps_each_group_and_guards_both_lists(self) -> None:
        request = {"groups": [{"key": "control", "caseIds": ["SYNTH-CASE-0003", "SYNTH-CASE-0001"]}, {"key": "case", "caseIds": MUTATED}]}
        result, transport = invoke("sample_lists", request)
        self.assertEqual(result.observed, {"groups": [{"key": "control", "count": 2, "sampleIds": [1, 3]}, {"key": "case", "count": 4, "sampleIds": [2, 5, 7, 11]}]})
        self.assertEqual([r.json["filter0"] for r in transport.sent], [case_id_filter(["SYNTH-CASE-0001", "SYNTH-CASE-0003"]), case_id_filter(MUTATED)])
        result, transport = invoke("sample_lists", {"groups": [{"key": "control", "caseIds": []}, {"key": "case", "caseIds": MUTATED}]})
        self.assertEqual((result.error and result.error.code, result.limitation), ("GUARD_FAILED", "EMPTY_COHORT"))
        self.assertEqual(transport.sent, [])
        none = fake_stack()
        none.routes["/protein-paint/termdb"] = fixed(200, [])
        result, _ = invoke("sample_lists", request, none)
        self.assertEqual((result.status, result.limitation), ("empty", "EMPTY_COHORT"))

    def test_the_sample_list_body_guard_is_the_gateways_8_mib(self) -> None:
        self.assertIsNone(body_within_limit("control", {"ids": ["x" * 100] * 10}))
        violation = body_within_limit("case", {"ids": ["x" * 1024] * 8200})
        assert violation is not None
        self.assertEqual((violation.code, violation.group), ("REQUEST_TOO_LARGE", "case"))

    def test_de_preanalysis_counts_eligible_samples_and_flags_small_groups(self) -> None:
        big = list(range(1, 21))
        result, _ = invoke("de_preanalysis", {"groups": [{"key": "control", "sampleIds": big}, {"key": "case", "sampleIds": big[:12]}]})
        self.assertEqual((result.status, result.limitation), ("ok", None))
        self.assertEqual(result.observed, {"groups": [{"key": "control", "eligible": 20}, {"key": "case", "eligible": 12}]})
        result, _ = invoke("de_preanalysis", {"groups": [{"key": "control", "sampleIds": big}, {"key": "case", "sampleIds": [1, 2]}]})
        self.assertEqual((result.status, result.limitation), ("ok", "INELIGIBLE_GROUP"))
        result, transport = invoke("de_preanalysis", {"groups": [{"key": "control", "sampleIds": list(range(1, 2002))}, {"key": "case", "sampleIds": list(range(1, 2001))}]})
        self.assertEqual((result.error and result.error.code, result.limitation), ("GUARD_FAILED", "REQUEST_TOO_LARGE"))
        self.assertEqual(transport.sent, [])

    def test_de_run_keeps_the_rows_and_drops_the_drawing(self) -> None:
        request = {"groups": [{"name": "KRAS not detected (ORION)", "sampleIds": [1, 3]}, {"name": "KRAS mutated (ORION)", "sampleIds": [2, 5]}], "method": "wilcoxon"}
        result, _ = invoke("de_run", request)
        self.assertEqual(result.status, "ok")
        self.assertEqual(obj(result.observed)["totals"], {"tested": 7684, "significant": 3, "up": 1, "down": 2})
        self.assertEqual(obj(result.observed)["sampleSizes"], {"control": 2, "case": 2})
        self.assertEqual(obj(result.observed)["genes"][0], {"gene_name": "BRAF", "fold_change": -1.78, "original_p_value": 7.7e-135, "adjusted_p_value": 5.9e-131})
        self.assertEqual(obj(result.observed)["cacheId"], "a04534ac6567948134d7534646e74f23")
        self.assertNotIn("volcanoPng", json.dumps(result.observed))

    def test_de_run_refuses_a_response_that_does_not_add_up(self) -> None:
        request = {"groups": [{"name": "a", "sampleIds": [1]}, {"name": "b", "sampleIds": [2]}], "method": "wilcoxon"}
        transport = fake_stack()
        transport.routes["/protein-paint/termdb/DE"] = fixed(200, de_body(["a", "b"], [1, 1], method="edgeR"))
        self.assertEqual(error_code(invoke("de_run", request, transport)[0]), "UPSTREAM_SHAPE")
        body = de_body(["a", "b"], [1, 1])
        body["data"]["totalSignificantRows"] = 9
        transport.routes["/protein-paint/termdb/DE"] = fixed(200, body)
        self.assertEqual(error_code(invoke("de_run", request, transport)[0]), "UPSTREAM_SHAPE")
        many = {"groups": [{"name": "a", "sampleIds": list(range(1, 4002))}, {"name": "b", "sampleIds": [1]}], "method": "wilcoxon"}
        result, sent = invoke("de_run", many)
        self.assertEqual((result.limitation, sent.sent), ("REQUEST_TOO_LARGE", []))

    def test_kg_query_is_not_connected_and_makes_no_call(self) -> None:
        result, transport = invoke("kg_query", {"question": "What is known about KRAS?", "genes": ["KRAS"]})
        self.assertEqual((result.status, result.error and result.error.code, result.limitation), ("unavailable", "NOT_CONNECTED", "KG_NOT_CONNECTED"))
        self.assertEqual(transport.sent, [])


class ErrorMappingTests(unittest.TestCase):
    def count_with(self, responder: Any) -> ToolResult:
        return invoke("cohort_count", {"filter": HIGH}, FakeTransport({"/guppy/graphql": responder}))[0]

    def test_statuses_map_to_tool_errors(self) -> None:
        cases = {
            429: ("CACHE_BUSY", True), 502: ("HTTP_ERROR", True), 503: ("HTTP_ERROR", True), 504: ("HTTP_ERROR", True),
            400: ("HTTP_ERROR", False), 401: ("HTTP_ERROR", False), 404: ("HTTP_ERROR", False), 500: ("HTTP_ERROR", False),
        }
        for status, (code, retryable) in cases.items():
            result = self.count_with(fixed(status, {"error": "Invalid sandbox request"}))
            assert result.error is not None
            self.assertEqual((result.status, result.error.code, result.error.retryable, result.error.http_status), ("error", code, retryable, status))
            self.assertEqual((result.observed, result.limitation, result.provenance[0].http_status), (None, "TOOL_FAILED", status))
        failed = self.count_with(fixed(400, {"error": "Invalid sandbox request"})).error
        self.assertIn("Invalid sandbox request", failed.message if failed else "")

    def test_transport_failures_map_to_tool_errors(self) -> None:
        for error, code, retryable in (
            (HttpTimeout("slow"), "TIMEOUT", True), (HttpConnectError("refused"), "HTTP_ERROR", True), (HttpBodyTooLarge("big"), "UPSTREAM_SHAPE", False),
        ):
            result = self.count_with(raises(error))
            self.assertEqual((result.error and result.error.code, result.error and result.error.retryable), (code, retryable))
            self.assertEqual(len(result.provenance), 1)
            self.assertIsNone(result.provenance[0].http_status)

    def test_a_2xx_body_that_is_not_json_is_an_upstream_shape_error(self) -> None:
        result = self.count_with(lambda r: HttpResponse(200, (), b"<html></html>"))
        self.assertEqual(result.error and result.error.code, "UPSTREAM_SHAPE")

    def test_invalid_input_is_never_sent(self) -> None:
        for name, bad in (("cohort_count", {"filter": HIGH, "extra": 1}), ("cases_query", {"filter": {"and": []}, "pageSize": 5000}), ("de_run", {"groups": []})):
            result, transport = invoke(name, bad)
            self.assertEqual((result.error and result.error.code, result.limitation, transport.sent), ("INVALID_INPUT", "TOOL_FAILED", []), name)

    def test_no_time_left_is_a_timeout_before_any_request(self) -> None:
        ctx = CTX.model_copy(update={"timeout_s": 0.000001})
        result, transport = invoke("cohort_count", {"filter": HIGH}, ctx=ctx)
        self.assertEqual(result.error and result.error.code, "TIMEOUT")

    def test_an_output_over_the_cap_is_an_upstream_shape_error(self) -> None:
        with mock.patch.object(server, "MAX_OBSERVED_BYTES", 100):
            result, _ = invoke("gene_cases", {"gene": "KRAS", "cohortFilter": {"and": []}})
        self.assertEqual((result.status, result.error and result.error.code), ("error", "UPSTREAM_SHAPE"))
        self.assertEqual(len(result.provenance), 1)

    def test_each_request_gets_what_remains_of_the_attempt(self) -> None:
        result, transport = invoke("survival_compare", {"scope": None, "term": "overall_survival", "split": None,
                                                         "groups": [{"label": "A", "filter": HIGH}, {"label": "B", "filter": HIGH}]})
        self.assertEqual(result.status, "ok")
        budgets = [r.timeout_s for r in transport.sent]
        self.assertTrue(all(0 < b <= TOOL_HTTP_TIMEOUT_S for b in budgets))
        self.assertEqual(budgets, sorted(budgets, reverse=True))

    def test_the_provenance_request_hash_is_the_canonical_payload(self) -> None:
        result, transport = invoke("cohort_count", {"filter": HIGH})
        sent = transport.sent[0].json
        self.assertEqual(result.provenance[0].request_sha256, sha256_hex(canonical_json(sent, portable=False)))
        self.assertEqual(result.provenance[0].response_sha256, sha256_hex(canonical_json(json.loads(json_response(200, {"data": {"CaseCentric__aggregation": {"case_centric": {"_totalCount": 7}}}}).body), portable=False)))


if __name__ == "__main__":
    unittest.main()
