"""JSON schemas for structured output, within what Bedrock's `output_config.format` compiles.

Structured output takes `type`, `properties`, `required`, `items`, `enum`, `const`, `anyOf`,
`allOf`, `$ref`/`$defs`, `description` and `additionalProperties: false` on every object. It
takes no recursion and no numeric, length or array-size constraints. The Pydantic schemas of the
IR and of ComposerOutput carry those constraints (and `oneOf` with a `discriminator` for tagged
unions), so this module sends a reduced copy and the roles validate the reply against the full
Pydantic model in code, which is where every stripped constraint is enforced. `ir.py` and
`interfaces.py` stay unchanged.

The API also caps the grammar it compiles from a schema. Measured on Bedrock
(us.anthropic.claude-opus-5-5, us-east-1, 2026-11-05): the IR schema as Pydantic writes it is
refused with "The compiled grammar is too large", and so is every variant that keeps its tagged
unions. Each `$defs` entry is compiled on its own with its references expanded (an unreferenced
one included); a union of objects costs far more than the same properties in one object (an array
of 8 kinds with 8 strings each is refused at 3.5 KB, one object with 60 strings compiles); and more
than 24 optional properties is a 400 of its own. So every schema goes out as a *wire encoding*
(`WireSchema`):

- every `$ref` is expanded in place and `$defs` is dropped;
- a union of objects that each carry a `kind` constant becomes one object: `kind` is the enum of
  the constants, and the other properties are those of every kind;
- every property is required. One that a kind leaves unset, or does not have, takes an empty value:
  `""` for a string or a choice (`""` joins the enum), `0` for an integer whose minimum is 1 or
  more, `[]` for a list, and for an object a list of at most one object;
- `X | None` on an optional property becomes `X` (the empty value stands for None), and an
  `integer` branch beside a `number` branch is dropped (number takes integers).

A `WirePolicy` departs from the last three where the grammar can afford it: it keeps named small
kind-tagged unions as an `anyOf` (each kind one object with only its own properties) and sends
named properties as optional (left out when not given). Every empty value is an output token, and
planner latency is about 1.5 s plus 10 ms per output token, so the planner's PLANNER_POLICY keeps
J3's plan near 250 tokens instead of 400 (NFR-01, measured below).

`WireSchema.decode` turns a reply back into the Pydantic model's shape before validation: an empty
value is removed where the reply's kind leaves that property optional or does not have it, and a
one-object list becomes the object. Anything else is passed on as sent, so a property of another
kind, or a list of two objects, still fails the full validation (the models forbid extra fields).
`WireSchema.encode` does the reverse for the prompt's examples. `grammar_profile` counts what the
documented limits count; `wire_schema` checks that the policy's optional properties and unions are
the only ones, and the tests pin both schemas' profiles to what Bedrock compiled.
"""

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Final, Literal

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
# How a property that is not given is sent: "" (string or choice), 0 (integer), [] (list), [object] (object).
Empty = Literal["string", "zero", "list", "one"]


class SchemaError(ValueError):
    """A schema structured output cannot express (recursion, an open object)."""


def _merge_numbers(branches: list[JsonValue]) -> list[JsonValue]:
    """`integer` inside an anyOf that also has `number` adds a branch but no value: number takes integers."""
    if {"type": "number"} in branches and {"type": "integer"} in branches:
        return [b for b in branches if b != {"type": "integer"}]
    return branches


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
    if isinstance(branches := out.get("anyOf"), list):
        merged = _merge_numbers(branches)
        if len(merged) == 1 and isinstance(merged[0], dict):
            out = {**{k: v for k, v in out.items() if k != "anyOf"}, **merged[0]}
        else:
            out["anyOf"] = merged
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


# --- The wire encoding -----------------------------------------------------------------------------

_EMPTY_VALUES: Final[dict[Empty, JsonValue]] = {"string": "", "zero": 0, "list": [], "one": []}
# The documented grammar limits (Anthropic structured outputs; Bedrock returns a 400 past them).
MAX_OPTIONAL: Final = 24
MAX_UNION_PROPERTIES: Final = 16


@dataclass(frozen=True)
class WirePolicy:
    """Where a wire encoding departs from "flatten every union, require every property".

    Paths are dotted property names from the schema's top level, `[]` for a list's items:
    `output.analyses[]`, `output.cohorts[].def.gene`.

    - `unions`: kind-tagged unions sent as an `anyOf` of one object per kind, each with only its
      own properties. Compact, but a union of objects costs far more grammar than one object, so
      only small ones are kept (measured: the cohort definition's seven kinds are refused).
    - `optional`: properties sent as optional, so a reply leaves them out instead of writing an
      empty value. Each one costs grammar too (24 at most; 21 took about 25 s to compile).
    """

    unions: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Leaf:
    """A value sent as it is (a string, a number, a choice)."""


@dataclass(frozen=True)
class ListCodec:
    items: "Codec"


@dataclass(frozen=True)
class Prop:
    empty: Empty | None  # how "not given" is sent (or, for an optional one, recognised); None for a property every kind requires
    value: "Codec"  # the value's codec (the item's, for "one")
    optional: bool = False  # optional on the wire: left out when not given


@dataclass(frozen=True)
class ObjectCodec:
    props: Mapping[str, Prop]
    # A flattened union: each kind's own properties, and whether that kind requires each.
    # None: one object type, whose properties are all its own.
    kinds: Mapping[str, Mapping[str, bool]] | None = None


@dataclass(frozen=True)
class UnionCodec:
    """A kind-tagged union sent as an anyOf: one object codec per kind."""

    branches: Mapping[str, ObjectCodec]


Codec = Leaf | ListCodec | ObjectCodec | UnionCodec


@dataclass(frozen=True)
class WireSchema:
    """What is sent (`schema`) and how a reply maps back to the Pydantic model's shape (`codec`)."""

    schema: dict[str, JsonValue]
    codec: Codec

    def decode(self, data: JsonValue) -> JsonValue:
        """A reply in the wire encoding, in the model's own shape: empty values removed, one-object lists unwrapped."""
        return _decode(self.codec, data)

    def encode(self, data: JsonValue) -> JsonValue:
        """Data in the model's shape, in the wire encoding (for the prompt's examples)."""
        return _encode(self.codec, data)


def _is_empty(empty: Empty, value: JsonValue) -> bool:
    if empty == "zero":
        return isinstance(value, int) and not isinstance(value, bool) and value == 0
    return value == _EMPTY_VALUES[empty]


def _decode(codec: Codec, data: JsonValue) -> JsonValue:
    if isinstance(codec, ListCodec):
        return [_decode(codec.items, item) for item in data] if isinstance(data, list) else data
    if isinstance(codec, Leaf) or not isinstance(data, dict):
        return data
    if isinstance(codec, UnionCodec):
        kind = data.get("kind")
        branch = codec.branches.get(kind) if isinstance(kind, str) else None
        return data if branch is None else _decode(branch, data)  # an unknown kind: validation reports it
    if codec.kinds is None:
        members: Mapping[str, bool] = {key: prop.empty is None for key, prop in codec.props.items()}
    else:
        kind = data.get("kind")
        found = codec.kinds.get(kind) if isinstance(kind, str) else None
        if found is None:
            return data  # an unknown kind: validation reports it
        members = found
    out: dict[str, JsonValue] = {}
    for key, value in data.items():
        prop = codec.props.get(key)
        if prop is None:
            out[key] = value  # not in the schema: validation reports it
            continue
        if prop.empty is not None and not members.get(key, False) and _is_empty(prop.empty, value):
            continue  # not stated, or not this kind's property
        if prop.empty == "one" and not prop.optional and isinstance(value, list) and len(value) <= 1:
            if value:
                out[key] = _decode(prop.value, value[0])
            continue
        # Anything else is passed on as sent (a property of another kind, a list of two objects),
        # so the full validation reports it.
        out[key] = value if prop.empty == "one" and not prop.optional else _decode(prop.value, value)
    return out


def _encode(codec: Codec, data: JsonValue) -> JsonValue:
    if isinstance(codec, ListCodec):
        return [_encode(codec.items, item) for item in data] if isinstance(data, list) else data
    if isinstance(codec, Leaf) or not isinstance(data, dict):
        return data
    if isinstance(codec, UnionCodec):
        kind = data.get("kind")
        branch = codec.branches.get(kind) if isinstance(kind, str) else None
        return data if branch is None else _encode(branch, data)
    out: dict[str, JsonValue] = {}
    for key, prop in codec.props.items():
        if key in data:
            value = _encode(prop.value, data[key])
            out[key] = [value] if prop.empty == "one" and not prop.optional else value
        elif prop.empty is not None and not prop.optional:
            out[key] = copy.deepcopy(_EMPTY_VALUES[prop.empty])
    out.update({key: value for key, value in data.items() if key not in codec.props})
    return out


def _ref_name(node: Mapping[str, JsonValue]) -> str | None:
    ref = node.get("$ref")
    return ref.removeprefix("#/$defs/") if isinstance(ref, str) and ref.startswith("#/$defs/") else None


def _resolve(node: JsonValue, defs: Mapping[str, JsonValue]) -> JsonValue:
    """`node` with its `$ref` replaced by the definition (sibling keywords kept)."""
    if not isinstance(node, dict) or (name := _ref_name(node)) is None:
        return node
    body = _resolve(defs[name], defs)
    rest = {key: value for key, value in node.items() if key != "$ref"}
    return {**body, **rest} if isinstance(body, dict) else body


def _without_null(node: JsonValue) -> JsonValue:
    """`X | None` as `X`; any other schema unchanged."""
    if not isinstance(node, dict) or not isinstance(branches := node.get("anyOf"), list):
        return node
    kept = [b for b in branches if b != {"type": "null"}]
    if len(kept) == len(branches) or not kept:
        return node
    if len(kept) > 1:
        return {**node, "anyOf": kept}
    single = kept[0]
    if not isinstance(single, dict):
        return node
    return {**{k: v for k, v in node.items() if k != "anyOf"}, **single}


def _minimum(node: Mapping[str, JsonValue]) -> float | None:
    low = node.get("minimum")
    if isinstance(low, (int, float)) and not isinstance(low, bool):
        return float(low)
    above = node.get("exclusiveMinimum")
    if isinstance(above, (int, float)) and not isinstance(above, bool):
        return float(above) + 1
    return None


def _kind_const(node: JsonValue) -> str | None:
    if not isinstance(node, dict) or node.get("type") != "object" or not isinstance(props := node.get("properties"), dict):
        return None
    kind = props.get("kind")
    const = kind.get("const") if isinstance(kind, dict) else None
    return const if isinstance(const, str) else None


def _child(where: str, name: str) -> str:
    return f"{where}.{name}" if where else name


@dataclass
class _Field:
    raw: dict[str, JsonValue]  # the source schema, constraints included (null branch removed when optional)
    wire: JsonValue  # the encoded schema of the value
    codec: Codec


@dataclass
class _Build:
    """What one wire_schema call builds from: the source's definitions, the policy, the policy paths used."""

    defs: Mapping[str, JsonValue]
    policy: WirePolicy
    used: set[str]


def _build(node: JsonValue, ctx: _Build, path: str, where: str) -> tuple[JsonValue, Codec]:
    node = _resolve(node, ctx.defs)
    if not isinstance(node, dict):
        return node, Leaf()
    if isinstance(branches := node.get("anyOf", node.get("oneOf")), list):
        resolved = [_resolve(b, ctx.defs) for b in branches]
        if all(_kind_const(b) is not None for b in resolved):
            objects = [b for b in resolved if isinstance(b, dict)]
            if where in ctx.policy.unions:
                ctx.used.add(where)
                return _union(objects, ctx, path, where)
            return _flatten(objects, ctx, path, where)
        if any(isinstance(b, dict) and (b.get("type") in ("object", "array") or "properties" in b) for b in resolved):
            raise SchemaError(f"{path} is a union of objects or lists without a kind to tell them apart")
        return node, Leaf()
    if node.get("type") == "array":
        wire, codec = _build(node.get("items"), ctx, f"{path}/items", f"{where}[]")
        return {**node, "items": wire}, ListCodec(codec)
    if node.get("type") == "object" or "properties" in node:
        return _flatten([node], ctx, path, where, single=True)
    return node, Leaf()


def _fields(node: dict[str, JsonValue], ctx: _Build, path: str, where: str) -> tuple[dict[str, _Field], set[str]]:
    props = node.get("properties")
    required = node.get("required", [])
    if not isinstance(props, dict) or not isinstance(required, list):
        raise SchemaError(f"{path} is an object without properties")
    fields: dict[str, _Field] = {}
    for name, child in props.items():
        raw = _resolve(child if name in required else _without_null(child), ctx.defs)
        if not isinstance(raw, dict):
            raise SchemaError(f"{path}/properties/{name} has no schema")
        wire, codec = _build(raw, ctx, f"{path}/properties/{name}", _child(where, name))
        fields[name] = _Field(raw, wire, codec)
    return fields, {str(name) for name in required}


def _empty_kind(field: _Field, where: str) -> tuple[Empty, JsonValue]:
    """How a property that some kind leaves unset is sent, and its encoded schema."""
    wire = field.wire
    kind = wire.get("type") if isinstance(wire, dict) else None
    if kind == "object":
        return "one", {"type": "array", "items": wire}
    if kind == "array":
        return "list", wire
    if kind == "string" and isinstance(wire, dict):
        if isinstance(choices := wire.get("enum"), list):
            return "string", {**wire, "enum": [*[c for c in choices if c != ""], ""]}
        if "const" in wire:
            raise SchemaError(f"{where} is an optional constant")
        return "string", wire
    if kind == "integer":
        return "zero", wire
    raise SchemaError(f"{where} is optional with a type the wire encoding has no empty value for")


def _union(branches: list[dict[str, JsonValue]], ctx: _Build, path: str, where: str) -> tuple[JsonValue, Codec]:
    """A kind-tagged union kept as an anyOf: each kind one object with only its own properties."""
    wires: list[JsonValue] = []
    codecs: dict[str, ObjectCodec] = {}
    for i, branch in enumerate(branches):
        const = _kind_const(branch)
        assert const is not None
        wire, codec = _flatten([branch], ctx, f"{path}/anyOf/{i}", where, single=True)
        assert isinstance(codec, ObjectCodec)
        wires.append(wire)
        codecs[const] = codec
    return {"anyOf": wires}, UnionCodec(codecs)


def _flatten(
    branches: list[dict[str, JsonValue]], ctx: _Build, path: str, where: str, *, single: bool = False
) -> tuple[JsonValue, Codec]:
    """One object for an object type (`single`) or a kind-tagged union: every property required,
    except those the policy sends as optional."""
    parts = [_fields(branch, ctx, f"{path}/anyOf/{i}" if not single else path, where) for i, branch in enumerate(branches)]
    merged: dict[str, _Field] = {}
    owners: dict[str, list[tuple[int, bool]]] = {}
    for i, (fields, required) in enumerate(parts):
        for name, field in fields.items():
            if name == "kind" and not single:
                continue
            seen = merged.get(name)
            if seen is not None and _clean(copy.deepcopy(seen.wire), path) != _clean(copy.deepcopy(field.wire), path):
                raise SchemaError(f"{path}: the kinds give {name!r} different schemas")
            merged.setdefault(name, field)
            owners.setdefault(name, []).append((i, name in required))
    props: dict[str, Prop] = {}
    wire_props: dict[str, JsonValue] = {}
    wire_required: list[str] = []
    if not single:
        kinds = [_kind_const(branch) for branch in branches]
        wire_props["kind"] = {"type": "string", "enum": [k for k in kinds if k is not None]}
        props["kind"] = Prop(None, Leaf())
        wire_required.append("kind")
    for name, field in merged.items():
        at = f"{path}/properties/{name}"
        dotted = _child(where, name)
        if len(owners[name]) == len(branches) and all(req for _, req in owners[name]):
            if dotted in ctx.policy.optional:
                raise SchemaError(f"{dotted} is required by every kind, so it cannot be sent as optional")
            wire_props[name], props[name] = field.wire, Prop(None, field.codec)
            wire_required.append(name)
            continue
        empty, wire = _empty_kind(field, at)
        if empty == "zero":
            for i, req in owners[name]:
                low = _minimum(parts[i][0][name].raw)
                if not req and (low is None or low < 1):
                    raise SchemaError(f"{at} is an optional integer that may be 0, so 0 cannot mean 'not given'")
        if dotted in ctx.policy.optional:
            if empty == "one":
                raise SchemaError(f"{dotted} is an object; the wire encoding sends no optional object")
            ctx.used.add(dotted)
            # The value's own schema: left out when not given, so no empty value joins its choices.
            wire_props[name], props[name] = field.wire, Prop(empty, field.codec, optional=True)
            continue
        wire_props[name], props[name] = wire, Prop(empty, field.codec)
        wire_required.append(name)
    base = branches[0] if single else {}
    keep = {key: value for key, value in base.items() if key in ("description",)}
    wire_node: dict[str, JsonValue] = {
        **keep, "type": "object", "properties": wire_props, "required": list(wire_required), "additionalProperties": False,
    }
    if single:
        return wire_node, ObjectCodec(props)
    kind_map: dict[str, dict[str, bool]] = {}
    for i, branch in enumerate(branches):
        const = _kind_const(branch)
        assert const is not None
        fields, required = parts[i]
        kind_map[const] = {"kind": True, **{name: name in required for name in fields if name != "kind"}}
    return wire_node, ObjectCodec(props, kind_map)


def grammar_profile(schema: JsonValue) -> dict[str, int]:
    """What the documented limits count: optional properties, properties with a union, and unions of objects."""
    counts = {"optional": 0, "unions": 0, "object_unions": 0, "refs": 0}

    def visit(node: JsonValue) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            counts["refs"] += 1
        branches = node.get("anyOf")
        if isinstance(branches, list) or isinstance(node.get("type"), list):
            counts["unions"] += 1
            if isinstance(branches, list) and any(isinstance(b, dict) and (b.get("type") == "object" or "$ref" in b) for b in branches):
                counts["object_unions"] += 1
        props, required = node.get("properties"), node.get("required", [])
        if isinstance(props, dict) and isinstance(required, list):
            counts["optional"] += len(set(props) - {name for name in required if isinstance(name, str)})
            for child in props.values():
                visit(child)
        for key, value in node.items():
            if key != "properties":
                visit(value)

    visit(schema)
    return counts


def wire_schema(schema: Mapping[str, JsonValue], policy: WirePolicy = WirePolicy()) -> WireSchema:
    """The wire encoding of a Pydantic JSON schema (module docstring); raises SchemaError when none exists."""
    source = copy.deepcopy(dict(schema))
    _check_acyclic(source)
    raw_defs = source.pop("$defs", {})
    ctx = _Build(raw_defs if isinstance(raw_defs, dict) else {}, policy, set())
    wire, codec = _build(source, ctx, "#", "")
    unused = (policy.unions | policy.optional) - ctx.used
    if unused:
        raise SchemaError(f"the policy names {sorted(unused)}, which the schema has no union or optional property at")
    cleaned = _clean(wire, "#")
    if not isinstance(cleaned, dict) or cleaned.get("type") != "object":
        raise SchemaError("the top level of a structured output schema is an object")
    profile = grammar_profile(cleaned)
    expected = {"optional": len(policy.optional), "object_unions": len(policy.unions), "refs": 0}
    if any(profile[key] != value for key, value in expected.items()):
        raise SchemaError(f"the wire encoding left {profile}; the policy allows {expected}")
    if profile["optional"] > MAX_OPTIONAL or profile["unions"] > MAX_UNION_PROPERTIES:
        raise SchemaError(f"the wire encoding exceeds the grammar limits: {profile}")
    return WireSchema(schema=cleaned, codec=codec)


def _planner_source() -> dict[str, JsonValue]:
    union = dict(ir.planner_output_json_schema())
    defs = union.pop("$defs", {})
    return {
        "type": "object",
        "properties": {PLANNER_OUTPUT_KEY: union},
        "required": [PLANNER_OUTPUT_KEY],
        "additionalProperties": False,
        "$defs": defs,
    }


# The planner's policy, measured on Bedrock (us.anthropic.claude-opus-5-5, us-east-1, 2026-11-05):
# PlannerOutput's three kinds and an analysis's four stay unions, a cohort definition's seven kinds
# are flattened with their properties optional, and so is a cohort's name: eight optional
# properties and two unions of objects compile (about 12 s on first use, then cached), and J3's
# plan shrinks from about 400 output tokens to about 250 (planner latency is about 1.5 s plus
# 10 ms per output token, NFR-01). The cohort definition kept as a union is refused as too large.
PLANNER_POLICY: Final = WirePolicy(
    unions=frozenset({PLANNER_OUTPUT_KEY, f"{PLANNER_OUTPUT_KEY}.analyses[]"}),
    optional=frozenset(
        {f"{PLANNER_OUTPUT_KEY}.cohorts[].name"}
        | {f"{PLANNER_OUTPUT_KEY}.cohorts[].def.{name}" for name in ("clauses", "gene", "within", "a", "b", "cohort_name", "ledger_ref")}
    ),
)


@cache
def planner_wire() -> WireSchema:
    """`{"output": PlannerOutput}` in the wire encoding, under PLANNER_POLICY."""
    return wire_schema(_planner_source(), PLANNER_POLICY)


@cache
def composer_wire() -> WireSchema:
    return wire_schema(ComposerOutput.model_json_schema(mode="validation"))


def planner_output_schema() -> dict[str, JsonValue]:
    """The planner's schema as sent (a copy: callers may not change the cached one)."""
    return copy.deepcopy(planner_wire().schema)


def composer_output_schema() -> dict[str, JsonValue]:
    """The composer's schema as sent. ComposerOutput has no union and no optional property, so its
    reply needs no decoding; only its definitions are expanded in place."""
    return copy.deepcopy(composer_wire().schema)
