"""The AgentCore Gateway tool schema for a Lambda target, generated from the tool catalog.

RFA rev 25, A1 seams: "MCP tool schemas generated from one JSON Schema source". The source is
each server tool's Input model in `orion_agent.tools.schemas.TOOL_REGISTRY`; this module turns
its Pydantic JSON Schema into the subset a Gateway target's tool schema accepts (Terraform
`tool_schema`, here the S3 form): every schema node has a `type` (string, number, integer,
boolean, array or object) and may carry `description`, `properties`, `required` and `items`.

Rules, in order, for each node:
- `$ref` is inlined from `$defs` (the catalog's models have no recursive references).
- `anyOf`/`oneOf` with a `null` branch: the null branch is dropped. Gateway cannot say "or null",
  so a nullable property is never listed in `required`, and the Lambda's Input model stays the
  authority on whether null is accepted (`required_nullable` lists where it must be).
- `enum` and `const` keep their JSON type; the allowed values go into the description.
- A node with no type (`JsonValue`) is an `object` when it sits under an object's
  `additionalProperties` parent, which is how every Gen3 filter is typed.
- Numeric bounds, lengths, patterns, defaults and titles are dropped: the Lambda validates the
  call against the same Input model before any HTTP request (TOOLS.md 1, "Error mapping").

Run from the sandbox repo root, in the agent image:

    docker run --rm -v $PWD/orion/agent:/app:ro -v $PWD/orion/infra:/infra -w /app \\
      -e PYTHONPATH=/app orion-agent:dev python /infra/gateway_tools.py \\
      --write /infra/terraform/orion-infra/vlab-tools.json        # or --check
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

from orion_agent.tools.base import GATEWAY_TARGET, KG_GATEWAY_TARGET
from orion_agent.tools.schemas import TOOL_REGISTRY

JsonObject = dict[str, object]

GATEWAY_TYPES: Final = frozenset({"string", "number", "integer", "boolean", "array", "object"})
GATEWAY_KEYS: Final = frozenset({"type", "description", "properties", "required", "items"})
TARGETS: Final = (GATEWAY_TARGET, KG_GATEWAY_TARGET)
DEFAULT_OUTPUT: Final = Path(__file__).resolve().parent / "terraform" / "orion-infra" / "vlab-tools.json"


class SchemaError(ValueError):
    """The Input model's JSON Schema has a shape the Gateway subset cannot express."""


def _deref(node: JsonObject, defs: Mapping[str, object]) -> JsonObject:
    ref = node.get("$ref")
    if ref is None:
        return node
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        raise SchemaError(f"unsupported reference {ref!r}")
    target = defs.get(ref.removeprefix("#/$defs/"))
    if not isinstance(target, dict):
        raise SchemaError(f"unknown reference {ref!r}")
    merged = {**cast(JsonObject, target), **{k: v for k, v in node.items() if k != "$ref"}}
    return _deref(merged, defs)


def _split_nullable(node: JsonObject, defs: Mapping[str, object]) -> tuple[JsonObject, bool]:
    """The node without its null branch, and whether it had one."""
    for key in ("anyOf", "oneOf"):
        branches = node.get(key)
        if branches is None:
            continue
        if not isinstance(branches, list):
            raise SchemaError(f"{key} is not a list")
        options = [_deref(cast(JsonObject, b), defs) for b in branches]
        others = [b for b in options if b.get("type") != "null"]
        if len(others) != 1:
            raise SchemaError(f"{key} with {len(others)} non-null branches cannot be expressed")
        rest = {k: v for k, v in node.items() if k != key}
        return {**others[0], **rest}, len(others) != len(options)
    return node, False


def _describe(node: JsonObject) -> str | None:
    text = node.get("description")
    description = text.strip() if isinstance(text, str) else ""
    values: list[object] | None = None
    if "const" in node:
        values = [node["const"]]
    elif isinstance(node.get("enum"), list):
        values = cast(list[object], node["enum"])
    if values is not None:
        allowed = ", ".join(json.dumps(v) for v in values)
        suffix = f"One of: {allowed}." if len(values) > 1 else f"Always {allowed}."
        description = f"{description} {suffix}".strip()
    return description or None


def _json_type(node: JsonObject) -> str:
    declared = node.get("type")
    if isinstance(declared, str) and declared in GATEWAY_TYPES:
        return declared
    for key in ("const", "enum"):
        sample = node.get(key)
        if key == "enum" and isinstance(sample, list) and sample:
            sample = sample[0]
        if isinstance(sample, bool):
            return "boolean"
        if isinstance(sample, int):
            return "integer"
        if isinstance(sample, float):
            return "number"
        if isinstance(sample, str):
            return "string"
    if declared is None and ("properties" in node or "additionalProperties" in node):
        return "object"
    raise SchemaError(f"no Gateway type for {json.dumps(node, sort_keys=True)[:120]}")


def convert(node: JsonObject, defs: Mapping[str, object]) -> JsonObject:
    """One JSON Schema node as a Gateway schema node."""
    node, _ = _split_nullable(_deref(node, defs), defs)
    kind = _json_type(node)
    out: JsonObject = {"type": kind}
    description = _describe(node)
    if description is not None:
        out["description"] = description
    if kind == "object":
        properties = node.get("properties")
        if isinstance(properties, dict) and properties:
            props: JsonObject = {}
            required: list[str] = []
            listed = node.get("required")
            listed_names = set(cast(list[str], listed)) if isinstance(listed, list) else set()
            for name, child in cast(JsonObject, properties).items():
                resolved, nullable = _split_nullable(_deref(cast(JsonObject, child), defs), defs)
                props[name] = convert(resolved, defs)
                if name in listed_names and not nullable:
                    required.append(name)
            out["properties"] = props
            if required:
                out["required"] = required
    elif kind == "array":
        items = node.get("items")
        if not isinstance(items, dict):
            raise SchemaError("an array without one items schema cannot be expressed")
        out["items"] = convert(cast(JsonObject, items), defs)
    return out


def required_nullable(schema: JsonObject) -> list[str]:
    """Dotted paths of properties the Input requires that may be null (Gateway shows them optional)."""
    defs = cast(Mapping[str, object], schema.get("$defs", {}))
    found: list[str] = []

    def walk(node: JsonObject, path: str) -> None:
        node, _ = _split_nullable(_deref(node, defs), defs)
        properties = node.get("properties")
        listed = node.get("required")
        names = set(cast(list[str], listed)) if isinstance(listed, list) else set()
        if isinstance(properties, dict):
            for name, child in cast(JsonObject, properties).items():
                resolved, nullable = _split_nullable(_deref(cast(JsonObject, child), defs), defs)
                if nullable and name in names:
                    found.append(f"{path}{name}")
                walk(resolved, f"{path}{name}.")
        items = node.get("items")
        if isinstance(items, dict):
            walk(cast(JsonObject, items), f"{path}[].")

    walk(schema, "")
    return found


def gateway_tools(target: str = GATEWAY_TARGET) -> list[JsonObject]:
    """The target's tool definitions, in catalog order: `{name, description, inputSchema}`.

    `name` is the bare tool name; Gateway exposes it as `<target>___<name>` (base.GATEWAY_TOOL_SEPARATOR).
    """
    if target not in TARGETS:
        raise ValueError(f"unknown Gateway target {target!r}")
    tools: list[JsonObject] = []
    for name, spec in TOOL_REGISTRY.items():
        if spec.kind != "server" or spec.gateway_target != target:
            continue
        schema = cast(JsonObject, spec.input_model.model_json_schema(by_alias=True))
        defs = cast(Mapping[str, object], schema.get("$defs", {}))
        input_schema = convert(schema, defs)
        if input_schema["type"] != "object":
            raise SchemaError(f"{name}: an MCP inputSchema is an object")
        input_schema.setdefault("properties", {})
        tools.append({"name": name, "description": f"{spec.summary}. {spec.version}, read-only.", "inputSchema": input_schema})
    return tools


def render(tools: Sequence[JsonObject]) -> str:
    """The file Terraform uploads: stable key order, two-space indent, trailing newline."""
    return json.dumps(list(tools), indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", default=GATEWAY_TARGET, choices=TARGETS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", type=Path, metavar="PATH", help="write the schema file")
    mode.add_argument("--check", type=Path, metavar="PATH", help="exit 1 if the file differs")
    args = parser.parse_args(argv)
    text = render(gateway_tools(args.target))
    if args.write is not None:
        args.write.write_text(text, encoding="utf-8")
    elif args.check is not None:
        current = args.check.read_text(encoding="utf-8") if args.check.exists() else ""
        if current != text:
            print(f"{args.check} is stale; rerun with --write", file=sys.stderr)
            return 1
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
