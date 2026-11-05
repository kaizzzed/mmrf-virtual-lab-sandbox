"""The executor's four pure functions, versioned, and their steps' reads and post-guards."""

import asyncio
import unittest
from typing import Any

from pydantic import JsonValue
from test_tools_fakes import RUN_ID, ManualClock, obj

from orion_agent.models import canonical_json, sha256_hex
from orion_agent.tools.base import KmCurve, KmPoint, ToolContext
from orion_agent.tools.functions import (
    FunctionError,
    FunctionTool,
    call_function,
    check_subset,
    derive_km_median,
    rank_top_n,
    run_function,
    set_operation,
)
from orion_agent.tools.records import call_records
from orion_agent.tools.schemas import StepValueReader, resolve_pointer

A = ["SYNTH-CASE-0003", "SYNTH-CASE-0001", "SYNTH-CASE-0002", "SYNTH-CASE-0001"]
B = ["SYNTH-CASE-0002", "SYNTH-CASE-0004"]


def reader(steps: dict[str, Any]) -> StepValueReader:
    """The executor's reader: a pointer into a step's last call ({"observed": ...}); LookupError when absent."""

    def read(step_id: str, pointer: str) -> JsonValue:
        return resolve_pointer({"observed": steps[step_id]}, pointer)

    return read


def de_row(gene: str, fc: float, p: float, padj: float) -> dict[str, JsonValue]:
    return {"gene_name": gene, "fold_change": fc, "original_p_value": p, "adjusted_p_value": padj}


class PureFunctionTests(unittest.TestCase):
    def test_set_operation(self) -> None:
        self.assertEqual(set_operation("difference", A, B).case_ids, ["SYNTH-CASE-0001", "SYNTH-CASE-0003"])
        self.assertEqual(set_operation("intersection", A, B).case_ids, ["SYNTH-CASE-0002"])
        union = set_operation("union", A, B)
        self.assertEqual((union.count, union.a_count, union.b_count), (4, 3, 2))
        size = set_operation("size", A)
        self.assertEqual((size.count, size.case_ids, size.b_count, size.version), (3, [], None, "set_operation@1"))
        with self.assertRaises(FunctionError):
            set_operation("size", A, B)
        with self.assertRaises(FunctionError):
            set_operation("union", [f"SYNTH-CASE-{i:05d}" for i in range(6000)], [f"SYNTH-CASE-{i:05d}" for i in range(6000, 12000)])

    def test_check_subset(self) -> None:
        inside = check_subset(["SYNTH-CASE-0002"], A)
        self.assertEqual((inside.is_subset, inside.outside_count, inside.version), (True, 0, "check_subset@1"))
        outer = [f"SYNTH-CASE-{i:04d}" for i in range(1, 5)]
        inner = [f"SYNTH-CASE-{i:04d}" for i in range(30, 0, -1)]
        outside = check_subset(inner, outer)
        self.assertEqual((outside.is_subset, outside.inner_count, outside.outer_count, outside.outside_count), (False, 30, 4, 26))
        self.assertEqual(outside.outside_sample, [f"SYNTH-CASE-{i:04d}" for i in range(5, 15)])

    def test_rank_top_n_keeps_ties_at_the_cutoff_and_shares_their_rank(self) -> None:
        rows = [de_row("C", 1.0, 0.01, 0.03), de_row("A", -1.0, 0.01, 0.02), de_row("B", 2.0, 0.01, 0.03), de_row("D", 0.5, 0.01, 0.04)]
        ranked = rank_top_n(rows, key="gene_name", rank_by="adjusted_p_value", n=2, ascending=True, tie_break="gene_name")
        self.assertEqual([(r.rank, r.key, r.value) for r in ranked.rows], [(1, "A", 0.02), (2, "B", 0.03), (2, "C", 0.03)])
        self.assertEqual((ranked.ties_at_cutoff, ranked.count, ranked.total_rows, ranked.n_requested), (["C"], 3, 4, 2))
        self.assertEqual(ranked.rows[1].row, rows[2])
        no_tie = rank_top_n(rows, key="gene_name", rank_by="adjusted_p_value", n=3, ascending=True, tie_break="gene_name")
        self.assertEqual(([r.key for r in no_tie.rows], no_tie.ties_at_cutoff), (["A", "B", "C"], []))
        self.assertEqual(no_tie.version, "rank_top_n@1")

    def test_rank_top_n_by_absolute_fold_change_descending(self) -> None:
        rows = [de_row("A", -2.5, 0.1, 0.1), de_row("B", 2.0, 0.1, 0.1), de_row("C", 0.4, 0.1, 0.1)]
        ranked = rank_top_n(rows, key="gene_name", rank_by="abs(fold_change)", n=2, ascending=False, tie_break="gene_name")
        self.assertEqual([(r.key, r.value) for r in ranked.rows], [("A", 2.5), ("B", 2.0)])

    def test_rank_top_n_with_fewer_rows_and_bad_rows(self) -> None:
        ranked = rank_top_n([{"gene": "KRAS", "cases": 4}], key="gene", rank_by="cases", n=3, ascending=False, tie_break="gene")
        self.assertEqual((ranked.count, ranked.total_rows), (1, 1))
        self.assertEqual(rank_top_n([], key="gene", rank_by="cases", n=3, ascending=False, tie_break="gene").count, 0)
        with self.assertRaises(FunctionError):
            rank_top_n([{"gene": "KRAS", "cases": "4"}], key="gene", rank_by="cases", n=1, ascending=False, tie_break="gene")
        with self.assertRaises(FunctionError):
            rank_top_n([{"gene": 1, "cases": 4}], key="gene", rank_by="cases", n=1, ascending=False, tie_break="gene")

    def test_derive_km_median(self) -> None:
        curves = [
            KmCurve(label="High risk", points=[KmPoint(time=0, survival=1), KmPoint(time=730, survival=0.5), KmPoint(time=365, survival=0.7)]),
            KmCurve(label="Standard risk", points=[KmPoint(time=0, survival=1), KmPoint(time=2190, survival=0.6)]),
        ]
        result = derive_km_median(curves, unit="days")
        self.assertEqual([(g.label, g.median, g.reached) for g in result.groups], [("High risk", 730, True), ("Standard risk", None, False)])
        self.assertEqual((result.version, result.unit), ("derive_km_median@1", "days"))


class StepTests(unittest.TestCase):
    def test_set_operation_reads_case_ids_and_reports_an_empty_result(self) -> None:
        read = reader({"s3": {"caseIds": A}, "s2": {"caseIds": B}})
        result = run_function("set_operation", {"op": "difference", "a": "s3", "b": "s2"}, read)
        self.assertEqual((result.status, result.limitation, result.provenance), ("ok", None, []))
        self.assertEqual(obj(result.observed)["caseIds"], ["SYNTH-CASE-0001", "SYNTH-CASE-0003"])
        empty = run_function("set_operation", {"op": "difference", "a": "s2", "b": "s2"}, read)
        self.assertEqual((empty.status, empty.limitation), ("empty", "EMPTY_COHORT"))

    def test_check_subset_reports_cases_outside(self) -> None:
        read = reader({"s2": {"caseIds": B}, "s3": {"caseIds": A}})
        result = run_function("check_subset", {"inner": "s2", "outer": "s3"}, read)
        self.assertEqual((result.status, result.limitation), ("ok", "NOT_SUBSET"))
        self.assertEqual((obj(result.observed)["outsideCount"], obj(result.observed)["outsideSample"]), (1, ["SYNTH-CASE-0004"]))

    def test_rank_top_n_applies_de_thresholds_before_ranking(self) -> None:
        genes = [de_row("A", -1.8, 1e-10, 1e-8), de_row("B", 0.2, 1e-12, 1e-9), de_row("C", 0.9, 0.04, 0.2), de_row("D", 1.1, 0.001, 0.01)]
        read = reader({"s9": {"genes": genes}})
        adjusted: dict[str, Any] = {"pValueCutoff": 0.05, "pValueType": "adjusted", "foldChangeCutoff": 0.3}
        args: dict[str, Any] = {"rows": "s9", "key": "gene_name", "rankBy": "adjusted_p_value", "n": 10, "ascending": True, "tieBreak": "gene_name", "thresholds": adjusted}
        result = run_function("rank_top_n", args, read)
        self.assertEqual([r["key"] for r in obj(result.observed)["rows"]], ["A", "D"])
        self.assertEqual((result.limitation, obj(result.observed)["totalRows"]), ("FEWER_THAN_N", 2))
        original = run_function("rank_top_n", {**args, "thresholds": {**adjusted, "pValueType": "original"}, "n": 3}, read)
        self.assertEqual([r["key"] for r in obj(original.observed)["rows"]], ["A", "D", "C"])
        none = run_function("rank_top_n", {**args, "thresholds": {**adjusted, "pValueCutoff": 1e-20}}, read)
        self.assertEqual((none.status, none.limitation), ("empty", "FEWER_THAN_N"))

    def test_rank_top_n_sees_a_tie_through_next_gene(self) -> None:
        read = reader({"s3": {"genes": [{"gene": "KRAS", "cases": 4}, {"gene": "TP53", "cases": 3}], "nextGene": {"gene": "NRAS", "cases": 3}}})
        args: dict[str, Any] = {"rows": "s3", "key": "gene", "rankBy": "cases", "n": 2, "ascending": False, "tieBreak": "gene"}
        result = run_function("rank_top_n", args, read)
        self.assertEqual([(r["rank"], r["key"]) for r in obj(result.observed)["rows"]], [(1, "KRAS"), (2, "NRAS"), (2, "TP53")])
        self.assertEqual((result.limitation, obj(result.observed)["tiesAtCutoff"]), ("TIES_AT_CUTOFF", ["TP53"]))
        plain = run_function("rank_top_n", args, reader({"s3": {"genes": [{"gene": "KRAS", "cases": 4}, {"gene": "TP53", "cases": 3}]}}))
        self.assertEqual((plain.status, plain.limitation), ("ok", None))

    def test_derive_km_median_reads_the_survival_step(self) -> None:
        groups: list[dict[str, Any]] = [
            {"label": "High risk", "cases": 3, "events": 2, "curve": [{"time": 0, "survival": 1.0}, {"time": 100, "survival": 0.4}]},
            {"label": "Standard risk", "cases": 3, "events": 0, "curve": [{"time": 0, "survival": 1.0}, {"time": 100, "survival": 1.0}]},
        ]
        result = run_function("derive_km_median", {"curves": "s4"}, reader({"s4": {"groups": groups}}))
        self.assertEqual(obj(result.observed)["groups"], [{"label": "High risk", "median": 100.0, "reached": True}, {"label": "Standard risk", "median": None, "reached": False}])

    def test_a_failed_function_is_an_error_recorded_as_internal_error(self) -> None:
        result = run_function("set_operation", {"op": "difference", "a": "s9", "b": "s2"}, reader({"s2": {"caseIds": B}}))
        # The contract's ToolErrorCode has INTERNAL_ERROR for an executor function that raised (v1.2).
        self.assertEqual((result.status, result.limitation, result.error and result.error.code), ("error", "TOOL_FAILED", "INTERNAL_ERROR"))
        bad = run_function("check_subset", {"inner": "s2"}, reader({}))
        self.assertEqual(bad.status, "error")
        clock = ManualClock()
        outcome = call_function("set_operation", {"op": "difference", "a": "s9", "b": "s2"}, reader({"s2": {"caseIds": B}}),
                                run_id=RUN_ID, plan_version=1, step_id="s5", call_n=1, now=clock.now, monotonic=clock.monotonic)
        record = call_records(outcome, plan_version=1, step_id="s5")[0]
        self.assertTrue((record.error or "").startswith("INTERNAL_ERROR"))

    def test_call_function_records_one_in_process_attempt(self) -> None:
        clock = ManualClock()
        request: dict[str, Any] = {"op": "difference", "a": "s3", "b": "s2"}
        outcome = call_function("set_operation", request, reader({"s3": {"caseIds": A}, "s2": {"caseIds": B}}),
                                run_id=RUN_ID, plan_version=2, step_id="s5", call_n=3, now=clock.now, monotonic=clock.monotonic)
        self.assertEqual((outcome.transport, outcome.tool_version, outcome.request), ("in_process", "set_operation@1", request))
        self.assertEqual(outcome.request_sha256, sha256_hex(canonical_json(request, portable=False)))
        self.assertEqual([a.call_id for a in outcome.attempts], [f"{RUN_ID}.v2.s5.c3"])
        record = call_records(outcome, plan_version=2, step_id="s5")[0]
        self.assertEqual((record.http_status, record.provenance, record.attempt, record.error), (None, [], 1, None))
        self.assertEqual(record.response_sha256, sha256_hex(canonical_json(outcome.result.observed, portable=False)))

    def test_function_tool_is_an_executor_function(self) -> None:
        tool = FunctionTool("check_subset", reader({"s2": {"caseIds": B}, "s3": {"caseIds": B}}))
        self.assertEqual((tool.version, tool.pure, [g.on_fail for g in tool.guards]), ("check_subset@1", True, ["NOT_SUBSET"]))
        self.assertIn("inner", obj(tool.input_schema["properties"]))
        ctx = ToolContext(run_id=RUN_ID, step_id="s4", call_id=f"{RUN_ID}.v1.s4.c1", attempt=1, timeout_s=1)
        result = asyncio.run(tool.invoke({"inner": "s2", "outer": "s3"}, ctx))
        self.assertEqual((result.status, result.limitation), ("ok", None))


if __name__ == "__main__":
    unittest.main()
