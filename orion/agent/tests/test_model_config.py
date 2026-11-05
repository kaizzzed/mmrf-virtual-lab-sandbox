"""Model-role configuration, the factory, callId numbering, warm-up dispatch and the structured
output schemas."""

import unittest
from typing import Any

from pydantic import JsonValue, ValidationError

from orion_agent.interfaces import ComposerOutput
from orion_agent.model.calls import ModelCallScope, current_scope, model_call_scope
from orion_agent.model.config import ModelConfig
from orion_agent.model.factory import RefusingComposer, RefusingPlanner, make_composer, make_planner, make_verifier
from orion_agent.model.schema import SchemaError, composer_output_schema, planner_output_schema, structured_output_schema
from orion_agent.model.stub_composer import StubComposer
from orion_agent.model.verifier import CodeVerifier
from orion_agent.model.warmup import warm_up_roles
from orion_agent.models import ModelCallRecord
from orion_agent.settings import Settings
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

    def test_the_planner_schema_wraps_the_ir_union_within_the_limits(self) -> None:
        schema = planner_output_schema()
        self.check(schema)
        union = schema["properties"]["output"]  # type: ignore[index, call-overload]
        self.assertEqual(len(union["anyOf"]), 3)  # type: ignore[index, call-overload, arg-type]

    def test_the_composer_schema_is_within_the_limits(self) -> None:
        schema = composer_output_schema()
        self.check(schema)
        self.assertEqual(set(schema["required"]), set(ComposerOutput.model_fields))  # type: ignore[arg-type]

    def test_recursion_and_open_objects_are_refused(self) -> None:
        recursive: dict[str, JsonValue] = {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}},
                                           "$defs": {"Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}}}
        with self.assertRaises(SchemaError):
            structured_output_schema(recursive)
        with self.assertRaises(SchemaError):
            structured_output_schema({"type": "object", "additionalProperties": {"type": "string"}})


if __name__ == "__main__":
    unittest.main()
