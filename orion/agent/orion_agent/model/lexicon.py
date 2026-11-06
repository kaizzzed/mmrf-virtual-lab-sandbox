"""The words the verifier and the prompts share: number words, gene-like tokens, placeholders and
the vocabulary's terms (A2 verifier, V-NUM and V-ENT).

A gene symbol is recognised by its shape (HGNC symbols are upper case: BRAF, JUN, ETV4) after the
allow-listed terms are removed, so ORION never needs the sandbox's 10,000-symbol gene list:
8,659 of those symbols contain a digit anyway (V-NUM's digit rule), and the rest are upper-case
tokens that are not an acronym ORION knows.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Final

from ..catalog import APPS
from ..interfaces import PLACEHOLDER
from ..vocab import FieldSpec, Vocabulary

# Cyrillic and Greek letters drawn like Latin ones: "dexamethasone" written with a Cyrillic o reads as the drug name.
_CONFUSABLES: Final = str.maketrans(
    "аеорсухіјѕԁӏԛԝАВЕКМНОРСТХІЈЅУοαικνρτυχεΑΒΕΖΗΙΚΜΝΟΡΤΥΧ",
    "aeopcyxijsdlqwABEKMHOPCTXIJSYoaikvptuxeABEZHIKMNOPTYX",
)

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
# Letters and digits of any script, so a fullwidth "５０" is one token (and a digit).
_TOKEN: Final = re.compile(r"[^\W_]+(?:['’][^\W_]+)?")
_GENE_SHAPE: Final = re.compile(r"^[A-Z][A-Z0-9]+$")
_DIGIT: Final = re.compile(r"[0-9]")
# A whole number: not part of a word or a decimal. A full stop after it ends the sentence ("the top 6.").
_INTEGER: Final = re.compile(r"(?<![A-Za-z0-9.])([0-9]+)(?![A-Za-z0-9]|\.[0-9])")
_LETTER: Final = re.compile(r"[A-Za-z]")
GENE_FIELDS: Final = frozenset({"gene.symbol", "gene.gene_id"})
_MODEL_NAMES: Final[dict[str, str]] = {
    "anthropic.claude-opus-5-5": "Claude Opus 5.5",
    "anthropic.claude-opus-5": "Claude Opus 5",
    "anthropic.claude-sonnet-5-5": "Claude Sonnet 5.5",
}
# Bedrock inference-profile prefixes: us.anthropic.claude-opus-5-5 is Claude Opus 5.5.
_PROFILE_PREFIX: Final = re.compile(r"^(?:us|eu|apac|jp|au|global)\.(?=anthropic\.)")


def canonical_text(text: str) -> str:
    """`text` as a reader sees it, for the rules that read words: compatibility forms folded (fullwidth
    "ｌｅｎａｌｉｄｏｍｉｄｅ"), format characters and combining marks removed (a zero-width space or soft hyphen
    inside "bortezomib"), and Cyrillic or Greek letters drawn like Latin ones written as Latin."""
    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(ch for ch in decomposed if unicodedata.category(ch) not in ("Mn", "Cf"))
    return unicodedata.normalize("NFKC", kept).translate(_CONFUSABLES)


# V-SEC (G-5): the closed set of words a sentence about a regimen or a regimen-defined group may use, besides the
# run's names, placeholders, allow-listed terms and the vocabulary's descriptive field labels. They describe a group
# and its data: its size and members, and a comparison of what the run measured with a named group. A word outside
# the set fails such a sentence, so a benefit, outcome, management or advice claim needs no word list of its own
# ("may thrive", "may outlive the others", "is the way to go", "Begin <regimen> in …"). Left out on purpose: outcome
# words other than survival and its events (live, die, relapse, remission, response, progression), judgement words
# (better, best, good, well, safe, right, worth, help, advantage, edge, value), the treatment verbs (use, give, start,
# begin, keep, stay, join, add, assign, include, select, receive …) and their participles outside a described group
# ("cases receiving <regimen>"), "see", "ask", "need", "should", "must", "will" and agents ("clinicians", "we").
FUNCTION_WORDS: Final = frozenset(
    """a an the this that these those its their it they them there here which who whom whose what where when while
    whether if how why and or nor but yet so though although however also too as than of in on at by for with without
    outside within among amongst across between from to into over under per versus vs via after before during about around
    against along alongside beyond despite except unlike relative compared each every both either neither all any
    some none no not only even still other others another same such own most more less least much many few several
    rather whereas then thus hence instead s is are was were be been being has have had having do does did done may
    might could would can cannot can't couldn't doesn't don't didn't isn't aren't wasn't weren't hasn't haven't hadn't
    wouldn't mightn't""".split()
)
DESCRIPTIVE_WORDS: Final = FUNCTION_WORDS | frozenset(
    """
    differ differs differed differing difference differences gap gaps different differently distinct distinctly similar
    similarly comparable compare compares comparing comparison comparisons contrast contrasts contrasted
    appear appears appeared seem seems seemed tend tends tended tendency show shows showed shown showing seen suggest
    suggests suggested suggesting indicate indicates indicated consistent consistently inconsistent associated
    association associations link links linked related relationship relationships pattern patterns trend trends
    signal evidence explain explains explained remain remains remained persist persists persisted disappear
    disappears hold holds held vary varies varied variation cross crosses crossed crossing overlap overlaps overlapped
    separate separates separated separately separation apart split splits stratify stratified stratifies strata
    stratum restrict restricts restricted limit limited limits divide divided subset subsets filter filters filtered
    define defines defined match matched matches matching group grouped grouping rank ranks ranked ranking rankings
    form forms formed make makes made up part parts portion share shares majority minority contain contains contained
    cover covers covered falls fell pass passes passed note noted notes report
    reports reported record records recorded observe observed observation observations find finds found finding
    findings changes changed change increase increases increased increasing decrease decreases decreased reduced
    elevated enriched depleted overrepresented underrepresented over-represented under-represented up-regulated
    down-regulated upregulated downregulated overexpressed underexpressed
    check checks checked checking test tests tested testing untested look looks looked looking at explore explores
    explored exploring examine examines examined examining assess assesses assessed assessing assessment review
    reviews reviewed evaluate evaluates evaluated evaluating evaluation investigate investigates investigated
    consider considers considered considering inspect inspects inspected measure measures measured estimate
    estimates estimated count counts counted describe describes described summarize summarizes summarized summarise
    summarised list lists listed plot plots plotted chart charts table tables repeat repeats repeated repeating run
    runs ran open opens opened adjust adjusts adjusted adjusting adjustment adjustments unadjusted confound confounds
    confounded confounding confounder confounders bias biased limitation limitations caveat caveats
    gene genes expression expressed mutation mutations mutated mutant mutants unmutated non-mutated variant variants
    pathway pathways differential differentially rna dna seq ssm cnv snv wgs wxs
    wild-type panel panels top top-ranked fold log-rank kaplan meier statistic statistics statistically significance
    significant threshold thresholds p p-value p-values viewer viewers downstream upstream
    survival median mean average averages curve curves event events censored censoring os pfs progression-free
    event-free follow-up follow-ups time times day days month months year years duration durations earlier later
    longer shorter older younger higher lower high low greater smaller larger large small fewer total totals
    frequency frequencies frequent frequently infrequent often common uncommon rare rarer rarely usually sometimes
    rate rates proportion proportions fraction fractions number numbers ratio ratios interval intervals confidence
    uncertainty uncertain unclear possible possibly perhaps likely unlikely
    case cases patient patients group groups cohort cohorts subgroup subgroups arm arms sample samples people
    participants subjects individuals population populations members ones rest remaining reference control controls
    profiled eligible ineligible present absent available unavailable missing new first second next step steps
    previous previously prior current currently
    stage stages status age ages diagnosis diagnosed index gender sex female females male males race ethnicity white
    black african american asian hispanic latino non-hispanic bone marrow lesion lesions lytic tumor tumour tumors
    primary site type types strategy strategies multiple myeloma normal osteopenia osteoporosis high-risk
    standard-risk low-risk category categories criteria level levels yes
    data dataset datasets field fields plan plans scope analysis analyses result results synthetic regimen regimens
    sandbox virtual lab fabricated clinical meaning carries
    slightly slight somewhat modest modestly marked markedly considerable considerably substantial substantially
    notable notably particular particularly especially mainly mostly largely partly generally overall roughly
    broadly nearly almost approximately about very respectively further furthermore together alone single whole
    entire just solely
    """.split()
)


def json_list(value: object) -> list[object]:
    """A JSON value as a list: StepSpec.args values are JsonValue, and a missing key is no items."""
    return list(value) if isinstance(value, list) else []


def tokens(text: str) -> list[str]:
    """Word tokens; a hyphenated compound yields each part, so KRAS-mutated yields KRAS."""
    return _TOKEN.findall(text)


def has_digit(text: str) -> bool:
    """Any digit or numeric character: ASCII, fullwidth (５０), superscripts (²), fractions (½), numeral signs."""
    return _DIGIT.search(text) is not None or any(ch.isnumeric() for ch in text)


def integers(text: str) -> list[int]:
    return [int(m) for m in _INTEGER.findall(text)]


def number_words(text: str) -> list[str]:
    """Number words, and the solid -fold compounds of one ("twofold", "tenfold", "hundredfold"); "two-fold" splits."""
    out: list[str] = []
    for token in tokens(text):
        word = token.casefold()
        if word in NUMBER_WORDS or (word.endswith("fold") and word[: -len("fold")] in NUMBER_WORDS):
            out.append(token)
    return out


def panel_gene_mentions(text: str, genes: Iterable[str]) -> list[str]:
    """Tokens that are one of `genes` in any letter case ("kras", "Braf"): a gene symbol all the same."""
    folded = {g.casefold() for g in genes}
    return [token for token in tokens(text) if token.casefold() in folded]


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
    """Every field of the vocabulary, sorted by id."""
    return vocab.fields()


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
