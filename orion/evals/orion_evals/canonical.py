"""The contract's canonical JSON and the hashes built on it, written independently of the agent.

Contract sections: "Canonical JSON (every hash)", "Cohort definitions and `defSha256`",
"Pinned inputs" and "Mode H fingerprint". `tests/test_canonical.py` reproduces every vector in
`orion/contracts/fixtures/cohort-def-vectors.json`, `viz-pinned-vectors.json` and
`fingerprint-vectors.json`.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

COHORT_DEF_SCHEMA: Final = "orion.cohort-def/v1"
VIZ_PINNED_SCHEMA: Final = "orion.viz-pinned/v1"
FINGERPRINT_SCHEMA: Final = "orion.fixture-fingerprint/v1"
MAX_SAFE_INTEGER: Final = 2**53 - 1
MAX_CASE_IDS: Final = 10_000
MAX_CLAUSES: Final = 20
COMPARISON_OPS: Final = frozenset({"!=", ">", ">=", "<", "<="})
CASE_ID_FIELDS: Final = frozenset({"case_id", "cases.case_id"})


class PortableNumberError(ValueError):
    """A number outside the range JavaScript and Python print alike."""


def _number(value: int | float, portable: bool) -> str:
    if isinstance(value, bool):
        raise TypeError("booleans are not numbers here")
    if isinstance(value, int):
        if portable and abs(value) > MAX_SAFE_INTEGER:
            raise PortableNumberError(f"{value} is beyond 2^53 - 1")
        return str(value)
    if not math.isfinite(value):
        raise PortableNumberError(f"{value!r} is not finite")
    if value == int(value) and abs(value) <= MAX_SAFE_INTEGER:
        return str(int(value))
    if portable and not 1e-4 <= abs(value) < 1e15:
        raise PortableNumberError(f"{value!r} is outside 1e-4 <= |x| < 1e15")
    # In the portable range Python's shortest repr equals JavaScript's Number#toString.
    return repr(value)


def canonical_json(value: Any, *, portable: bool = True) -> str:
    """Keys sorted by code point at every level, arrays in order, no whitespace, non-ASCII kept."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _number(value, portable)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: str(item[0]))
        return "{" + ",".join(f"{json.dumps(str(k), ensure_ascii=False)}:{canonical_json(v, portable=portable)}" for k, v in items) + "}"
    if isinstance(value, Sequence):
        return "[" + ",".join(canonical_json(item, portable=portable) for item in value) + "]"
    raise TypeError(f"{type(value).__name__} is not JSON")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Cohort definitions -----------------------------------------------------------------


def _sorted_values(values: Sequence[Any]) -> list[Any]:
    unique: list[Any] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if all(isinstance(v, str) for v in unique):
        return sorted(unique)
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in unique):
        return sorted(unique)
    raise ValueError("a clause's values are all strings or all numbers")


def normalize_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The contract's normalization: dedupe and sort ids or clause values, sort clauses, case-ID rule."""
    kind = definition.get("kind")
    if kind == "case_ids":
        ids = definition.get("caseIds")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("case_ids carries a list of string ids")
        unique = sorted(set(ids))
        if len(unique) > MAX_CASE_IDS:
            raise ValueError("more than 10,000 case ids")
        return {"kind": "case_ids", "caseIds": unique}
    if kind != "filter":
        raise ValueError(f"unknown definition kind {kind!r}")
    raw = definition.get("clauses")
    if not isinstance(raw, list):
        raise ValueError("filter carries a list of clauses")
    clauses: list[dict[str, Any]] = []
    for clause in raw:
        field, op, values = clause.get("field"), clause.get("op"), clause.get("values")
        if not isinstance(field, str) or not field or not isinstance(values, list) or not values:
            raise ValueError("a clause has a field and values")
        if op != "in" and op not in COMPARISON_OPS:
            raise ValueError(f"unknown operator {op!r}")
        normalized = {"field": field, "op": op, "values": _sorted_values(values)}
        if op != "in" and len(normalized["values"]) != 1:
            raise ValueError("a comparison carries exactly one value")
        if normalized not in clauses:
            clauses.append(normalized)
    if len(clauses) > MAX_CLAUSES:
        raise ValueError("more than 20 clauses")
    clauses.sort(key=lambda c: (c["field"], c["op"], canonical_json(c["values"])))
    if len(clauses) == 1 and clauses[0]["op"] == "in" and clauses[0]["field"] in CASE_ID_FIELDS:
        values = clauses[0]["values"]
        if all(isinstance(v, str) for v in values):
            return normalize_definition({"kind": "case_ids", "caseIds": values})
    return {"kind": "filter", "clauses": clauses}


def definition_canonical(definition: Mapping[str, Any]) -> str:
    return canonical_json({"schema": COHORT_DEF_SCHEMA, **normalize_definition(definition)})


def def_sha256(definition: Mapping[str, Any]) -> str:
    """defSha256 of a canonical cohort definition."""
    return sha256_hex(definition_canonical(definition))


class _Unreadable(Exception):
    pass


def _scalar(operand: Any) -> str | int | float:
    if isinstance(operand, Mapping):
        legacy = operand.get("cases.case_id")
        if len(operand) == 1 and isinstance(legacy, str):
            return legacy
        raise _Unreadable("non-scalar operand")
    if isinstance(operand, bool) or not isinstance(operand, (str, int, float)):
        raise _Unreadable("non-scalar operand")
    scalar: str | int | float = operand
    return scalar


def _clauses_of(key: str, operation: Any) -> list[dict[str, Any]]:
    if not isinstance(operation, Mapping):
        raise _Unreadable("an operation is an object")
    operator = operation.get("operator")
    field = operation.get("field", key)
    if not isinstance(field, str):
        raise _Unreadable("a field is a string")
    if operator in ("in", "includes"):
        operands = operation.get("operands")
        if not isinstance(operands, list) or not operands:
            raise _Unreadable("in carries operands")
        return [{"field": field, "op": "in", "values": [_scalar(o) for o in operands]}]
    if operator == "=":
        return [{"field": field, "op": "in", "values": [_scalar(operation.get("operand"))]}]
    if operator in COMPARISON_OPS:
        return [{"field": field, "op": operator, "values": [_scalar(operation.get("operand"))]}]
    if operator == "and":
        operands = operation.get("operands")
        if not isinstance(operands, list):
            raise _Unreadable("and carries operands")
        return [clause for item in operands for clause in _clauses_of(key, item)]
    raise _Unreadable(f"operator {operator!r}")


def definition_from_filterset(filter_set: Mapping[str, Any]) -> dict[str, Any] | None:
    """The canonical definition of a saved cohort's FilterSet, or None when it cannot be read."""
    try:
        root = filter_set.get("root")
        mode = filter_set.get("mode")
        if not isinstance(root, Mapping) or mode not in ("and", "or"):
            raise _Unreadable("a FilterSet has a mode and a root")
        if mode == "or" and len(root) > 1:
            raise _Unreadable("or over more than one key")
        clauses = [clause for key, op in root.items() for clause in _clauses_of(str(key), op)]
        if len(clauses) > MAX_CLAUSES:
            raise _Unreadable("more than 20 clauses")
        definition = normalize_definition({"kind": "filter", "clauses": clauses})
        canonical_json(definition)  # the portable number range
        return definition
    except (_Unreadable, ValueError, TypeError):
        return None


def filterset_def_sha256(filter_set: Mapping[str, Any]) -> str | None:
    definition = definition_from_filterset(filter_set)
    return None if definition is None else def_sha256(definition)


def filterset_from_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The FilterSet the browser writes for a definition (contract "On Create")."""
    normalized = normalize_definition(definition)
    if normalized["kind"] == "case_ids":
        ids = normalized["caseIds"]
        return {"mode": "and", "root": {"case_id": {"operator": "in", "field": "case_id", "operands": ids}}}
    by_field: dict[str, list[dict[str, Any]]] = {}
    for clause in normalized["clauses"]:
        if clause["op"] == "in":
            operation: dict[str, Any] = {"operator": "includes", "field": clause["field"], "operands": clause["values"]}
        else:
            operation = {"operator": clause["op"], "field": clause["field"], "operand": clause["values"][0]}
        by_field.setdefault(clause["field"], []).append(operation)
    root: dict[str, Any] = {}
    for field, operations in by_field.items():
        root[field] = operations[0] if len(operations) == 1 else {"operator": "and", "operands": operations}
    return {"mode": "and", "root": root}


# --- Pinned viz inputs and the fixture fingerprint ---------------------------------------------


def pinned_hash(pinned: Mapping[str, Any]) -> str:
    """pinnedHash: deDirect sample ids sorted ascending, every other array kept in order."""
    body = dict(pinned)
    if body.get("kind") == "deDirect":
        body["groups"] = [{**group, "sampleIds": sorted(group["sampleIds"])} for group in body["groups"]]
    return sha256_hex(canonical_json({"schema": VIZ_PINNED_SCHEMA, **body}))


def fingerprint_document(
    status: Mapping[str, Any], version: Mapping[str, Any], healthcheck: Mapping[str, Any], ranking: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The four-source document behind fingerprintSha256; volatile fields are left out."""
    info = healthcheck.get("versionInfo")
    if not isinstance(info, Mapping):
        raise ValueError("the ProteinPaint healthcheck carries versionInfo")
    return {
        "schema": FINGERPRINT_SCHEMA,
        "sandbox": {"status": status["status"], "synthetic": status["synthetic"], "cases": status["cases"]},
        "analysis": {"version": version["version"]},
        "proteinpaint": {"pkgver": info["pkgver"], "codedate": info["codedate"]},
        "ranking": [{"gene": row["gene"], "cases": row["cases"]} for row in ranking],
    }


def fingerprint_sha256(document: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json(document))
