"""The Gateway tool schema (orion/infra/gateway_tools.py) against the catalog and the fixtures.

Runs in the agent image with the agent package, the contracts and orion/infra mounted:

    docker run --rm -v $PWD/orion/agent:/app:ro -v $PWD/orion/contracts:/contracts:ro \\
      -v $PWD/orion/infra:/infra:ro -w /infra -e PYTHONPATH=/app:/infra orion-agent:dev \\
      python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast, get_args

import gateway_tools as g
from orion_agent.tools.base import GATEWAY_TOOL_SEPARATOR, ServerToolName
from orion_agent.tools.schemas import TOOL_REGISTRY, gateway_tool_name

INFRA = Path(__file__).resolve().parents[1]
SCHEMA_FILE = INFRA / "terraform" / "orion-infra" / "vlab-tools.json"
CONTRACTS = Path(os.environ.get("ORION_CONTRACTS_DIR", "/contracts"))
FIXTURES = CONTRACTS / "fixtures"

JsonObject = dict[str, object]

# Valid synthetic Input for every vlab tool, beside the recorded fixture requests (illustrative values).
SAMPLES: dict[str, list[JsonObject]] = {
    "fixture_fingerprint": [{}],
    "cohort_count": [{"filter": {"and": [{"in": {"demographic.race": ["black or african american"]}}]}}],
    "cases_query": [
        {"filter": {"and": [{"in": {"available_variation_data": ["ssm"]}}]}},
        {"filter": {"and": []}, "pageSize": 1000, "maxIds": 10000},
    ],
    "gene_cases": [{"gene": "KRAS", "cohortFilter": {"and": []}}],
    "top_mutated_genes": [
        {"filter0": None, "geneCount": 3},
        {"filter0": {"and": [{"in": {"case_id": ["SYNTH-CASE-0001"]}}]}, "geneCount": 10},
    ],
    "survival_compare": [],  # from the run-record fixtures and request-pinned vectors
    "sample_lists": [
        {"groups": [{"key": "control", "caseIds": ["SYNTH-CASE-0002"]}, {"key": "case", "caseIds": ["SYNTH-CASE-0001"]}]}
    ],
    "de_preanalysis": [{"groups": [{"key": "control", "sampleIds": [2, 5]}, {"key": "case", "sampleIds": [1, 3]}]}],
    "de_run": [
        {"groups": [{"name": "KRAS not detected (ORION)", "sampleIds": [2, 5]}, {"name": "KRAS mutated (ORION)", "sampleIds": [1, 3]}],
         "method": "wilcoxon"},
    ],
}

TOOL_FOR_LAUNCHER = {"survival": "survival_compare", "oncomatrix": "top_mutated_genes", "deDirect": "de_run", "deInput": "de_run"}


def _fixture_requests() -> dict[str, list[JsonObject]]:
    """Every server-tool request the fixtures record: run-record calls and request-pinned vectors."""
    found: dict[str, list[JsonObject]] = {}
    for name in ("run-record.j1-complete.json", "run-record.j1-retry.json"):
        record = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        for call in record["calls"]:
            if TOOL_REGISTRY[call["tool"]].kind == "server":
                found.setdefault(call["tool"], []).append(call["request"])
    vectors = json.loads((FIXTURES / "request-pinned-vectors.json").read_text(encoding="utf-8"))
    for vector in vectors["vectors"]:
        found.setdefault(TOOL_FOR_LAUNCHER[vector["launcher"]], []).append(vector["request"])
    return found


def conformance_errors(value: object, node: JsonObject, path: str = "$") -> list[str]:
    """Where `value` breaks a Gateway schema node: types, required keys, unknown keys.

    A null is accepted only for a property the node does not list in `required` (the nullable
    rule in gateway_tools); the Lambda's Input model decides whether null is meant.
    """
    kind = node["type"]
    checks: dict[str, bool] = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if not checks[cast(str, kind)]:
        return [f"{path}: expected {kind}, got {type(value).__name__}"]
    errors: list[str] = []
    if kind == "object" and "properties" in node:
        obj = cast(JsonObject, value)
        properties = cast(dict[str, JsonObject], node["properties"])
        required = cast(list[str], node.get("required", []))
        for name in required:
            if obj.get(name) is None:
                errors.append(f"{path}.{name}: required")
        for name, child in obj.items():
            if name not in properties:
                errors.append(f"{path}.{name}: not in the schema")
            elif child is not None:
                errors.extend(conformance_errors(child, properties[name], f"{path}.{name}"))
    if kind == "array":
        for index, item in enumerate(cast(list[object], value)):
            errors.extend(conformance_errors(item, cast(JsonObject, node["items"]), f"{path}[{index}]"))
    return errors


def subset_errors(node: JsonObject, path: str = "$") -> list[str]:
    """Where a generated node leaves the Gateway subset."""
    errors = [f"{path}: key {key}" for key in node if key not in g.GATEWAY_KEYS]
    kind = node.get("type")
    if kind not in g.GATEWAY_TYPES:
        errors.append(f"{path}: type {kind!r}")
    if "properties" in node:
        if kind != "object":
            errors.append(f"{path}: properties on {kind}")
        properties = cast(dict[str, JsonObject], node["properties"])
        for name in cast(list[str], node.get("required", [])):
            if name not in properties:
                errors.append(f"{path}: required {name} is not a property")
        for name, child in properties.items():
            errors.extend(subset_errors(child, f"{path}.{name}"))
    elif "required" in node:
        errors.append(f"{path}: required without properties")
    if "items" in node:
        if kind != "array":
            errors.append(f"{path}: items on {kind}")
        errors.extend(subset_errors(cast(JsonObject, node["items"]), f"{path}[]"))
    elif kind == "array":
        errors.append(f"{path}: array without items")
    return errors


class GatewayToolSchemaTest(unittest.TestCase):
    tools: list[JsonObject]
    by_name: dict[str, JsonObject]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = g.gateway_tools()
        cls.by_name = {cast(str, tool["name"]): tool for tool in cls.tools}

    def test_committed_file_is_current(self) -> None:
        self.assertEqual(SCHEMA_FILE.read_text(encoding="utf-8"), g.render(self.tools),
                         "vlab-tools.json is stale: rerun gateway_tools.py --write")

    def test_nine_vlab_tools_in_catalog_order(self) -> None:
        expected = [name for name in get_args(ServerToolName) if name != "kg_query"]
        self.assertEqual([tool["name"] for tool in self.tools], expected)
        for tool in self.tools:
            name = cast(ServerToolName, tool["name"])
            self.assertEqual(f"vlab{GATEWAY_TOOL_SEPARATOR}{name}", gateway_tool_name(name))
            self.assertEqual(set(tool), {"name", "description", "inputSchema"})
            self.assertIn(TOOL_REGISTRY[name].version, cast(str, tool["description"]))

    def test_kg_target_is_separate(self) -> None:
        self.assertEqual([tool["name"] for tool in g.gateway_tools("kg")], ["kg_query"])
        self.assertNotIn("kg_query", self.by_name)
        with self.assertRaises(ValueError):
            g.gateway_tools("other")

    def test_every_node_stays_in_the_gateway_subset(self) -> None:
        for target in g.TARGETS:
            for tool in g.gateway_tools(target):
                schema = cast(JsonObject, tool["inputSchema"])
                self.assertEqual(schema["type"], "object")
                self.assertEqual(subset_errors(schema), [], tool["name"])

    def test_no_input_property_is_lost(self) -> None:
        for name, tool in self.by_name.items():
            source = TOOL_REGISTRY[cast(ServerToolName, name)].input_model.model_json_schema(by_alias=True)
            generated = cast(dict[str, object], cast(JsonObject, tool["inputSchema"])["properties"])
            self.assertEqual(set(generated), set(cast(dict[str, object], source.get("properties", {}))), name)

    def test_enums_are_named_in_descriptions(self) -> None:
        de_run = cast(JsonObject, self.by_name["de_run"]["inputSchema"])
        method = cast(JsonObject, cast(dict[str, object], de_run["properties"])["method"])
        self.assertEqual(method["type"], "string")
        self.assertIn('"wilcoxon"', cast(str, method["description"]))

    def test_required_nullable_fields_are_optional_on_the_gateway(self) -> None:
        # Gateway cannot say "or null": these required-but-nullable Input fields are shown optional,
        # so a transport that strips nulls would break them. Pinned so a change gets reviewed.
        found = {
            name: g.required_nullable(TOOL_REGISTRY[cast(ServerToolName, name)].input_model.model_json_schema(by_alias=True))
            for name in self.by_name
        }
        self.assertEqual({name: paths for name, paths in found.items() if paths},
                         {"top_mutated_genes": ["filter0"], "survival_compare": ["scope", "split"]})
        for name, paths in found.items():
            schema = cast(JsonObject, self.by_name[name]["inputSchema"])
            for path in paths:
                self.assertNotIn(path, cast(list[str], schema.get("required", [])))

    def test_recorded_and_sample_requests_conform(self) -> None:
        requests = _fixture_requests()
        self.assertTrue({"fixture_fingerprint", "cohort_count", "survival_compare", "top_mutated_genes", "de_run"} <= set(requests))
        for name, samples in SAMPLES.items():
            requests.setdefault(name, []).extend(samples)
        self.assertEqual(set(requests), set(self.by_name))
        for name, bodies in requests.items():
            model = TOOL_REGISTRY[cast(ServerToolName, name)].input_model
            schema = cast(JsonObject, self.by_name[name]["inputSchema"])
            self.assertTrue(bodies, name)
            for body in bodies:
                model.model_validate(body)  # every case is a real Input first
                self.assertEqual(conformance_errors(body, schema), [], f"{name}: {json.dumps(body)[:200]}")

    def test_conformance_check_catches_mismatches(self) -> None:
        schema = cast(JsonObject, self.by_name["gene_cases"]["inputSchema"])
        self.assertEqual(conformance_errors({"gene": "KRAS"}, schema), ["$.cohortFilter: required"])
        self.assertEqual(conformance_errors({"gene": 7, "cohortFilter": {}}, schema), ["$.gene: expected string, got int"])
        self.assertEqual(conformance_errors({"gene": "KRAS", "cohortFilter": {}, "extra": 1}, schema), ["$.extra: not in the schema"])

    def test_unsupported_unions_are_refused(self) -> None:
        with self.assertRaises(g.SchemaError):
            g.convert({"anyOf": [{"type": "string"}, {"type": "integer"}]}, {})
        with self.assertRaises(g.SchemaError):
            g.convert({"$ref": "#/$defs/Missing"}, {})
        self.assertEqual(g.convert({"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "n"}, {}),
                         {"type": "integer", "description": "n"})

    def test_check_mode_reports_a_stale_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vlab-tools.json"
            self.assertEqual(g.main(["--write", str(path)]), 0)
            self.assertEqual(g.main(["--check", str(path)]), 0)
            path.write_text("[]\n", encoding="utf-8")
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(g.main(["--check", str(path)]), 1)
            self.assertIn("stale", err.getvalue())


if __name__ == "__main__":
    unittest.main()
