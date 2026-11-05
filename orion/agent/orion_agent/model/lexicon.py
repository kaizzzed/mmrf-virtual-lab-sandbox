"""The words the verifier and the prompts share: number words, gene-like tokens, placeholders and
the vocabulary's terms (A2 verifier, V-NUM and V-ENT).

A gene symbol is recognised by its shape (HGNC symbols are upper case: BRAF, JUN, ETV4) after the
allow-listed terms are removed, so ORION never needs the sandbox's 10,000-symbol gene list:
8,659 of those symbols contain a digit anyway (V-NUM's digit rule), and the rest are upper-case
tokens that are not an acronym ORION knows.
"""

import re
from collections.abc import Iterable, Sequence
from typing import Final

from ..catalog import APPS
from ..interfaces import PLACEHOLDER
from ..vocab import FieldSpec, Vocabulary

# V-NUM: "two groups" fails. Ordinals from "third" up; "first" and "second" stay ordinary words.
NUMBER_WORDS: Final = frozenset(
    """zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen
    seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred hundreds thousand
    thousands million millions billion billions trillion dozen dozens half halves twice thrice double doubled
    triple tripled quadruple quarter quarters third thirds fourth fifth sixth seventh eighth ninth tenth eleventh
    twelfth twentieth hundredth thousandth percent percentage""".split()
)
ROMAN_NUMERALS: Final = frozenset({"II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "XIII", "XIV", "XV"})
# Upper-case tokens that are not gene symbols. Vocabulary labels add theirs (CGS, ISS).
ACRONYMS: Final = frozenset(
    {
        "ORION", "DE", "RNA", "DNA", "API", "ID", "IDS", "SSM", "SNV", "CNV", "UMAP", "FDR", "OS", "PFS", "KM",
        "ISS", "CGS", "ECOG", "BMI", "WGS", "WXS", "MM", "AND", "OR", "NOT", "OK", "PP", "SV", "US", "UK",
    }
)
# Terms with digits or number words that ORION's own answers use (V-NUM allow-list, contract).
FIXED_TERMS: Final = (
    "log2 fold change", "log2FC", "log2", "Gen3 Analysis API", "Gen3 Analysis", "Gen3", "six-gene mutation panel",
    "six-gene panel", "six-gene",
)
ORION_SUFFIX: Final = " (ORION)"
_TOKEN: Final = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")
_GENE_SHAPE: Final = re.compile(r"^[A-Z][A-Z0-9]+$")
_DIGIT: Final = re.compile(r"[0-9]")
_INTEGER: Final = re.compile(r"(?<![A-Za-z0-9.])([0-9]+)(?![A-Za-z0-9.])")
_LETTER: Final = re.compile(r"[A-Za-z]")
GENE_FIELDS: Final = frozenset({"gene.symbol", "gene.gene_id"})
_MODEL_NAMES: Final[dict[str, str]] = {
    "anthropic.claude-opus-5-5": "Claude Opus 5.5",
    "anthropic.claude-opus-5": "Claude Opus 5",
    "anthropic.claude-sonnet-5-5": "Claude Sonnet 5.5",
}
# Bedrock inference-profile prefixes: us.anthropic.claude-opus-5-5 is Claude Opus 5.5.
_PROFILE_PREFIX: Final = re.compile(r"^(?:us|eu|apac|jp|au|global)\.(?=anthropic\.)")


def json_list(value: object) -> list[object]:
    """A JSON value as a list: StepSpec.args values are JsonValue, and a missing key is no items."""
    return list(value) if isinstance(value, list) else []


def tokens(text: str) -> list[str]:
    """Word tokens; a hyphenated compound yields each part, so KRAS-mutated yields KRAS."""
    return _TOKEN.findall(text)


def has_digit(text: str) -> bool:
    return _DIGIT.search(text) is not None


def integers(text: str) -> list[int]:
    return [int(m) for m in _INTEGER.findall(text)]


def number_words(text: str) -> list[str]:
    return [token for token in tokens(text) if token.casefold() in NUMBER_WORDS]


def gene_like_tokens(text: str, acronyms: Iterable[str] = ACRONYMS) -> list[str]:
    known = frozenset(acronyms)
    return [t for t in tokens(text) if _GENE_SHAPE.match(t) and t not in known and t not in ROMAN_NUMERALS]


def roman_numerals(text: str) -> list[str]:
    return [t for t in tokens(text) if t in ROMAN_NUMERALS]


def placeholders(text: str) -> list[str]:
    return [m.group(1) for m in PLACEHOLDER.finditer(text)]


def without_placeholders(text: str, marker: str = " ") -> str:
    return PLACEHOLDER.sub(marker, text)


def remove_terms(text: str, terms: Sequence[str]) -> str:
    """`text` with every term removed (case-insensitive, on word boundaries, longest first)."""
    out = text
    for term in sorted({t for t in terms if t.strip()}, key=lambda t: (-len(t), t)):
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE)
        out = pattern.sub(" ", out)
    return out


def cohort_label(name: str) -> str:
    """The name without the ORION suffix, as fact labels and DE titles write it."""
    return name[: -len(ORION_SUFFIX)] if name.endswith(ORION_SUFFIX) else name


def model_display_name(model_id: str) -> str:
    return _MODEL_NAMES.get(_PROFILE_PREFIX.sub("", model_id), model_id)


def vocabulary_fields(vocab: Vocabulary) -> list[FieldSpec]:
    """Every field of the vocabulary, sorted by id.

    Vocabulary exposes `field(id)` only; this reads its field table through `fields()` when a
    later vocab.py offers one, else through the private mapping (integration note: add
    `Vocabulary.fields()`).
    """
    public = getattr(vocab, "fields", None)
    if callable(public):
        found = public()
        specs = list(found.values()) if isinstance(found, dict) else list(found)
    else:
        table = getattr(vocab, "_fields", {})
        specs = list(table.values()) if isinstance(table, dict) else []
    return sorted((s for s in specs if isinstance(s, FieldSpec)), key=lambda s: s.id)


def panel_genes(vocab: Vocabulary) -> tuple[str, ...]:
    spec = vocab.field("gene.symbol")
    values = tuple(str(v) for v in (spec.values or ())) if spec else ()
    return values or ("KRAS", "NRAS", "TP53", "BRAF", "DIS3", "MYC")


def _needs_allowing(text: str) -> bool:
    return has_digit(text) or bool(number_words(text)) or bool(roman_numerals(text)) or bool(gene_like_tokens(text))


def vocabulary_terms(vocab: Vocabulary) -> list[str]:
    """Vocabulary labels and values that V-NUM would otherwise reject: they hold a digit, a number
    word, a Roman numeral or an upper-case token ("Stage II", "t(4;14): Present"). A value that is
    only a number ("0", "18.2") is left out, or every bare digit would pass."""
    terms: set[str] = set()
    for spec in vocabulary_fields(vocab):
        if spec.id in GENE_FIELDS or spec.type == "case_id":
            continue
        for text in (spec.label, spec.group, spec.value_prefix):
            if text and _needs_allowing(text):
                terms.add(text)
        for value in spec.values or ():
            text = str(value)
            if isinstance(value, str) and _LETTER.search(text) and _needs_allowing(text):
                terms.add(text)
    return sorted(terms)


def vocabulary_acronyms(vocab: Vocabulary) -> frozenset[str]:
    """Upper-case tokens of field labels and value prefixes (CGS, ISS): words, not gene symbols."""
    found: set[str] = set()
    for spec in vocabulary_fields(vocab):
        if spec.id in GENE_FIELDS:
            continue
        for text in (spec.label, spec.group, spec.value_prefix):
            found |= {t for t in tokens(text or "") if _GENE_SHAPE.match(t) and t not in ROMAN_NUMERALS}
    return frozenset(found)


def app_titles() -> list[str]:
    return sorted(app.title for app in APPS.values())
