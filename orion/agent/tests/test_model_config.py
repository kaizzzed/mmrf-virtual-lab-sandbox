"""Model-role configuration, the factory, callId numbering, warm-up dispatch and the structured
output schemas."""

import json
import unittest
from typing import Any

from pydantic import JsonValue, ValidationError

from orion_agent.interfaces import ComposerOutput
from orion_agent.model.calls import ModelCallScope, current_scope, model_call_scope
from orion_agent.model.config import ModelConfig
from orion_agent.model.factory import RefusingComposer, RefusingPlanner, make_composer, make_planner, make_verifier
from orion_agent import ir
from orion_agent.model.prompts import PLANNER_EXAMPLES
from orion_agent.model.schema import (
    PLANNER_OUTPUT_KEY,
    PLANNER_POLICY,
    SchemaError,
    WirePolicy,
    _planner_source,
    composer_output_schema,
    composer_wire,
    grammar_profile,
    planner_output_schema,
    planner_wire,
    structured_output_schema,
    wire_schema,
)
from orion_agent.model.stub_composer import StubComposer
from orion_agent.model.verifier import CodeVerifier
from orion_agent.model.warmup import warm_up_roles
from orion_agent.models import ModelCallRecord
from orion_agent.settings import Settings
from orion_agent.stubs import J3_NAMED, J3_OPEN, plan_question
from support import FIELDS_PATH, context
from test_model_mock import HAS_SDK, RUN_ID, SKIP_REASON, run

UNSUPPORTED = {"minLength", "maxLength", "pattern", "minimum", "maximum", "minItems", "maxItems", "oneOf", "discriminator", "default", "title"}


def walk(node: JsonValue, path: str = "#") -> list[tuple[str, dict[str, JsonValue]]]:
    found: list[tuple[str, dict[str, JsonValue]]] = []
    if isinstance(node, dict):
        found.append((path, node))
        for key, value in node.items():
            found += walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found += walk(item, f"{path}/{i}")
    return found


class ConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        config = ModelConfig.from_env({})
        self.assertEqual((config.planner, config.composer, config.region), ("stub", "stub", "us-east-1"))
        self.assertEqual((config.planner_effort, config.composer_effort), ("low", "medium"))
        self.assertEqual((config.planner_timeout_s, config.composer_timeout_s, config.max_retries, config.max_tokens), (30.0, 60.0, 1, 16000))
        self.assertEqual(config.cache_ttl, "5m")
        self.assertFalse(config.uses_bedrock)

    def test_every_variable_is_read(self) -> None:
        config = ModelConfig.from_env({
            "ORION_PLANNER": "Bedrock", "ORION_COMPOSER": "bedrock", "ORION_BEDROCK_CLIENT": "mantle", "ORION_BEDROCK_REGION": "us-west-2",
            "ORION_PLANNER_MODEL": "anthropic.claude-sonnet-5-5", "ORION_COMPOSER_MODEL": "anthropic.claude-opus-5-5",
            "ORION_FALLBACK_MODEL": "anthropic.claude-opus-5", "ORION_PLANNER_EFFORT": "medium", "ORION_COMPOSER_EFFORT": "high",
            "ORION_PLANNER_TIMEOUT_S": "12.5", "ORION_COMPOSER_TIMEOUT_S": "90", "ORION_MODEL_MAX_RETRIES": "0",
            "ORION_MODEL_MAX_TOKENS": "8000", "ORION_MODEL_CACHE_TTL": "1h",
        })
        self.assertEqual((config.planner, config.composer, config.bedrock_client, config.region), ("bedrock", "bedrock", "mantle", "us-west-2"))
        self.assertEqual((config.planner_model, config.composer_model, config.fallback_model),
                         ("anthropic.claude-sonnet-5-5", "anthropic.claude-opus-5-5", "anthropic.claude-opus-5"))
        self.assertEqual((config.planner_effort, config.composer_effort, config.planner_timeout_s, config.composer_timeout_s),
                         ("medium", "high", 12.5, 90.0))
        self.assertEqual((config.max_retries, config.max_tokens, config.cache_ttl), (0, 8000, "1h"))
        self.assertTrue(config.uses_bedrock)

    def test_the_fallback_can_be_turned_off(self) -> None:
        for value in ("", "none", "OFF"):
            self.assertIsNone(ModelConfig.from_env({"ORION_FALLBACK_MODEL": value}).fallback_model)

    def test_mantle_takes_base_ids_by_default(self) -> None:
        config = ModelConfig.from_env({"ORION_BEDROCK_CLIENT": "mantle"})
        self.assertEqual((config.planner_model, config.fallback_model), ("anthropic.claude-opus-5-5", "anthropic.claude-opus-5"))

    def test_values_the_record_cannot_hold_are_rejected(self) -> None:
        for env in ({"ORION_PLANNER_EFFORT": "max"}, {"ORION_PLANNER": "openai"}, {"ORION_MODEL_MAX_RETRIES": "4"},
                    {"ORION_PLANNER_TIMEOUT_S": "0"}, {"ORION_MODEL_CACHE_TTL": "2h"}):
            with self.subTest(env=env), self.assertRaises(ValidationError):
                ModelConfig.from_env(env)


class FactoryTest(unittest.TestCase):
    def settings(self, **values: Any) -> Settings:
        return Settings(fields_path=FIELDS_PATH, **values)

    def test_stub_roles_and_the_code_verifier(self) -> None:
        from orion_agent.stubs import StubPlanner

        config = ModelConfig()
        self.assertIsInstance(make_planner(self.settings(), config=config), StubPlanner)
        self.assertIsInstance(make_composer(self.settings(), config=config), StubComposer)
        self.assertIsInstance(make_verifier(self.settings()), CodeVerifier)

    def test_refuse_faults_decline_without_a_model_call(self) -> None:
        settings = self.settings(faults="planner:refuse,composer:refuse")
        planner, composer = make_planner(settings, config=ModelConfig()), make_composer(settings, config=ModelConfig())
        self.assertIsInstance(planner, RefusingPlanner)
        self.assertIsInstance(composer, RefusingComposer)
        result = run(planner.plan("q", context(), None))
        self.assertEqual((result.output, result.declined, result.model_calls), (None, True, []))

    @unittest.skipUnless(HAS_SDK, SKIP_REASON)
    def test_bedrock_roles_share_one_client(self) -> None:
        from anthropic import AsyncAnthropicBedrock

        from orion_agent.model.composer import BedrockComposer
        from orion_agent.model.planner import BedrockPlanner

        config = ModelConfig(planner="bedrock", composer="bedrock")
        planner, composer = make_planner(self.settings(), config=config), make_composer(self.settings(), config=config)
        assert isinstance(planner, BedrockPlanner) and isinstance(composer, BedrockComposer)
        self.assertIs(planner._client, composer._client)
        self.assertIsInstance(planner._client.sdk, AsyncAnthropicBedrock)

    def test_warm_up_skips_stubs(self) -> None:
        from orion_agent.stubs import StubPlanner

        results = run(warm_up_roles(StubPlanner(), StubComposer()))
        self.assertEqual([(r.role, r.outcome) for r in results], [("planner", "skipped"), ("composer", "skipped")])


class ScopeTest(unittest.TestCase):
    def test_call_ids_number_per_role_and_plan_version(self) -> None:
        scope = ModelCallScope(RUN_ID)
        ids = [scope.next_call_id("planner"), scope.next_call_id("planner"), scope.next_call_id("composer", 1), scope.next_call_id("composer", 2)]
        self.assertEqual(ids, [f"{RUN_ID}.planner.c1", f"{RUN_ID}.planner.c2", f"{RUN_ID}.v1.composer.c1", f"{RUN_ID}.v2.composer.c1"])
        for call_id in ids:
            ModelCallRecord.model_validate(
                {"callId": call_id, "role": "planner", "model": "m", "fallback": False, "effort": "low", "attempt": 1,
                 "startedAt": "2026-11-05T10:00:00.000Z", "elapsedMs": 0, "outcome": "ok", "stopReason": None, "inputTokens": None,
                 "outputTokens": None, "cacheReadTokens": None, "cacheWriteTokens": None}
            )

    def test_the_scope_is_entered_and_left(self) -> None:
        self.assertIsNone(current_scope())
        with model_call_scope(RUN_ID) as scope:
            self.assertIs(current_scope(), scope)
        self.assertIsNone(current_scope())
        with self.assertRaises(ValueError):
            ModelCallScope("not-a-run")
        with self.assertRaises(ValueError):
            ModelCallScope(RUN_ID).next_call_id("composer")


class SchemaTest(unittest.TestCase):
    def check(self, schema: dict[str, JsonValue]) -> None:
        self.assertEqual(schema["type"], "object")
        for path, node in walk(schema):
            if path.endswith("/properties") or path.endswith("/$defs"):
                continue
            self.assertEqual(set(node) & UNSUPPORTED, set(), path)
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False, path)

    def test_the_planner_schema_keeps_two_small_unions_within_the_limits(self) -> None:
        schema = planner_output_schema()
        self.check(schema)
        output: Any = schema["properties"]["output"]  # type: ignore[index, call-overload]
        self.assertEqual([b["properties"]["kind"]["const"] for b in output["anyOf"]], ["plan", "clarify", "unsupported"])
        plan = output["anyOf"][0]
        self.assertEqual(plan["required"], ["kind", "base_cohort", "follows", "cohorts", "analyses", "named_tools"])
        analyses = plan["properties"]["analyses"]["items"]["anyOf"]
        self.assertEqual([b["properties"]["kind"]["const"] for b in analyses],
                         ["survival_compare", "top_mutated_genes", "differential_expression", "cohort_size"])
        cohort = plan["properties"]["cohorts"]["items"]
        self.assertEqual(cohort["required"], ["ref", "def"])  # a cohort's name is left out unless stated
        definition = cohort["properties"]["def"]
        self.assertEqual(definition["required"], ["kind"])  # the seven kinds flattened, each kind's fields optional
        self.assertEqual(len(definition["properties"]["kind"]["enum"]), 7)
        # What Bedrock compiled on 2026-11-05 (about 12 s on first use): 8 optional properties and 2 unions of
        # objects, no $ref. The documented limits are 24 optional properties and 16 properties with a union;
        # the cohort definition kept as a union was refused as too large, and 21 optional properties took 25 s.
        self.assertEqual(grammar_profile(schema), {"optional": 8, "unions": 4, "object_unions": 2, "refs": 0})
        self.assertLess(len(json.dumps(schema)), 5_000, "the grammar that compiled was 4.7 KB of schema; a larger one needs a live check")

    def test_the_composer_schema_is_within_the_limits(self) -> None:
        schema = composer_output_schema()
        self.check(schema)
        self.assertEqual(set(schema["required"]), set(ComposerOutput.model_fields))  # type: ignore[arg-type]
        self.assertEqual(grammar_profile(schema), {"optional": 0, "unions": 0, "object_unions": 0, "refs": 0})

    def test_every_planner_example_round_trips_through_the_wire_encoding(self) -> None:
        wire = planner_wire()
        branches = {b["properties"]["kind"]["const"]: b for b in planner_output_schema()["properties"]["output"]["anyOf"]}  # type: ignore[index, call-overload, union-attr]
        own_fields = {"filter": {"clauses"}, "gene_mutated": {"gene", "within"}, "mutation_tested": {"within"},
                      "difference": {"a", "b"}, "intersection": {"a", "b"}, "saved": {"cohort_name"}, "thread": {"ledger_ref"}}
        for example in PLANNER_EXAMPLES:
            with self.subTest(example.question):
                data: JsonValue = json.loads(json.dumps({PLANNER_OUTPUT_KEY: dict(example.output)}))
                encoded: Any = wire.encode(data)
                output = encoded[PLANNER_OUTPUT_KEY]
                # Every property of the kind's object is present, as the grammar requires.
                self.assertEqual(list(output), branches[output["kind"]]["required"])  # type: ignore[index, call-overload]
                for cohort in output.get("cohorts", []):
                    self.assertEqual(set(cohort["def"]) - {"kind"}, own_fields[cohort["def"]["kind"]], cohort)
                decoded = wire.decode(encoded)
                assert isinstance(decoded, dict)
                self.assertEqual(ir.PLANNER_OUTPUT.validate_python(decoded[PLANNER_OUTPUT_KEY]),
                                 ir.PLANNER_OUTPUT.validate_python(data[PLANNER_OUTPUT_KEY]))  # type: ignore[index, call-overload]

    def test_the_j3_plan_is_short_on_the_wire(self) -> None:
        """NFR-01: planner latency is about 1.5 s plus 10 ms per output token (Bedrock, 2026-11-05), so the
        plan-visible p95 of 5 s rests on J3, the longest reference plan, staying short in the wire encoding."""
        for question in (J3_OPEN, J3_NAMED):
            with self.subTest(question):
                output = plan_question(question, context(), None)
                encoded = planner_wire().encode({PLANNER_OUTPUT_KEY: output.model_dump(mode="json", by_alias=True, exclude_none=True)})
                text = json.dumps(encoded, separators=(",", ":"))
                self.assertLess(len(text), 560, text)  # 869 characters, about 400 tokens, with every field present
                decoded: Any = planner_wire().decode(json.loads(text))
                self.assertEqual(ir.PLANNER_OUTPUT.validate_python(decoded[PLANNER_OUTPUT_KEY]), output)

    def test_decoding_drops_only_empty_values_and_leaves_the_rest_to_validation(self) -> None:
        wire = planner_wire()
        reply: dict[str, Any] = {"output": {
            "kind": "plan", "base_cohort": [{"source": "all_cases", "cohort_name": "", "ledger_ref": ""}], "follows": "",
            "cohorts": [
                {"ref": "tested", "def": {"kind": "mutation_tested", "within": "base", "a": "", "clauses": []}, "name": ""},
                {"ref": "kras", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "tested"}},
                {"ref": "kras_wt", "def": {"kind": "difference", "a": "tested", "b": "kras"}, "name": "KRAS wild type (ORION)"}],
            "analyses": [
                {"kind": "differential_expression", "group1": "kras_wt", "group2": "kras", "method": "", "rank_by": "", "top_n": 0,
                 "thresholds": []},
                {"kind": "top_mutated_genes", "cohort": "kras", "n": 5}],
            "named_tools": []}}
        decoded = wire.decode(reply)
        self.assertEqual(decoded, {"output": {
            "kind": "plan", "base_cohort": {"source": "all_cases"},
            "cohorts": [{"ref": "tested", "def": {"kind": "mutation_tested", "within": "base"}},
                        {"ref": "kras", "def": {"kind": "gene_mutated", "gene": "KRAS", "within": "tested"}},
                        {"ref": "kras_wt", "def": {"kind": "difference", "a": "tested", "b": "kras"}, "name": "KRAS wild type (ORION)"}],
            "analyses": [{"kind": "differential_expression", "group1": "kras_wt", "group2": "kras"},
                         {"kind": "top_mutated_genes", "cohort": "kras", "n": 5}]}})
        ir.PLANNER_OUTPUT.validate_python(decoded["output"])  # type: ignore[index, call-overload]
        # A field of another kind, a missing field of the kind, a second base cohort, or an unknown kind is
        # passed on (or left missing), and the IR refuses it.
        for path, value in ((("cohorts", 0, "def", "gene"), "KRAS"), (("cohorts", 1, "def", "within"), None),
                            (("analyses", 1, "group1"), "kras"), (("analyses", 0, "kind"), "survival_compare"),
                            (("base_cohort",), [{"source": "all_cases"}, {"source": "active_cohort"}]),
                            (("slot",), "gene"), (("kind",), "edit")):
            bad = json.loads(json.dumps(reply))
            node = bad["output"]
            for key in path[:-1]:
                node = node[key]
            if value is None:
                del node[path[-1]]
            else:
                node[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValidationError):
                ir.PLANNER_OUTPUT.validate_python(wire.decode(bad)["output"])  # type: ignore[index, call-overload]
        # A count of 0 is "not stated" only where the IR's own minimum is 1 or more.
        zero: dict[str, JsonValue] = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}, "required": [],
                                      "additionalProperties": False}
        with self.assertRaises(SchemaError):
            wire_schema(zero)

    def test_the_policy_names_only_what_the_schema_has(self) -> None:
        source = _planner_source()
        for policy in (
            WirePolicy(optional=frozenset({"output.cohorts[].ref"})),  # required by every kind: never optional
            WirePolicy(optional=frozenset({"output.base_cohort"})),  # an object: the encoding sends none optional
            WirePolicy(optional=frozenset({"output.nothing"})),
            WirePolicy(unions=frozenset({"output.cohorts[]"})),  # not a union
        ):
            with self.subTest(policy=policy), self.assertRaises(SchemaError):
                wire_schema(source, policy)
        # The default policy flattens every union and requires every property, as stage 2 first sent it.
        self.assertEqual(grammar_profile(wire_schema(source).schema), {"optional": 0, "unions": 2, "object_unions": 0, "refs": 0})
        self.assertEqual(PLANNER_POLICY.unions, frozenset({"output", "output.analyses[]"}))

    def test_the_composer_reply_needs_no_decoding(self) -> None:
        reply: JsonValue = {"interpretation": [{"text": "t", "cites": ["f.s1.cases"]}], "hypotheses": [], "follow_ups": [], "open_app_ids": []}
        self.assertEqual(composer_wire().decode(reply), reply)

    def test_recursion_and_open_objects_are_refused(self) -> None:
        recursive: dict[str, JsonValue] = {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}},
                                           "$defs": {"Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}}}
        with self.assertRaises(SchemaError):
            structured_output_schema(recursive)
        with self.assertRaises(SchemaError):
            structured_output_schema({"type": "object", "additionalProperties": {"type": "string"}})


if __name__ == "__main__":
    unittest.main()
