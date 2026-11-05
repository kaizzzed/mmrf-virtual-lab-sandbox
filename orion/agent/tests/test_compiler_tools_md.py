"""The compiled step tables against orion/contracts/TOOLS.md section 5, read from the file itself.

Each table row's step id, row, row label, tool and args are compared with the compiled plan. Args
cells are the catalog's JSON with a few shorthands, expanded here exactly as TOOLS.md defines them:
`StepRef(sN)` / `StepRef(sN, /pointer)` (a StepRef, default pointer /observed/caseIds),
`DeRunStepArgs {...}` (the args model's JSON), `<same>` (the thresholds of the row above) and the
table's own placeholders (`n`, `<cohort>`, `"<name> (ORION)"`).
"""

import json
import re
import unittest
from dataclasses import dataclass

from pydantic import JsonValue
from support import CONTRACTS, context, vocabulary

from orion_agent import ir
from orion_agent.compiler import CompiledPlan, Compiler, CompileStop
from orion_agent.models import CohortContext, DeInputPinned, derive_request_pinned
from orion_agent.tools import schemas

TOOLS_MD = CONTRACTS / "TOOLS.md"
RACE = "demographic.race"
CGS = "cgs_risk_key_criteria.cgs_risk_category"
RACE_CLAUSE: JsonValue = {"field": RACE, "op": "in", "values": ["black or african american"]}
RACE_ROOT: dict[str, dict[str, JsonValue]] = {RACE: {"operator": "in", "field": RACE, "operands": ["Black or African American"]}}
STAGE_ROOT: dict[str, dict[str, JsonValue]] = {"diagnoses.iss_stage": {"operator": "in", "field": "diagnoses.iss_stage", "operands": ["Stage III"]}}
J3: dict[str, JsonValue] = {
    "kind": "plan",
    "cohorts": [
        {"ref": "tested", "def": {"kind": "mutation_tested", "within": "base"}},
        {"ref": "kras_mut", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "tested"}},
        {"ref": "kras_wt", "def": {"kind": "difference", "a": "tested", "b": "kras_mut"}},
    ],
    "analyses": [{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut", "top_n": 10}],
}


@dataclass(frozen=True)
class TableRow:
    step: str
    row: int
    label: str | None
    tool: str
    args: str


def table(heading: str) -> list[TableRow]:
    """The step table under a TOOLS.md heading: rows whose first cell is a step id."""
    text = TOOLS_MD.read_text(encoding="utf-8")
    section = text[text.index(heading):]
    section = section[: section.index("\n### ", 1)] if "\n### " in section[1:] else section
    rows: list[TableRow] = []
    for line in section.splitlines():
        if not re.match(r"^\| s\d+ \|", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        row = re.match(r'^(\d+)(?: \(folded\))?(?: "([^"]+)")?', cells[1])
        assert row is not None, line
        tool = cells[2].strip("`")
        rows.append(TableRow(cells[0], int(row.group(1)), row.group(2), tool, cells[3]))
    return rows


def expand(cell: str, values: dict[str, str], previous: JsonValue = None) -> JsonValue:
    """The cell's JSON: its first backticked object (a cell may add a note or name the args model)."""
    text = next(part for part in re.findall(r"`([^`]+)`", cell) if part.startswith("{"))
    text = re.sub(r"StepRef\((s\d+)(?:, (/[^)]+))?\)", lambda m: json.dumps({"ref": m.group(1), "pointer": m.group(2) or "/observed/caseIds"}), text)
    if previous is not None and isinstance(previous, dict):
        text = text.replace("<same>", json.dumps(previous.get("thresholds")))
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    parsed: JsonValue = json.loads(text)
    return parsed


class ToolsMdCase(unittest.TestCase):
    def setUp(self) -> None:
        if not TOOLS_MD.exists():
            self.skipTest(f"{TOOLS_MD} is not mounted")
        self.compiler = Compiler(vocabulary(), "active_cohort")

    def compile(self, plan: dict[str, JsonValue], ctx: CohortContext | None = None) -> CompiledPlan:
        result: CompiledPlan | CompileStop = self.compiler.compile(ir.Plan.model_validate(plan), ctx or context())
        assert isinstance(result, CompiledPlan), result
        return result

    def assert_table(self, heading: str, plan: CompiledPlan, values: dict[str, str], *, parse_args: set[str]) -> None:
        rows = table(heading)
        self.assertEqual([r.step for r in rows], [s.id for s in plan.steps], heading)
        previous: JsonValue = None
        for spec, step in zip(rows, plan.steps):
            with self.subTest(heading=heading, step=step.id):
                self.assertEqual((step.row, step.tool), (spec.row, spec.tool))
                if spec.label is not None:
                    label = spec.label
                    for placeholder, value in values.items():
                        label = label.replace(placeholder, value.strip('"'))
                    self.assertEqual(plan.rows[step.row - 1].label, label)
                if step.id in parse_args:
                    self.assertEqual(step.args, expand(spec.args, values, previous))
                self.assert_args_validate(step.tool, step.args)
            previous = step.args

    def assert_args_validate(self, tool: str, args: dict[str, JsonValue]) -> None:
        """Executor inputs and the read-back steps' args models validate the compiled args as they are."""
        spec = schemas.tool_spec(tool)
        if spec.step_args_model is not None:
            spec.step_args_model.model_validate(args)
        elif spec.kind == "exec":
            spec.input_model.model_validate(args)


class J3TableTests(ToolsMdCase):
    def test_section_5_3_unfiltered_is_the_compiled_j3(self) -> None:
        plan = self.compile(J3)
        self.assert_table("### 5.3 J3", plan, {}, parse_args={f"s{i}" for i in range(2, 11)})

    def test_filtered_scope_adds_the_scope_step_and_shifts_every_id(self) -> None:
        plan = self.compile(J3, context(RACE_ROOT, name="Black patients", case_count=334))
        self.assertEqual((len(plan.steps), len(plan.rows)), (11, 7))
        self.assertEqual((plan.rows[0].label, plan.steps[1].tool), ("Find cases in Black patients", "cases_query"))
        scope = {"in": {"case_id": {"ref": "s2", "pointer": "/observed/caseIds"}}}
        self.assertEqual(plan.steps[2].args, {"gene": "KRAS", "cohortFilter": {"and": [scope]}})
        self.assertEqual(plan.steps[3].args["filter"], {"and": [scope, {"in": {"available_variation_data": ["ssm"]}}]})
        self.assertEqual([s.tool for s in plan.steps[:1] + plan.steps[2:]], [r.tool for r in table("### 5.3 J3")])


class J2TableTests(ToolsMdCase):
    J2: dict[str, JsonValue] = {
        "kind": "plan",
        "cohorts": [{"ref": "black", "def": {"kind": "filter", "clauses": [RACE_CLAUSE]}}],
        "analyses": [{"kind": "top_mutated_genes", "cohort": "black", "n": 3}],
    }

    def test_section_5_2_unfiltered_is_the_compiled_j2(self) -> None:
        plan = self.compile(self.J2)
        values = {"geneCount\": n": "geneCount\": 3", "\"n\": n": "\"n\": 3", "<cohort>": '"black"', "<name>": "Black or African American",
                  "{cohort name}": "Black or African American", "{n}": "3"}
        self.assert_table("### 5.2 J2", plan, values, parse_args={"s3", "s4", "s5"})
        self.assertEqual(plan.steps[1].args, {"filter": {"and": [{"in": {RACE: ["black or african american"]}}]}, "pageSize": 1000, "maxIds": 10000})

    def test_filtered_scope_adds_the_scope_step_and_its_case_ids_reach_the_cohort(self) -> None:
        plan = self.compile(self.J2, context(STAGE_ROOT, name="Stage III", case_count=300))
        self.assertEqual([s.tool for s in plan.steps], ["fixture_fingerprint", "cases_query", "cases_query", "top_mutated_genes", "rank_top_n", "save_cohorts"])
        self.assertEqual(plan.rows[0].label, "Find cases in Stage III")
        scope = {"in": {"case_id": {"ref": "s2", "pointer": "/observed/caseIds"}}}
        self.assertEqual(plan.steps[2].args["filter"], {"and": [scope, {"in": {RACE: ["black or african american"]}}]})
        self.assertEqual(plan.steps[3].args["filter0"], {"and": [{"in": {"case_id": {"ref": "s3", "pointer": "/observed/caseIds"}}}]})
        self.assertEqual(plan.steps[5].args, {"cohorts": [{"ref": "black", "name": "Black or African American (ORION)"}]})

    def test_top_mutated_genes_in_the_scope_has_no_cohort_and_no_save(self) -> None:
        in_scope: dict[str, JsonValue] = {"kind": "plan", "analyses": [{"kind": "top_mutated_genes", "cohort": "base", "n": 3}]}
        plan = self.compile(in_scope)
        self.assertEqual([(s.id, s.row, s.tool) for s in plan.steps], [("s1", 1, "fixture_fingerprint"), ("s2", 1, "top_mutated_genes"), ("s3", 1, "rank_top_n")])
        self.assertEqual(plan.rows[0].label, "Rank mutated genes: top 3")
        self.assertEqual(plan.steps[1].args, {"filter0": None, "geneCount": 3})
        self.assertEqual(plan.steps[2].args["rows"], "s2")
        self.assertFalse(plan.writes)
        filtered = self.compile(in_scope, context(STAGE_ROOT, name="Stage III", case_count=300))
        # The oncomatrix fixture's step layout: s1, s2 the scope's cases_query, s3 ranked from s2, s4.
        self.assertEqual([(s.id, s.row, s.tool) for s in filtered.steps],
                         [("s1", 1, "fixture_fingerprint"), ("s2", 1, "cases_query"), ("s3", 2, "top_mutated_genes"), ("s4", 2, "rank_top_n")])
        self.assertEqual(filtered.steps[2].args["filter0"], {"and": [{"in": {"case_id": {"ref": "s2", "pointer": "/observed/caseIds"}}}]})


class J1TableTests(ToolsMdCase):
    J1: dict[str, JsonValue] = {"kind": "plan", "analyses": [{"kind": "survival_compare", "split_term": CGS}]}

    def test_section_5_1_rows_tools_and_args(self) -> None:
        plan = self.compile(self.J1)
        self.assertEqual([(s.id, s.row, s.tool) for s in plan.steps],
                         [("s1", 1, "fixture_fingerprint"), ("s2", 1, "cohort_count"), ("s3", 1, "cohort_count"),
                          ("s4", 2, "survival_compare"), ("s5", 2, "derive_km_median")])
        self.assertEqual([r.label for r in plan.rows], ["Count both CGS risk groups", "Compare survival: Kaplan–Meier, split by CGS risk category"])
        groups = [{"label": v, "filter": {"and": [{"in": {CGS: [v]}}]}} for v in ("High risk", "Standard risk")]
        self.assertEqual(plan.steps[3].args, {"scope": None, "term": "overall_survival", "split": {"field": CGS, "overlay": "cgs_risk_category"}, "groups": groups})
        self.assertEqual((plan.steps[3].depends_on, plan.steps[4].args), (["s2", "s3"], {"curves": "s4"}))
        schemas.SurvivalCompareInput.model_validate(plan.steps[3].args)

    def test_filtered_scope_reads_the_scope_once_and_every_filter_names_its_case_ids(self) -> None:
        plan = self.compile(self.J1, context(RACE_ROOT, name="Black patients", case_count=334))
        self.assertEqual([r.label for r in plan.rows][0], "Find cases in Black patients")
        self.assertEqual(plan.steps[1].args, {"filter": {"and": [{"in": {RACE: ["black or african american"]}}]}, "pageSize": 1000, "maxIds": 10000})
        scope = {"in": {"case_id": {"ref": "s2", "pointer": "/observed/caseIds"}}}
        self.assertEqual(plan.steps[2].args, {"filter": {"and": [scope, {"in": {CGS: ["High risk"]}}]}})
        survival = plan.steps[4]
        self.assertEqual(survival.args["scope"], {"and": [scope]})
        self.assertEqual(survival.depends_on, ["s2", "s3", "s4"])
        self.assertEqual([s.row for s in plan.steps], [1, 1, 2, 2, 3, 3])

    def test_survival_between_cohorts_has_no_split_and_offers_cohort_comparison(self) -> None:
        between: dict[str, JsonValue] = {
            "kind": "plan",
            "cohorts": [
                {"ref": "high", "def": {"kind": "filter", "clauses": [{"field": CGS, "op": "in", "values": ["High risk"]}]}},
                {"ref": "kras_mut", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "base"}},
            ],
            "analyses": [{"kind": "survival_compare", "groups": ["high", "kras_mut"]}],
        }
        plan = self.compile(between)
        survival = next(s for s in plan.steps if s.tool == "survival_compare")
        self.assertIsNone(survival.args["split"])
        groups = survival.args["groups"]
        assert isinstance(groups, list)
        self.assertEqual(groups[0], {"label": "CGS high-risk", "filter": {"and": [{"in": {CGS: ["High risk"]}}]}})
        self.assertEqual(groups[1], {"label": "KRAS mutated", "filter": {"and": [{"in": {"case_id": {"ref": "s3", "pointer": "/observed/caseIds"}}}]}})
        # Each group is sized by the step that defines it: a cohort_count for the filter cohort.
        self.assertEqual(survival.depends_on, ["s2", "s3"])
        self.assertEqual([s.tool for s in plan.steps[:3]], ["fixture_fingerprint", "cohort_count", "gene_cases"])
        self.assertEqual([a.app_id for a in plan.open_apps], ["CohortComparison"])
        schemas.SurvivalCompareInput.model_validate({**survival.args, "groups": [{"label": "a", "filter": {"and": []}}, {"label": "b", "filter": {"and": []}}]})


class VizInputTests(ToolsMdCase):
    """TOOLS.md 5.5 and 5.6: the cards pin what the compiled steps send, once their StepRefs resolve."""

    def read(self, calls: dict[str, JsonValue]) -> schemas.StepValueReader:
        def read(step_id: str, pointer: str) -> JsonValue:
            return schemas.resolve_pointer(calls[step_id], pointer)

        return read

    def test_the_oncomatrix_card_pins_the_cohorts_case_ids_and_the_returned_genes(self) -> None:
        plan = self.compile(J2TableTests.J2)
        ids: list[JsonValue] = ["SYNTH-CASE-0002", "SYNTH-CASE-0001"]
        request = schemas.resolve_step_refs(plan.step("s3").args, self.read({"s2": {"observed": {"caseIds": ids}}}))
        assert isinstance(request, dict)
        schemas.TopMutatedGenesInput.model_validate(request)
        observed: JsonValue = {"genes": [{"gene": "KRAS", "cases": 3}, {"gene": "NRAS", "cases": 2}]}
        pinned = derive_request_pinned("oncomatrix", request, observed)
        self.assertEqual(pinned.model_dump(mode="json"), {"kind": "oncomatrix", "filter0": {"and": [{"in": {"case_id": ids}}]}, "genes": ["KRAS", "NRAS"]})

    def test_the_de_input_fallback_runs_the_same_steps_and_reads_the_saved_cohorts(self) -> None:
        plan = self.compile(J3)
        args = schemas.DeRunStepArgs.model_validate(plan.step("s9").args)
        samples: JsonValue = {"groups": [{"key": "control", "count": 2, "sampleIds": [1, 3]}, {"key": "case", "count": 1, "sampleIds": [2]}]}
        saved = {
            "kras_wt": schemas.SavedGroup("KRAS not detected (ORION)", "c-wt", schemas.case_id_filter(["SYNTH-CASE-0001", "SYNTH-CASE-0003"])),
            "kras_mut": schemas.SavedGroup("KRAS mutated (ORION) 2", "c-mut", schemas.case_id_filter(["SYNTH-CASE-0002"])),
        }
        request = schemas.de_run_input(args, self.read({"s6": {"observed": samples}}), saved).model_dump(mode="json")
        pinned = derive_request_pinned("deInput", request)
        assert isinstance(pinned, DeInputPinned)
        self.assertEqual([(g.name, g.cohort_id) for g in pinned.groups], [("KRAS not detected (ORION)", "c-wt"), ("KRAS mutated (ORION) 2", "c-mut")])
        direct = derive_request_pinned("deDirect", schemas.de_run_input(args, self.read({"s6": {"observed": samples}}), {}).model_dump(mode="json"))
        self.assertEqual(direct.model_dump(mode="json")["volcano"], {"foldChangeCutoff": 0.3, "pValue": 1.3, "pValueType": "adjusted"})


class CohortSizeTableTests(ToolsMdCase):
    def size(self, cohort: str, cohorts: list[JsonValue] | None = None, ctx: CohortContext | None = None) -> CompiledPlan:
        return self.compile({"kind": "plan", "cohorts": cohorts or [], "analyses": [{"kind": "cohort_size", "cohort": cohort}]}, ctx)

    def test_section_5_4_every_cohort_form(self) -> None:
        unfiltered = self.size("base")
        self.assertEqual([(s.id, s.tool, s.args) for s in unfiltered.steps][1:], [("s2", "cohort_count", {"filter": {"and": []}})])
        self.assertEqual(unfiltered.rows[0].label, "Count All cases")
        filtered = self.size("base", ctx=context(RACE_ROOT, name="Black patients", case_count=334))
        self.assertEqual([s.tool for s in filtered.steps], ["fixture_fingerprint", "cases_query"])
        self.assertEqual(filtered.rows[0].label, "Count Black patients")
        planned = self.size("black", [{"ref": "black", "def": {"kind": "filter", "clauses": [RACE_CLAUSE]}}], context(RACE_ROOT, name="Black patients"))
        scope = {"in": {"case_id": {"ref": "s2", "pointer": "/observed/caseIds"}}}
        self.assertEqual(planned.steps[2].args, {"filter": {"and": [scope, {"in": {RACE: ["black or african american"]}}]}})
        computed = self.size("kras_mut", [{"ref": "kras_mut", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "base"}}])
        self.assertEqual([(s.tool, s.args) for s in computed.steps][1:],
                         [("gene_cases", {"gene": "KRAS", "cohortFilter": {"and": []}}), ("set_operation", {"op": "size", "a": "s2"})])
        for plan in (unfiltered, filtered, planned, computed):
            self.assertFalse(plan.writes)
            for step in plan.steps:
                self.assert_args_validate(step.tool, step.args)


if __name__ == "__main__":
    unittest.main()
