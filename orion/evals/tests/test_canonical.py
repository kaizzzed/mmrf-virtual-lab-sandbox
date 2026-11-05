"""The harness's own canonical JSON, defSha256, pinnedHash, fingerprint and format_fact reproduce
every shared vector in orion/contracts/fixtures."""

import json
import unittest

from helpers import fixture_events, fixture_json

from orion_evals.canonical import (
    PortableNumberError,
    canonical_json,
    def_sha256,
    definition_canonical,
    definition_from_filterset,
    filterset_def_sha256,
    filterset_from_definition,
    fingerprint_document,
    fingerprint_sha256,
    pinned_hash,
)
from orion_evals.factformat import format_fact


class CohortDefVectors(unittest.TestCase):
    def test_every_vector(self) -> None:
        vectors = fixture_json("cohort-def-vectors.json")["vectors"]
        self.assertGreaterEqual(len(vectors), 15)
        for vector in vectors:
            with self.subTest(vector["name"]):
                if "definition" in vector:
                    self.assertEqual(definition_canonical(vector["definition"]), vector["canonical"])
                    self.assertEqual(def_sha256(vector["definition"]), vector["sha256"])
                else:
                    self.assertEqual(filterset_def_sha256(vector["filterSet"]), vector["sha256"])
                    definition = definition_from_filterset(vector["filterSet"])
                    if vector["canonical"] is None:
                        self.assertIsNone(definition)
                    else:
                        assert definition is not None
                        self.assertEqual(definition_canonical(definition), vector["canonical"])

    def test_the_written_filterset_reads_back_to_the_same_hash(self) -> None:
        for vector in fixture_json("cohort-def-vectors.json")["vectors"]:
            if "definition" not in vector:
                continue
            with self.subTest(vector["name"]):
                written = filterset_from_definition(vector["definition"])
                self.assertEqual(written["mode"], "and")
                self.assertEqual(filterset_def_sha256(written), vector["sha256"])

    def test_the_contract_filterset_forms(self) -> None:
        ids = filterset_from_definition({"kind": "case_ids", "caseIds": ["SYNTH-CASE-0003", "SYNTH-CASE-0001"]})
        self.assertEqual(ids, {"mode": "and", "root": {"case_id": {"operator": "in", "field": "case_id", "operands": ["SYNTH-CASE-0001", "SYNTH-CASE-0003"]}}})
        mixed = filterset_from_definition(
            {
                "kind": "filter",
                "clauses": [
                    {"field": "demographic.race", "op": "in", "values": ["white"]},
                    {"field": "diagnoses.age_at_diagnosis", "op": ">=", "values": [65]},
                    {"field": "diagnoses.age_at_diagnosis", "op": "<", "values": [75.5]},
                ],
            }
        )
        self.assertEqual(mixed["root"]["demographic.race"], {"operator": "includes", "field": "demographic.race", "operands": ["white"]})
        self.assertEqual(
            mixed["root"]["diagnoses.age_at_diagnosis"],
            {"operator": "and", "operands": [{"operator": "<", "field": "diagnoses.age_at_diagnosis", "operand": 75.5}, {"operator": ">=", "field": "diagnoses.age_at_diagnosis", "operand": 65}]},
        )

    def test_the_j3_pending_hashes_reproduce_from_their_definitions(self) -> None:
        args = "".join(e["delta"] for e in fixture_events("j3-save-pause.events.jsonl") if e["type"] == "TOOL_CALL_ARGS")
        pending = json.loads(args)
        for cohort in pending["cohorts"]:
            with self.subTest(cohort["ref"]):
                self.assertEqual(def_sha256(cohort["definition"]), cohort["defSha256"])


class Canonical(unittest.TestCase):
    def test_numbers_print_as_javascript_does(self) -> None:
        self.assertEqual(canonical_json({"b": 2.0, "a": [65.0, 75.5, 0.3, None, True]}), '{"a":[65,75.5,0.3,null,true],"b":2}')
        self.assertEqual(canonical_json({"é": "café – x"}), '{"é":"café – x"}')

    def test_portable_range_is_enforced(self) -> None:
        with self.assertRaises(PortableNumberError):
            canonical_json(5e-05)
        with self.assertRaises(PortableNumberError):
            canonical_json(2**53)
        self.assertEqual(canonical_json(7.1e-60, portable=False), "7.1e-60")


class PinnedAndFingerprint(unittest.TestCase):
    def test_viz_pinned_vectors(self) -> None:
        for vector in fixture_json("viz-pinned-vectors.json")["vectors"]:
            with self.subTest(vector["name"]):
                self.assertEqual(pinned_hash(vector["pinned"]), vector["sha256"])

    def test_fingerprint_vectors(self) -> None:
        for vector in fixture_json("fingerprint-vectors.json")["vectors"]:
            with self.subTest(vector["name"]):
                src = vector["sources"]
                document = fingerprint_document(src["sandboxStatus"], src["analysisVersion"], src["healthcheck"], src["ranking"])
                self.assertEqual(document, vector["document"])
                self.assertEqual(canonical_json(document), vector["canonical"])
                self.assertEqual(fingerprint_sha256(document), vector["sha256"])

    def test_fixture_viz_specs_hash_as_they_say(self) -> None:
        events = fixture_events("j1-complete.events.jsonl")
        spec = [e for e in events if e["type"] == "STATE_SNAPSHOT"][-1]["snapshot"]["vizSpecs"][0]
        self.assertEqual(pinned_hash(spec["pinned"]), spec["pinnedHash"])


class FactFormat(unittest.TestCase):
    def test_vectors(self) -> None:
        for vector in fixture_json("fact-format-vectors.json")["vectors"]:
            with self.subTest(value=vector["value"], unit=vector["unit"]):
                self.assertEqual(format_fact(vector["value"], vector["unit"]), vector["text"])

    def test_refuses_non_finite(self) -> None:
        with self.assertRaises(ValueError):
            format_fact(float("nan"), "p")


if __name__ == "__main__":
    unittest.main()
