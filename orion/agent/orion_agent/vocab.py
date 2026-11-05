"""C-VOCAB: the clinical vocabulary every filter is checked against.

Fields and values come from config/cohort-fields.json (read-only, ORION_FIELDS_PATH) plus
vocab_supplement.json for values the config omits. Values are normalized to the stored
spelling, because the APIs match case-sensitively and return 0 cases, not an error, on a miss.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue

from .ir import Clause, FilterOp, Value
from .models import FilterSet

ProblemCode = Literal["UNKNOWN_FIELD", "UNKNOWN_VALUE", "UNSUPPORTED_FIELD"]
Engine = Literal["gen3", "pp_bridge"]

SUPPLEMENT_PATH: Final = Path(__file__).with_name("vocab_supplement.json")
NUMERIC_TYPES: Final = frozenset({"age", "age_in_years", "days", "year", "numeric_range"})
CASE_ID_TYPE: Final = "case_id"
CATEGORICAL_OPS: Final[frozenset[FilterOp]] = frozenset({"in", "=", "!="})
NUMERIC_OPS: Final[frozenset[FilterOp]] = frozenset({"in", "=", "!=", ">", ">=", "<", "<="})
ES_ONLY_PREFIXES: Final = ("gene.", "summary.")
_SINGLE_VALUE_OPS: Final[frozenset[FilterOp]] = frozenset({"=", "!=", ">", ">=", "<", "<="})
_BROWSER_VALUE_OPS: Final[dict[str, FilterOp]] = {"=": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}
_LEGACY_CASE_KEY: Final = "cases.case_id"


@dataclass(frozen=True)
class FieldSpec:
    id: str
    type: str
    values: tuple[Value, ...] | None
    pattern: str | None
    es_only: bool
    label: str
    group: str
    value_prefix: str
    split_assumption: str | None
    pp_twin: str | None


@dataclass(frozen=True)
class VocabProblem:
    code: ProblemCode
    field: str
    text: str


def _humanize(field_id: str) -> str:
    return field_id.rsplit(".", 1)[-1].replace("_", " ").capitalize()


def _fold(value: Value) -> str:
    return " ".join(str(value).split()).casefold()


def _scalar(value: JsonValue) -> Value | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return value


class Vocabulary:
    def __init__(self, fields: dict[str, FieldSpec]) -> None:
        self._fields = fields

    @classmethod
    def load(cls, fields_path: Path, supplement_path: Path = SUPPLEMENT_PATH) -> "Vocabulary":
        config: dict[str, dict[str, JsonValue]] = json.loads(fields_path.read_text(encoding="utf-8"))
        supplement = json.loads(supplement_path.read_text(encoding="utf-8"))
        extra: dict[str, dict[str, JsonValue]] = supplement["fields"]
        labels: dict[str, dict[str, str | None]] = supplement["labels"]
        twins: dict[str, str] = supplement["proteinpaintTwins"]["terms"]
        fields: dict[str, FieldSpec] = {}
        for field_id in sorted(config.keys() | extra.keys()):
            spec = {**config.get(field_id, {}), **extra.get(field_id, {})}
            raw_values = spec.get("values")
            values = (
                tuple(v for v in (_scalar(item) for item in raw_values) if v is not None)
                if isinstance(raw_values, list)
                else None
            )
            label = labels.get(field_id, {})
            pattern = spec.get("pattern")
            fields[field_id] = FieldSpec(
                id=field_id,
                type=str(spec.get("type", "enum")),
                values=values,
                pattern=pattern if isinstance(pattern, str) else None,
                es_only=bool(spec.get("esOnly", False)) or field_id.startswith(ES_ONLY_PREFIXES),
                label=label.get("label") or _humanize(field_id),
                group=label.get("group") or _humanize(field_id).lower(),
                value_prefix=label.get("valuePrefix") or "",
                split_assumption=label.get("splitAssumption"),
                pp_twin=twins.get(field_id),
            )
        return cls(fields)

    def field(self, field_id: str) -> FieldSpec | None:
        return self._fields.get(field_id)

    def normalize_value(self, spec: FieldSpec, value: Value) -> Value | None:
        """The stored spelling (and JSON type) of `value`, or None when the field has no such value."""
        if spec.type in NUMERIC_TYPES:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
            try:
                number = float(value)
            except ValueError:
                return None
            return int(number) if number.is_integer() else number
        if spec.type == CASE_ID_TYPE:
            text = str(value).strip()
            return text if spec.pattern and re.fullmatch(spec.pattern, text) else None
        if spec.values is None:
            return None
        folded = _fold(value)
        return next((stored for stored in spec.values if _fold(stored) == folded), None)

    def check_clause(self, clause: Clause, engine: Engine = "gen3") -> Clause | VocabProblem:
        """Normalize a clause to stored spellings, or say why it cannot run on `engine`."""
        spec = self._fields.get(clause.field)
        if spec is None:
            return VocabProblem("UNKNOWN_FIELD", clause.field, f"{clause.field} is not a field in the Virtual Lab vocabulary.")
        if engine == "pp_bridge" and spec.es_only:
            return VocabProblem(
                "UNSUPPORTED_FIELD", clause.field, f"{spec.label} is not supported in the ProteinPaint cohort bridge."
            )
        allowed = NUMERIC_OPS if spec.type in NUMERIC_TYPES else CATEGORICAL_OPS
        if clause.op not in allowed:
            return VocabProblem("UNSUPPORTED_FIELD", clause.field, f"{spec.label} cannot be filtered with {clause.op}.")
        if clause.op in _SINGLE_VALUE_OPS and len(clause.values) != 1:
            return VocabProblem("UNKNOWN_VALUE", clause.field, f"{spec.label} {clause.op} takes exactly one value.")
        normalized: list[Value] = []
        for value in clause.values:
            stored = self.normalize_value(spec, value)
            if stored is None:
                return VocabProblem("UNKNOWN_VALUE", clause.field, f"{value!s} is not a value of {spec.label}.")
            if stored not in normalized:
                normalized.append(stored)
        return clause.model_copy(update={"values": normalized})


def _operand(value: JsonValue) -> Value | None:
    # ProteinPaint's "Create cohort" stores case ids as {"cases.case_id": id}; the gateway accepts it.
    if isinstance(value, dict) and len(value) == 1 and isinstance(value.get(_LEGACY_CASE_KEY), str):
        return _scalar(value[_LEGACY_CASE_KEY])
    return _scalar(value)


def clauses_from_filter_set(filters: FilterSet) -> tuple[list[Clause], list[VocabProblem]]:
    """Read a browser FilterSet into IR clauses; anything ORION cannot express is a problem, not an error."""
    clauses: list[Clause] = []
    problems: list[VocabProblem] = []
    if filters.mode == "or" and len(filters.root) > 1:
        problems.append(VocabProblem("UNSUPPORTED_FIELD", "*", "Cohort filters joined with OR are not supported."))
        return clauses, problems
    for key, operation in filters.root.items():
        _read_operation(operation, key, clauses, problems)
    return clauses, problems


def _read_operation(
    operation: dict[str, JsonValue], key: str, clauses: list[Clause], problems: list[VocabProblem]
) -> None:
    named = operation.get("field")
    field_id = named if isinstance(named, str) else key
    operator = operation.get("operator")
    operands = operation.get("operands")
    unreadable = VocabProblem("UNKNOWN_VALUE", field_id, f"The filter on {field_id} has values ORION cannot read.")
    raw: list[JsonValue]
    op: FilterOp
    if operator == "and" and isinstance(operands, list) and operands:
        # Gen3 stores a numeric range as an `and` of two comparisons; every part becomes a clause.
        for part in operands:
            if isinstance(part, dict):
                _read_operation(part, field_id, clauses, problems)
            else:
                problems.append(unreadable)
        return
    if operator in ("in", "includes") and isinstance(operands, list):
        op, raw = "in", operands
    elif isinstance(operator, str) and operator in _BROWSER_VALUE_OPS and "operand" in operation:
        op, raw = _BROWSER_VALUE_OPS[operator], [operation["operand"]]
    else:
        problems.append(VocabProblem("UNSUPPORTED_FIELD", field_id, f"The {operator!s} filter on {field_id} is not supported."))
        return
    values = [v for v in (_operand(item) for item in raw) if v is not None]
    if not values or len(values) != len(raw):
        problems.append(unreadable)
        return
    clauses.append(Clause(field=field_id, op=op, values=values))
