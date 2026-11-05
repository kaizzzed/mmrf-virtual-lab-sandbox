"""The pre- and post-guard checks, offline: the ProteinPaint bridge rule (open question 11), group
sizes and sample limits. test_tools_oq11.py checks the same rule against the live bridge."""

import os
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orion_agent.tools.guards import (
    bridge_safe_with_fields,
    case_lists_nonempty,
    de_samples_within_limit,
    eligible_enough,
    executor_pre_guard,
    filter0_bridge_safe,
    survival_groups_eligible,
)
from orion_agent.tools.schemas import case_id_filter
from orion_agent.vocab import Vocabulary

FIELDS = Path(os.environ.get("ORION_FIELDS_PATH", "/config/cohort-fields.json"))


@dataclass(frozen=True)
class Spec:
    type: str
    es_only: bool
    label: str
    values: tuple[str | int | float, ...] | None


class Lookup:
    FIELDS = {
        "demographic.gender": Spec("enum", False, "Gender", ("female", "male")),
        "demographic.age_at_index": Spec("age", False, "Age at index", None),
        "outcomes.censor_os": Spec("enum", False, "Censor OS", (0, 1)),
        "follow_ups.ecog_performance_status": Spec("enum", False, "ECOG", ("0", "1")),
        "available_variation_data": Spec("enum", True, "Available variation data", ("ssm", "cnv")),
    }

    def field(self, field_id: str) -> Spec | None:
        return self.FIELDS.get(field_id)


def clause(op: str, field: str, value: Any) -> dict[str, Any]:
    return {"and": [{op: {field: value}}]}


class BridgeRuleTests(unittest.TestCase):
    def test_what_the_bridge_reads(self) -> None:
        safe: list[Any] = [
            None, {"and": []}, clause("in", "demographic.gender", ["female"]), clause("=", "demographic.gender", "female"),
            clause("!=", "demographic.gender", "female"), {"in": {"demographic.gender": ["female"]}},
            {"and": [{">=": {"demographic.age_at_index": 18250}}, {"<": {"demographic.age_at_index": 25550}}]},
            {"or": [{"and": [{"in": {"demographic.gender": ["male"]}}]}, {"in": {"demographic.race": ["asian"]}}]},
            case_id_filter([f"SYNTH-CASE-{i:04d}" for i in range(1, 10_001)]), clause("in", "cases.case_id", ["SYNTH-CASE-0001"]),
            clause("in", "case_id", [{"cases.case_id": "SYNTH-CASE-0001"}]), clause("in", "case_id", []),
        ]
        for filter0 in safe:
            self.assertIsNone(filter0_bridge_safe(filter0), filter0)

    def test_what_the_bridge_refuses_or_reads_differently(self) -> None:
        refused: list[tuple[Any, str | None]] = [
            (clause("in", "available_variation_data", ["ssm"]), "available_variation_data"),
            (clause("in", "gene.symbol", ["KRAS"]), "gene.symbol"),
            (clause("in", "cases.summary.experimental_strategies.experimental_strategy", ["RNA-Seq"]), "cases.summary.experimental_strategies.experimental_strategy"),
            ({"and": [{"not": {"in": {"demographic.gender": ["female"]}}}]}, None),
            (clause("exclude", "demographic.gender", ["female"]), None),
            (clause("excludes", "demographic.gender", ["female"]), None),
            (clause("in", "demographic.gender", "female"), "demographic.gender"),
            (clause("in", "demographic.gender", [["nested"]]), "demographic.gender"),
            (clause("in", "case_id", ["not a case id!"]), "case_id"),
            (clause(">", "case_id", "SYNTH-CASE-0001"), "case_id"),
            ({"and": [{"in": {"a": ["x"]}, "=": {"b": "y"}}]}, None),
            ({"and": {"in": {"a": ["x"]}}}, None),
            ([], None),
        ]
        for filter0, field in refused:
            violation = filter0_bridge_safe(filter0)
            assert violation is not None, filter0
            self.assertEqual((violation.code, violation.rule, violation.field), ("UNSUPPORTED_FIELD", "filter0_bridge_safe", field))

    def test_the_executor_adds_the_vocabulary(self) -> None:
        lookup = Lookup()
        self.assertIsNone(bridge_safe_with_fields(clause("in", "outcomes.censor_os", [1]), lookup))
        self.assertIsNone(bridge_safe_with_fields(clause("in", "follow_ups.ecog_performance_status", ["0"]), lookup))
        self.assertIsNone(bridge_safe_with_fields(clause(">=", "demographic.age_at_index", 60), lookup))
        self.assertIsNone(bridge_safe_with_fields(case_id_filter(["SYNTH-CASE-0002"]), lookup))
        for filter0, label in (
            (clause("in", "outcomes.censor_os", ["1"]), "Censor OS"),  # the silent zero
            (clause("in", "follow_ups.ecog_performance_status", [0]), "ECOG"),
            (clause(">=", "demographic.age_at_index", "60"), "Age at index"),
            (clause("in", "nope.field", ["x"]), "nope.field"),
        ):
            violation = bridge_safe_with_fields(filter0, lookup)
            assert violation is not None, filter0
            self.assertEqual((violation.code, violation.field), ("UNSUPPORTED_FIELD", label))

    @unittest.skipUnless(FIELDS.is_file(), f"{FIELDS} is not mounted")
    def test_the_real_vocabulary_satisfies_the_lookup(self) -> None:
        vocab = Vocabulary.load(FIELDS)
        self.assertIsNone(bridge_safe_with_fields(clause("in", "cgs_risk_key_criteria.cgs_risk_category", ["High risk"]), vocab))
        violation = bridge_safe_with_fields(clause("in", "available_variation_data", ["ssm"]), vocab)
        self.assertEqual(violation and violation.code, "UNSUPPORTED_FIELD")
        self.assertEqual(bridge_safe_with_fields(clause("in", "outcomes.censor_os", ["1"]), vocab) is None, False)

    def test_executor_pre_guard_per_tool(self) -> None:
        self.assertIsNone(executor_pre_guard("top_mutated_genes", {"filter0": None, "geneCount": 3}))
        refused = executor_pre_guard("top_mutated_genes", {"filter0": clause("in", "outcomes.censor_os", ["1"]), "geneCount": 3}, fields=Lookup())
        self.assertEqual(refused and refused.code, "UNSUPPORTED_FIELD")
        small = executor_pre_guard("survival_compare", {}, group_counts={"High risk": 339, "Stage IV": 7})
        assert small is not None
        self.assertEqual((small.code, small.rule, small.group), ("INELIGIBLE_GROUP", "group_cases>=10", "Stage IV"))
        self.assertIsNone(executor_pre_guard("survival_compare", {}, group_counts={"A": 10, "B": 11}))
        self.assertIsNone(executor_pre_guard("de_run", {"groups": []}))


class SizeGuardTests(unittest.TestCase):
    def test_group_and_sample_limits(self) -> None:
        self.assertEqual(survival_groups_eligible({"A": 9}).code, "INELIGIBLE_GROUP")  # type: ignore[union-attr]
        self.assertIsNone(de_samples_within_limit([2000, 2000]))
        self.assertEqual(de_samples_within_limit([2000, 2001]).code, "REQUEST_TOO_LARGE")  # type: ignore[union-attr]
        self.assertIsNone(eligible_enough({"control": 10, "case": 10}))
        short = eligible_enough({"control": 10, "case": 9})
        self.assertEqual((short and short.code, short and short.group), ("INELIGIBLE_GROUP", "case"))
        empty = case_lists_nonempty([("control", ["SYNTH-CASE-0001"]), ("case", [])])
        self.assertEqual((empty and empty.code, empty and empty.group), ("EMPTY_COHORT", "case"))


if __name__ == "__main__":
    unittest.main()
