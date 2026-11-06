"""JSON schemas for structured output, within what the API's `output_config.format` accepts.

Structured output takes `type`, `properties`, `required`, `items`, `enum`, `const`, `anyOf`,
`allOf`, `$ref`/`$defs`, `description` and `additionalProperties: false` on every object. It
takes no recursion and no numeric, length or array-size constraints. The Pydantic schemas of the
IR and of ComposerOutput carry those constraints (and `oneOf` with a `discriminator` for tagged
unions), so this module sends a reduced copy and the roles validate the reply against the full
Pydantic model in code, which is where every stripped constraint is enforced. `ir.py` and
`interfaces.py` stay unchanged.
"""

import copy
from collections.abc import Mapping
from typing import Final

from pydantic import JsonValue

from .. import ir
from ..interfaces import ComposerOutput

# Dropped everywhere: constraints the API rejects, and annotations it does not need.
_DROPPED: Final = frozenset(
    {
        "minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties",
        "default", "title", "discriminator", "examples",
    }
)
_SUPPORTED_FORMATS: Final = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)
# The planner's union is wrapped in an object: {"output": <PlannerOutput>}.
PLANNER_OUTPUT_KEY: Final = "output"


class SchemaError(ValueError):
    """A schema structured output cannot express (recursion, an open object)."""


def _clean(node: JsonValue, path: str) -> JsonValue:
    if isinstance(node, list):
        return [_clean(item, f"{path}/{i}") for i, item in enumerate(node)]
    if not isinstance(node, dict):
        return node
    out: dict[str, JsonValue] = {}
    for key, value in node.items():
        if key in _DROPPED:
            continue
        if key == "format" and value not in _SUPPORTED_FORMATS:
            continue
        if key in ("properties", "$defs"):
            if not isinstance(value, dict):
                raise SchemaError(f"{path}/{key} is not an object")
            out[key] = {name: _clean(child, f"{path}/{key}/{name}") for name, child in value.items()}
            continue
        out["anyOf" if key == "oneOf" else key] = _clean(value, f"{path}/{key}")
    if out.get("type") == "object" or "properties" in out:
        extra = out.get("additionalProperties", False)
        if extra is not False:
            raise SchemaError(f"{path} is an open object; structured output needs additionalProperties false")
        out["additionalProperties"] = False
    return out


def _refs(node: JsonValue) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            found.add(ref.removeprefix("#/$defs/"))
        for key, value in node.items():
            if key != "$defs":
                found |= _refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _refs(item)
    return found


def _check_acyclic(schema: Mapping[str, JsonValue]) -> None:
    defs = schema.get("$defs")
    graph = {name: _refs(body) for name, body in defs.items()} if isinstance(defs, dict) else {}
    state: dict[str, int] = {}  # 1 visiting, 2 done

    def visit(name: str, trail: tuple[str, ...]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            raise SchemaError(f"recursive schema: {' -> '.join((*trail, name))}")
        if name not in graph:
            raise SchemaError(f"$ref to an undefined definition {name!r}")
        state[name] = 1
        for child in sorted(graph[name]):
            visit(child, (*trail, name))
        state[name] = 2

    for name in sorted(_refs({key: value for key, value in schema.items() if key != "$defs"}) | set(graph)):
        visit(name, ())


def structured_output_schema(schema: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """A copy of `schema` that structured output accepts; raises SchemaError when none exists."""
    cleaned = _clean(copy.deepcopy(dict(schema)), "#")
    assert isinstance(cleaned, dict)
    if cleaned.get("type") != "object":
        raise SchemaError("the top level of a structured output schema is an object")
    _check_acyclic(cleaned)
    return cleaned


def planner_output_schema() -> dict[str, JsonValue]:
    """`{"output": PlannerOutput}`: the IR's tagged union, wrapped so the top level is an object."""
    union = dict(ir.planner_output_json_schema())
    defs = union.pop("$defs", {})
    wrapped: dict[str, JsonValue] = {
        "type": "object",
        "properties": {PLANNER_OUTPUT_KEY: union},
        "required": [PLANNER_OUTPUT_KEY],
        "additionalProperties": False,
        "$defs": defs,
    }
    return structured_output_schema(wrapped)


def composer_output_schema() -> dict[str, JsonValue]:
    return structured_output_schema(ComposerOutput.model_json_schema(mode="validation"))
