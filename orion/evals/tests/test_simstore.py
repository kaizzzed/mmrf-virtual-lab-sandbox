"""The simulated browser's Create: reuse, the nonce-keyed ledger, unique names and the read-back."""

import copy
import unittest
from typing import Any

from helpers import fixture_turn

from orion_evals.canonical import def_sha256, filterset_def_sha256, filterset_from_definition
from orion_evals.simstore import SimulatedCohortStore


def j3_args() -> dict[str, Any]:
    args = fixture_turn("j3-save-pause.events.jsonl").tool_call_args()
    assert args is not None
    return args


class Create(unittest.TestCase):
    def test_creates_both_cohorts_and_reads_back_their_hashes(self) -> None:
        store = SimulatedCohortStore()
        args = j3_args()
        outcome = store.create(args, make_active_ref="kras_mut")
        self.assertEqual(outcome.created, ["kras_wt", "kras_mut"])
        self.assertEqual([s["ref"] for s in outcome.saved], ["kras_wt", "kras_mut"])
        for saved, pending in zip(outcome.saved, args["cohorts"], strict=True):
            self.assertEqual(saved["defSha256"], pending["defSha256"])
            self.assertFalse(saved["reused"])
            self.assertEqual(saved["name"], pending["name"])
        stored = store.by_id(outcome.saved[1]["cohortId"])
        assert stored is not None
        self.assertEqual(stored.filters["root"]["case_id"]["operator"], "in")
        self.assertEqual(store.active_id, stored.id)
        self.assertEqual([w["write"] for w in store.writes], ["createNewCohort", "createNewCohort", "set_active_cohort"])
        self.assertEqual(outcome.definition_mismatches, [])

    def test_reuses_an_identical_saved_cohort(self) -> None:
        store = SimulatedCohortStore()
        args = j3_args()
        existing = store.seed("My KRAS list", filterset_from_definition(args["cohorts"][1]["definition"]), 480)
        outcome = store.create(args)
        self.assertEqual(outcome.reused, ["kras_mut"])
        self.assertEqual(outcome.created, ["kras_wt"])
        self.assertEqual(outcome.saved[1], {"ref": "kras_mut", "cohortId": existing.id, "name": "My KRAS list", "defSha256": args["cohorts"][1]["defSha256"], "reused": True})
        self.assertEqual(store.duplicates(), [])

    def test_a_resumed_create_makes_nothing_twice(self) -> None:
        store = SimulatedCohortStore()
        args = j3_args()
        first = store.create(args)
        again = store.create(args)
        self.assertEqual(again.created, [])
        self.assertEqual(again.from_ledger, ["kras_wt", "kras_mut"])
        self.assertEqual(again.saved, first.saved)
        self.assertEqual(len(store.cohorts), 2)
        self.assertEqual(len(store.created_by(args["nonce"])), 2)

    def test_names_are_made_unique(self) -> None:
        store = SimulatedCohortStore()
        store.seed("KRAS mutated (ORION)", {"mode": "and", "root": {}}, 1000)
        store.seed("kras mutated (orion) (2)", {"mode": "and", "root": {}}, 1000)
        outcome = store.create(j3_args())
        self.assertEqual(outcome.saved[1]["name"], "KRAS mutated (ORION) (3)")
        self.assertEqual(store.unique_name("x" * 100)[-4:], "x" * 4)

    def test_a_definition_that_does_not_hash_to_its_defsha256_is_flagged(self) -> None:
        args = copy.deepcopy(j3_args())
        args["cohorts"][0]["definition"]["caseIds"] = args["cohorts"][0]["definition"]["caseIds"][1:]
        outcome = SimulatedCohortStore().create(args)
        self.assertEqual(outcome.definition_mismatches, ["kras_wt"])
        self.assertNotEqual(outcome.saved[0]["defSha256"], args["cohorts"][0]["defSha256"])

    def test_a_trimmed_definition_cannot_be_saved(self) -> None:
        args = copy.deepcopy(j3_args())
        args["cohorts"][0]["definition"] = None
        with self.assertRaises(ValueError):
            SimulatedCohortStore().create(args)

    def test_a_filter_cohort_round_trips(self) -> None:
        definition = {"kind": "filter", "clauses": [{"field": "demographic.race", "op": "in", "values": ["black or african american"]}]}
        pending = {"nonce": "a" * 32, "cohorts": [{"ref": "black", "name": "Black or African American (ORION)", "definition": definition, "defSha256": def_sha256(definition), "caseCount": 334}]}
        store = SimulatedCohortStore(counter=lambda d: 334)
        outcome = store.create(pending)
        cohort = store.by_id(outcome.saved[0]["cohortId"])
        assert cohort is not None
        self.assertEqual(cohort.filters, {"mode": "and", "root": {"demographic.race": {"operator": "includes", "field": "demographic.race", "operands": ["black or african american"]}}})
        self.assertEqual(filterset_def_sha256(cohort.filters), def_sha256(definition))
        self.assertEqual(cohort.case_count, 334)


class Context(unittest.TestCase):
    def test_cohort_context_has_the_contract_shape(self) -> None:
        store = SimulatedCohortStore(counter=lambda d: 1000)
        self.assertEqual(store.cohort_context(), {"active": {"id": None, "name": "All cases", "filters": {"mode": "and", "root": {}}, "caseCount": 1000}, "saved": []})
        cohort = store.seed("Race", {"mode": "and", "root": {"demographic.race": {"operator": "includes", "field": "demographic.race", "operands": ["asian"]}}}, 332)
        store.set_active(cohort.id)
        context = store.cohort_context()
        self.assertEqual(context["active"], {"id": cohort.id, "name": "Race", "filters": cohort.filters, "caseCount": 332})
        self.assertEqual(len(context["saved"]), 1)
        with self.assertRaises(KeyError):
            store.set_active("sim-9999")

    def test_snapshot_is_labelled(self) -> None:
        self.assertEqual(SimulatedCohortStore().snapshot()["label"], "simulated browser")


if __name__ == "__main__":
    unittest.main()
