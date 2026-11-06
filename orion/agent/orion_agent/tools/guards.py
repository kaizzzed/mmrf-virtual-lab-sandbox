"""Pre-guards a tool can check from its resolved Input, and the executor-side checks around them.

The ProteinPaint bridge (identity gateway, `bridgePayload`) turns a Gen3 `filter0` into a native
sample list by evaluating it against the generated cases. Open question 11 asked which filters
fail there; orion/notes/oq11-pp-bridge-filters.md has the probe table. The answer, encoded here:

- **400 "Invalid sandbox request"**: a field no case record carries, which is every
  Elasticsearch-only field (`available_variation_data`, `gene.*`, `summary.*`) and any unknown
  field; and the operators `excludes` (and anything else outside the bridge's list).
- **Silently different**: a numeric field written with string values (`outcomes.censor_os` in
  ["1"]) matches no case in the bridge while Gen3 matches 599; `not` and `exclude` work in the
  bridge but fail in Gen3 and Guppy, so a cohort using them cannot be counted the same way twice.
- **Safe**: `and` and `or` at any depth, `in`, `=`, `!=`, `>`, `>=`, `<`, `<=` on fields the case
  records carry, with values typed as stored, an unwrapped clause, and case-ID lists of any size
  up to 10,000 ids, as `case_id` or `cases.case_id`, plain or as ProteinPaint's legacy
  `{"cases.case_id": id}` operands.

So `filter0_bridge_safe` (the tools' own re-check, which also runs in the Gateway Lambda, where no
vocabulary is loaded) allows null, `{"and": []}`, the safe operators and any field that is not
Elasticsearch-only, and every case-ID clause. The executor's `bridge_safe_with_fields` adds what
needs the vocabulary: the field must be known, not `es_only`, and its values typed as stored.
Under C-23 ORION hands ProteinPaint only case-ID lists, so both checks pass every compiled plan;
they stop a browser filter or a compiler bug from reaching the bridge as a bare 400 or a silent zero.
The DE tools send no cohort filter at all (sample-id lists only; de_preanalysis keeps the golden's
fixed all-case `filter0` and de_run sends null), so their guards are the sample limits.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import JsonValue

from ..models import Code, canonical_json
from .base import MAX_DE_SAMPLES, MAX_SAMPLE_LIST_BODY_BYTES, MIN_DE_ELIGIBLE, MIN_SURVIVAL_GROUP

LOGICAL_OPS: Final = frozenset({"and", "or"})
LEAF_OPS: Final = frozenset({"in", "=", "!=", ">", ">=", "<", "<="})
# Operators the bridge evaluates but Gen3 and Guppy reject: a cohort using them has two meanings.
BRIDGE_ONLY_OPS: Final = frozenset({"not", "exclude"})
CASE_ID_FIELDS: Final = frozenset({"case_id", "cases.case_id"})
# Fields only the Elasticsearch index has; no generated case record carries them, so the bridge
# answers 400 (identity/cohorts.mjs `matches`, probed in OQ11).
ES_ONLY_FIELDS: Final = frozenset({"available_variation_data"})
ES_ONLY_PREFIXES: Final = ("gene.", "summary.")
NUMERIC_FIELD_TYPES: Final = frozenset({"age", "age_in_years", "days", "year", "numeric_range"})
_CASE_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


@dataclass(frozen=True)
class GuardViolation:
    """A failed guard: the rule (schemas.GuardSpec.guard.rule), its limitation code and why."""

    rule: str
    code: Code
    message: str
    field: str | None = None  # the field a bridge failure names (template filter.not_bridge_safe)
    group: str | None = None  # the group a size failure names


def is_es_only(field: str) -> bool:
    bare = field.removeprefix("cases.")
    return bare in ES_ONLY_FIELDS or bare.startswith(ES_ONLY_PREFIXES)


def _unsafe(field: str | None, message: str) -> GuardViolation:
    return GuardViolation("filter0_bridge_safe", "UNSUPPORTED_FIELD", message, field=field)


def _case_id_operand(value: JsonValue) -> bool:
    if isinstance(value, str):
        return bool(_CASE_ID.match(value))
    # ProteinPaint's legacy saved-cohort form: {"cases.case_id": "<id>"}.
    if isinstance(value, dict) and len(value) == 1 and isinstance(value.get("cases.case_id"), str):
        return bool(_CASE_ID.match(str(value["cases.case_id"])))
    return False


def _walk(expression: JsonValue, leaves: list[tuple[str, str, JsonValue]]) -> GuardViolation | None:
    if not isinstance(expression, dict) or len(expression) != 1:
        return _unsafe(None, "each filter node is an object with one operator")
    (operator, operands), = expression.items()
    op = operator.lower()
    if op in LOGICAL_OPS:
        if not isinstance(operands, list):
            return _unsafe(None, f"{operator} takes a list")
        for part in operands:
            problem = _walk(part, leaves)
            if problem is not None:
                return problem
        return None
    if op in BRIDGE_ONLY_OPS:
        return _unsafe(None, f"the {operator} operator works in the ProteinPaint bridge but not in Gen3")
    if op not in LEAF_OPS:
        return _unsafe(None, f"the ProteinPaint bridge does not read the {operator} operator")
    if not isinstance(operands, dict) or not operands:
        return _unsafe(None, f"{operator} takes an object of field to value")
    for field, value in operands.items():
        if op == "in" and not isinstance(value, list):
            return _unsafe(field, f"{field}: in takes a list of values")
        values = value if isinstance(value, list) else [value]
        if field in CASE_ID_FIELDS:
            if op not in ("in", "="):
                return _unsafe(field, f"{field} is filtered only with in")
            if not all(_case_id_operand(v) for v in values):
                return _unsafe(field, f"{field} holds a value that is not a case ID")
        elif is_es_only(field):
            return _unsafe(field, f"{field} is an Elasticsearch-only field the ProteinPaint bridge cannot read")
        elif not all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in values):
            return _unsafe(field, f"{field} holds a value that is not a string or a number")
        leaves.append((op, field, value))
    return None


def filter0_bridge_safe(filter0: JsonValue) -> GuardViolation | None:
    """The tools' structural check of a ProteinPaint filter0 (runs in the Lambda too)."""
    if filter0 is None:
        return None
    return _walk(filter0, [])


class FieldSpecLike(Protocol):
    """What the executor's bridge check reads from vocab.FieldSpec."""

    @property
    def type(self) -> str: ...

    @property
    def es_only(self) -> bool: ...

    @property
    def label(self) -> str: ...

    @property
    def values(self) -> tuple[str | int | float, ...] | None: ...


class FieldLookup(Protocol):
    """vocab.Vocabulary, as the executor holds it."""

    def field(self, field_id: str) -> FieldSpecLike | None: ...


def bridge_safe_with_fields(filter0: JsonValue, fields: FieldLookup) -> GuardViolation | None:
    """The executor's pre-guard (checked_by "executor"): the structural check, then every field
    known to the vocabulary, not es_only, and its values typed as stored (a numeric field written
    as strings matches nothing in the bridge)."""
    if filter0 is None:
        return None
    leaves: list[tuple[str, str, JsonValue]] = []
    problem = _walk(filter0, leaves)
    if problem is not None:
        return problem
    for _, field, value in leaves:
        if field in CASE_ID_FIELDS:
            continue
        spec = fields.field(field)
        if spec is None:
            return _unsafe(field, f"{field} is not a field in the Virtual Lab vocabulary")
        if spec.es_only:
            return _unsafe(spec.label, f"{spec.label} is an Elasticsearch-only field the ProteinPaint bridge cannot read")
        values = value if isinstance(value, list) else [value]
        numeric = spec.type in NUMERIC_FIELD_TYPES or (
            spec.values is not None and bool(spec.values) and all(isinstance(v, (int, float)) for v in spec.values)
        )
        if numeric and not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            return _unsafe(spec.label, f"{spec.label} is numeric; a string value matches no case in the ProteinPaint bridge")
        if not numeric and spec.values is not None and not all(isinstance(v, str) for v in values):
            return _unsafe(spec.label, f"{spec.label} holds text values; a number matches no case in the ProteinPaint bridge")
    return None


def case_lists_nonempty(groups: Sequence[tuple[str, Sequence[str]]]) -> GuardViolation | None:
    """sample_lists pre-guard: both case-ID lists are non-empty."""
    for key, ids in groups:
        if not ids:
            return GuardViolation("case_lists_nonempty", "EMPTY_COHORT", f"the {key} group has no case", group=key)
    return None


def body_within_limit(key: str, payload: JsonValue) -> GuardViolation | None:
    """sample_lists pre-guard: each body stays within the identity gateway's 8 MiB request limit."""
    size = len(canonical_json(payload, portable=False).encode("utf-8"))
    if size > MAX_SAMPLE_LIST_BODY_BYTES:
        return GuardViolation(
            f"body_bytes<={MAX_SAMPLE_LIST_BODY_BYTES}", "REQUEST_TOO_LARGE",
            f"the {key} sample lookup is {size} bytes, over the gateway's {MAX_SAMPLE_LIST_BODY_BYTES}", group=key,
        )
    return None


def de_samples_within_limit(counts: Sequence[int]) -> GuardViolation | None:
    """de_preanalysis and de_run pre-guard: both groups together have at most 4,000 samples."""
    total = sum(counts)
    if total > MAX_DE_SAMPLES:
        return GuardViolation(
            f"samples<={MAX_DE_SAMPLES}", "REQUEST_TOO_LARGE", f"the groups have {total} samples, over the {MAX_DE_SAMPLES} one DE run takes"
        )
    return None


def eligible_enough(counts: Mapping[str, int]) -> GuardViolation | None:
    """de_preanalysis post-guard: each group has at least 10 RNA-seq-eligible samples."""
    for key, count in counts.items():
        if count < MIN_DE_ELIGIBLE:
            return GuardViolation(
                f"eligible>={MIN_DE_ELIGIBLE}_per_group", "INELIGIBLE_GROUP",
                f"the {key} group has {count} RNA-seq-eligible samples, fewer than {MIN_DE_ELIGIBLE}", group=key,
            )
    return None


def executor_pre_guard(
    tool: str, request: JsonValue, *, fields: FieldLookup | None = None, group_counts: Mapping[str, int] | None = None
) -> GuardViolation | None:
    """The pre-guards the executor checks on a resolved request before it calls the tool
    (schemas.GuardSpec.checked_by == "executor"): the bridge check of top_mutated_genes' filter0 (with
    the vocabulary when given) and survival_compare's group sizes (each group label's count fact).
    A violation skips the step with its code, and no call is made. The tools re-check their own."""
    body = request if isinstance(request, dict) else {}
    if tool == "top_mutated_genes":
        filter0 = body.get("filter0")
        return bridge_safe_with_fields(filter0, fields) if fields is not None else filter0_bridge_safe(filter0)
    if tool == "survival_compare" and group_counts is not None:
        return survival_groups_eligible(group_counts)
    return None


def survival_groups_eligible(counts: Mapping[str, int]) -> GuardViolation | None:
    """survival_compare pre-guard (checked_by "executor"): each group's count fact is at least 10,
    so no survival call is made for a small group. `counts` maps group label to its count fact."""
    for label, count in counts.items():
        if count < MIN_SURVIVAL_GROUP:
            return GuardViolation(
                f"group_cases>={MIN_SURVIVAL_GROUP}", "INELIGIBLE_GROUP",
                f"{label} has {count} cases, fewer than {MIN_SURVIVAL_GROUP}", group=label,
            )
    return None
