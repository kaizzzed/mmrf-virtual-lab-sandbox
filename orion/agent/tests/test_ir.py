"""The planner IR (RFA rev 25, A2) and the app catalog."""

import json
import os
import unittest
from pathlib import Path

from pydantic import JsonValue, ValidationError

from orion_agent import ir
from orion_agent.catalog import ANALYSIS_APPS, APP_IDS, APPS, NAMED_TOOL_APPS, NAMED_TOOLS
from orion_agent.stubs import j1_plan

# Verbatim from the RFA, A2 "Planning: from question to signed plan" (the J3 tool-named phrasing).
J3_IR = """
{"kind": "plan",
 "cohorts": [
   {"ref": "tested", "def": {"kind": "mutation_tested", "within": "base"}},
   {"ref": "kras_mut", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "tested"}},
   {"ref": "kras_wt", "def": {"kind": "difference", "a": "tested", "b": "kras_mut"}}],
 "analyses": [{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras_mut",
               "top_n": 10}],
 "named_tools": [{"tool": "gen3_analysis_api"}, {"tool": "differential_expression"}]}
"""
J1_IR = {
    "kind": "plan",
    "cohorts": [],
    "analyses": [
        {"kind": "survival_compare", "split_term": "cgs_risk_key_criteria.cgs_risk_category", "split_values": ["High risk", "Standard risk"]}
    ],
    "named_tools": [{"tool": "correlation_plot"}],
}


def _plan(**overrides: JsonValue) -> str:
    return json.dumps({**J1_IR, **overrides})


class IRTests(unittest.TestCase):
    def test_doc_j3_ir_validates(self) -> None:
        output = ir.parse_planner_output(J3_IR)
        assert isinstance(output, ir.Plan)
        self.assertIsNone(output.base_cohort)
        self.assertEqual([c.definition.kind for c in output.cohorts], ["mutation_tested", "gene_mutated", "difference"])
        self.assertEqual(json.loads(output.model_dump_json(exclude_defaults=True)), json.loads(J3_IR))

    def test_j1_ir_validates_with_and_without_named_tools(self) -> None:
        output = ir.parse_planner_output(json.dumps(J1_IR))
        assert isinstance(output, ir.Plan)
        self.assertEqual(output.named_tools[0].tool, "correlation_plot")
        stub = j1_plan()
        self.assertIsNone(stub.base_cohort)
        self.assertEqual(ir.parse_planner_output(stub.model_dump_json()), stub)

    def test_base_cohort_sources(self) -> None:
        for source in ("all_cases", "active_cohort", "saved_cohort", "thread_cohort"):
            output = ir.parse_planner_output(_plan(base_cohort={"source": source}))
            assert isinstance(output, ir.Plan) and output.base_cohort is not None
            self.assertEqual(output.base_cohort.source, source)
        with self.assertRaises(ValidationError):
            ir.parse_planner_output(_plan(base_cohort={"source": "everyone"}))

    def test_every_cohort_kind_parses(self) -> None:
        cohorts: list[JsonValue] = [
            {"ref": "f", "def": {"kind": "filter", "clauses": [{"field": "demographic.race", "op": "in", "values": ["asian"]}]}},
            {"ref": "t", "def": {"kind": "mutation_tested", "within": "f"}},
            {"ref": "m", "def": {"kind": "gene_mutated", "gene": "TP53", "within": "t"}},
            {"ref": "d", "def": {"kind": "difference", "a": "t", "b": "m"}},
            {"ref": "i", "def": {"kind": "intersection", "a": "f", "b": "d"}},
            {"ref": "s", "def": {"kind": "saved", "cohort_name": "My cohort"}},
        ]
        output = ir.parse_planner_output(_plan(cohorts=cohorts, analyses=[{"kind": "cohort_size", "cohort": "i"}]))
        assert isinstance(output, ir.Plan)
        self.assertEqual(len(output.cohorts), 6)
        thread: list[JsonValue] = [{"ref": "x", "def": {"kind": "thread", "ledger_ref": "kras_wt"}}]
        self.assertIsInstance(ir.parse_planner_output(_plan(cohorts=thread)), ir.Plan)

    def test_schema_rejections(self) -> None:
        too_many: list[JsonValue] = [{"ref": f"c{i}", "def": {"kind": "mutation_tested", "within": "base"}} for i in range(7)]
        cases: dict[str, str] = {
            "extra field": _plan(note="hi"),
            "extra in def": _plan(cohorts=[{"ref": "a", "def": {"kind": "mutation_tested", "within": "base", "x": 1}}]),
            "gene outside enum": _plan(cohorts=[{"ref": "a", "def": {"kind": "gene_mutated", "gene": "IDH1", "within": "base"}}]),
            "no analyses": _plan(analyses=[]),
            "four analyses": _plan(analyses=[{"kind": "cohort_size", "cohort": "base"}] * 4),
            "seven cohorts": _plan(cohorts=too_many),
            "undefined ref": _plan(cohorts=[{"ref": "a", "def": {"kind": "difference", "a": "base", "b": "nope"}}]),
            "forward ref": _plan(
                cohorts=[
                    {"ref": "a", "def": {"kind": "difference", "a": "base", "b": "b"}},
                    {"ref": "b", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "base"}},
                ]
            ),
            "duplicate ref": _plan(cohorts=[{"ref": "a", "def": {"kind": "mutation_tested", "within": "base"}}] * 2),
            "analysis undefined ref": _plan(analyses=[{"kind": "cohort_size", "cohort": "ghost"}]),
            "unknown named tool": _plan(named_tools=[{"tool": "excel"}]),
            "follows not an id": _plan(follows="run-1"),
            "clarify one option": json.dumps({"kind": "clarify", "slot": "split_term", "option_ids": ["a"]}),
            "clarify six options": json.dumps({"kind": "clarify", "slot": "split_term", "option_ids": list("abcdef")}),
            "unsupported reason": json.dumps({"kind": "unsupported", "reason": "BORING", "nearest_app_ids": []}),
            "unsupported app": json.dumps({"kind": "unsupported", "reason": "OUT_OF_SCOPE", "nearest_app_ids": ["Excel"]}),
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                ir.parse_planner_output(payload)

    def test_edit_fields_are_optional_and_omitted_when_unset(self) -> None:
        output = ir.parse_planner_output(J3_IR)
        assert isinstance(output, ir.Plan)
        dumped = json.loads(output.model_dump_json(by_alias=True))
        self.assertNotIn("name", dumped["cohorts"][0])
        self.assertNotIn("thresholds", dumped["analyses"][0])
        renamed = output.model_copy(update={"cohorts": [output.cohorts[0].model_copy(update={"name": "Tested (ORION)"}), *output.cohorts[1:]]})
        stated = ir.Thresholds(p_value_cutoff=0.01, p_value_type="original", fold_change_cutoff=1.0)
        analysis = output.analyses[0].model_copy(update={"thresholds": stated})
        edited = renamed.model_copy(update={"analyses": [analysis]})
        again = ir.parse_planner_output(edited.model_dump_json(by_alias=True))
        self.assertEqual(again, edited)
        self.assertEqual(json.loads(edited.model_dump_json(by_alias=True))["analyses"][0]["thresholds"],
                         {"p_value_cutoff": 0.01, "p_value_type": "original", "fold_change_cutoff": 1.0})

    def test_edit_field_bounds(self) -> None:
        bad: dict[str, JsonValue] = {
            "empty name": {"cohorts": [{"ref": "a", "name": "", "def": {"kind": "mutation_tested", "within": "base"}}],
                           "analyses": [{"kind": "cohort_size", "cohort": "a"}]},
            "long name": {"cohorts": [{"ref": "a", "name": "x" * 101, "def": {"kind": "mutation_tested", "within": "base"}}],
                          "analyses": [{"kind": "cohort_size", "cohort": "a"}]},
            "zero cutoff": {"analyses": [{"kind": "differential_expression", "group1": "base", "group2": "base",
                                          "thresholds": {"p_value_cutoff": 0, "p_value_type": "adjusted", "fold_change_cutoff": 0.3}}]},
            "other p type": {"analyses": [{"kind": "differential_expression", "group1": "base", "group2": "base",
                                           "thresholds": {"p_value_cutoff": 0.05, "p_value_type": "raw", "fold_change_cutoff": 0.3}}]},
        }
        for name, fields in bad.items():
            assert isinstance(fields, dict)
            with self.subTest(name), self.assertRaises(ValidationError):
                ir.parse_planner_output(_plan(**fields))

    def test_clarify_and_unsupported(self) -> None:
        clarify = ir.parse_planner_output(
            json.dumps({"kind": "clarify", "slot": "split_term", "option_ids": ["cgs_risk_key_criteria.cgs_risk_category", "diagnoses.iss_stage"]})
        )
        self.assertIsInstance(clarify, ir.Clarify)
        unsupported = ir.parse_planner_output(json.dumps({"kind": "unsupported", "reason": "CLINICAL_ADVICE", "nearest_app_ids": ["SetOperations"]}))
        self.assertIsInstance(unsupported, ir.Unsupported)


class PlannerSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = ir.planner_output_json_schema()

    def _refs(self, node: JsonValue) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                found.add(ref.rsplit("/", 1)[-1])
            for value in node.values():
                found |= self._refs(value)
        elif isinstance(node, list):
            for value in node:
                found |= self._refs(value)
        return found

    def test_schema_has_no_recursive_ref_cycles(self) -> None:
        defs = self.schema["$defs"]
        assert isinstance(defs, dict)
        graph = {name: self._refs(body) for name, body in defs.items()}
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(name: str, path: tuple[str, ...]) -> None:
            if name in done:
                return
            self.assertNotIn(name, visiting, f"recursive $ref cycle: {' -> '.join(path + (name,))}")
            visiting.add(name)
            for child in graph[name]:
                visit(child, path + (name,))
            visiting.discard(name)
            done.add(name)

        for name in graph:
            visit(name, ())

    def test_schema_uses_def_alias_and_forbids_extra_properties(self) -> None:
        defs = self.schema["$defs"]
        assert isinstance(defs, dict)
        cohort = defs["CohortDef"]
        assert isinstance(cohort, dict) and isinstance(cohort["properties"], dict)
        self.assertIn("def", cohort["properties"])
        for name, body in defs.items():
            assert isinstance(body, dict)
            if body.get("type") == "object":
                self.assertIs(body.get("additionalProperties"), False, name)

    def test_schema_offers_the_three_output_kinds(self) -> None:
        refs = self._refs(self.schema["oneOf"])
        self.assertEqual(refs, {"Plan", "Clarify", "Unsupported"})


class CatalogTests(unittest.TestCase):
    def test_catalog_has_fifteen_apps_with_roles(self) -> None:
        self.assertEqual(len(APP_IDS), 15)
        self.assertEqual(set(APPS), APP_IDS)
        roles = {app.role for app in APPS.values()}
        self.assertEqual(roles, {"drives", "opens", "out_of_scope"})
        self.assertEqual({a for a, app in APPS.items() if app.role == "drives"}, {"CohortBuilder", "Correlation", "OncoMatrix", "DE"})
        self.assertEqual(APPS["Projects"].role, "out_of_scope")

    def test_named_tools_map_to_catalog_apps(self) -> None:
        self.assertEqual(set(NAMED_TOOL_APPS), NAMED_TOOLS)
        self.assertEqual({a for a in NAMED_TOOL_APPS.values() if a is not None}, APP_IDS)
        self.assertIsNone(NAMED_TOOL_APPS["gen3_analysis_api"])
        self.assertTrue(set(ANALYSIS_APPS.values()) <= APP_IDS)

    def test_catalog_matches_the_fork_analysis_tools(self) -> None:
        path = Path(os.environ.get("ORION_ANALYSIS_TOOLS_PATH", "/analysisTools.json"))
        if not path.exists():
            self.skipTest(f"{path} is not mounted")
        sections = json.loads(path.read_text())["sections"]
        fork_ids = {tool["appId"] for section in sections for tool in section["tools"]}
        self.assertEqual(fork_ids - {"Assistant"}, set(APP_IDS))


if __name__ == "__main__":
    unittest.main()
