"""The verifier (RFA rev 25, A2 verifier table; contract "Answer parts"; C-02).

Model text (Interpretation, Hypotheses, follow-ups) passes four rules, in this order; the first
that fails names the drop:

- V-REF: every placeholder is well formed and resolves to a fact of a finished step, every cite
  is a fact, every placeholder is cited, and every statement cites at least one fact.
- V-ENT: every cohort the text names ("… (ORION)", or a quoted name) is a run, ledger or
  vocabulary name; a follow-up's genes are panel genes or gene facts of the run.
- V-NUM: outside placeholders and allow-listed terms, no digit, no number word ("two groups"), no
  Roman numeral and no gene symbol. A follow-up may carry one integer from 1 to 100 when its kind
  takes a top N, and may name a known gene in plain text (it is asked as a new question).
- V-SEC: no causal or certainty word unless negated ("does not show a driver role"), no clinical
  or treatment advice (drug, brand and regimen names, the vocabulary's regimen values such as
  "VRd", care words such as "maintenance"); a hypothesis is a question ending in "?" or a hedged
  action. Follow-up labels and questions take the same causal and clinical checks. A clinical word
  inside a name the run itself uses (a regimen-defined cohort, a regimen value the plan filters
  on) is read as a name only where the text uses it as the name of a group ("the <regimen>
  (ORION) cohort", "cases receiving <regimen>", "in <regimen>", "<regimen> and the remaining
  cases", "<regimen> was the most common regimen"), never as the answer ("It is <regimen>"), as
  the bare object of a verb ("Consider <regimen> and …") or alone. A text that names such a name,
  every text of a run that names one, and any text with a treatment noun ("regimen") may then use
  only plain descriptive words (lexicon.DESCRIPTIVE_WORDS), so a benefit, outcome, management or
  advice claim fails whatever its wording ("may thrive", "may outlive the others", "Begin … on
  <regimen>"), and code reads the frames that descriptive words can still build: the regimen
  offered to or tried on a group ("<regimen> (ORION) for these cases", "Examine … in the mutated
  cases"), cases placed in its group ("could be among those receiving <regimen>"), its effect,
  and a survival comparison that names no comparison group. Any other mention is checked like any
  other text, as is a vocabulary value the run does not use. These rules read the text as a
  reader sees it (lexicon.canonical_text: no zero-width characters, no look-alike letters).

Observed and Limitations are code-written, so only V-COMP and V-LIM apply to them, and code fixes
what they find: V-COMP inserts the journey's missing template sentence (J1 groups and log-rank p,
J2 cohort size and ranking, J3 groups, method and top-N table), V-LIM the missing limitation
line (plan limitations, rounding, ties, fewer than N, panel coverage, subset, empty, the hazard
ratio). On the first attempt a failing model statement asks for one re-compose (`retry`); on the
second it is dropped, leaving `{"segments": [], "dropped": "<rule>"}`. When no Interpretation
sentence survives, `observed_only` is set (the Interpretation-present gate, C-02).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, get_args

from pydantic import BaseModel, JsonValue

from ..catalog import APPS, AnalysisKind
from ..compiler import CompiledPlan, SessionLedger, StepSpec
from ..interfaces import (
    PLACEHOLDER,
    ComposerFollowUp,
    ComposerHypothesis,
    ComposerOutput,
    ComposerStatement,
    ObservedResult,
    RuleFinding,
    VerifiedPart,
    VerifierResult,
)
from ..models import (
    MAX_TOP_N_DE,
    FactRecord,
    FollowUp,
    Limitation,
    NearestApp,
    Segment,
    Sentence,
    VerifierRule,
    format_fact,
)
from ..tools import schemas
from ..vocab import Vocabulary
from .lexicon import (
    ACRONYMS,
    DESCRIPTIVE_WORDS,
    FIXED_TERMS,
    FUNCTION_WORDS,
    GENE_FIELDS,
    ORION_SUFFIX,
    app_titles,
    canonical_text,
    cohort_label,
    gene_like_tokens,
    has_digit,
    integers,
    json_list,
    number_words,
    panel_gene_mentions,
    panel_genes,
    placeholders,
    remove_terms,
    roman_numerals,
    tokens,
    vocabulary_acronyms,
    vocabulary_fields,
    vocabulary_terms,
    without_placeholders,
)

MODEL_RULES: Final[tuple[VerifierRule, ...]] = ("V-REF", "V-ENT", "V-NUM", "V-SEC")
TOP_N_KINDS: Final[frozenset[str]] = frozenset({"differential_expression", "top_mutated_genes"})
ANALYSIS_KINDS: Final = frozenset(get_args(AnalysisKind))
_NEGATORS: Final = frozenset(
    {"not", "no", "never", "cannot", "can't", "doesn't", "don't", "didn't", "isn't", "aren't", "wasn't", "won't", "nor",
     "without", "neither", "doesn’t", "don’t", "isn’t", "can’t"}
)
_NEGATION_WINDOW: Final = 4
_CAUSAL: Final = re.compile(
    r"\b(prov(?:e|es|ed|en|ing)|proof|caus(?:e|es|ed|ing|al|ally|ation|ative)|driv(?:e|es|en|ing|er|ers)|"
    r"determin(?:e|es|ed|ing)|confirm(?:s|ed|ing)?|demonstrat(?:e|es|ed|ing)|establish(?:es|ed|ing)?|"
    r"leads? to|led to|results? in|resulted in|due to|because of|responsible for|guarantee(?:s|d)?|"
    r"clearly|definitely|definitively|certainly|undoubtedly|conclusive(?:ly)?|unequivocal(?:ly)?)\b",
    re.IGNORECASE,
)
# An infinitive of purpose ("to determine whether") names a test, not a claim.
_PURPOSE_VERBS: Final = frozenset({"determine", "confirm", "establish", "demonstrate", "prove"})
_CLINICAL: Final = re.compile(
    r"\b(treat(?:s|ed|ing|ment|ments)?|\w*therap(?:y|ies|eutic|eutics|ist|ists)|chemo\w*|radiation|"
    r"transplant(?:s|ed|ation|ations)?|drugs?|medications?|doses?|dosing|dosage|"
    r"prescri(?:be|bes|bed|bing|ption|ptions)|recommend(?:s|ed|ing|ation|ations)?|inhibitors?|prognos(?:is|es|tic)|"
    r"benefit(?:s|ed|ing)? from|(?:good |better |ideal |suitable |poor )?candidates? for(?! (?:further|follow|validation|study|"
    r"studies|analysis|testing|comparison)\b)|respond(?:s|ed|ing)? (?:well |poorly |better |worse )?to|"
    r"targetable|clinical trials?|curative|cure[sd]?|"
    r"standard of care|patient care|clinical (?:decision|decisions|care|practice|use|management)|"
    r"should (?:receive|be given|be treated|take|start|stop|avoid)|"
    # Myeloma and panel-gene drugs and classes (G-5): proteasome inhibitors, IMiDs, antibodies, CAR-T, KRAS/BRAF/MEK drugs.
    r"\w+zomib|\w*lidomide|imids?|\w+mab|monoclonal antibod(?:y|ies)|antibody-drug conjugates?|bispecifics?|"
    r"car[- ]t|cilta-cel|ide-cel|dexamethasone|melphalan|cyclophosphamide|selinexor|venetoclax|\w+tinib|\w+rafenib|"
    r"\w+metinib|\w+rasib|steroids?|corticosteroids?|predniso(?:ne|lone)|\w+inostat|\w*rubicin|\w+platin|\w+mustine|"
    r"vincristine|etoposide|interferon|bisphosphonates?|zoledron\w*|pamidronate|plerixafor|\w+grastim|\w+-cel|"
    # Brand names, and the care words a regimen text uses without a drug name ("Add maintenance for …").
    r"velcade|revlimid|kyprolis|darzalex|pomalyst|imnovid|thalomid|ninlaro|empliciti|sarclisa|xpovio|carvykti|abecma|"
    r"tecvayli|elrexfio|talvey|blenrep|farydak|alkeran|evomela|cytoxan|decadron|maintenance|asct|autologous|allogeneic|"
    r"stem[- ]cell (?:rescue|support|infusion|harvest|collection|graft|boost)s?|intensif(?:y|ies|ied|ying|ication|ications)|"
    r"de-?intensif\w*|aggressive (?:management|approach|care|regimens?))\b",
    re.IGNORECASE,
)
# Myeloma regimen abbreviations (G-5): a capital per drug and a lower-case d for dexamethasone ("VRd", "KRd", "DRd",
# "VCd", "Rd", "IsaKd", "Dara-VRd"), and CyBorD. Case-sensitive, so ordinary words never match.
_REGIMEN_ABBREVIATION: Final = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:Dara|Isa|Elo|Ixa|Sel|Pom|Cy)-?)?[VRKDCTPMIESX]{1,4}d(?![A-Za-z0-9])|(?<![A-Za-z0-9])CyBorD(?![A-Za-z0-9])"
)
# The words that make a text about a treatment even where it names none of the run's names (G-5): "Intensify the
# regimen for the high-risk cases", "The high-risk cases may need a stronger regimen". Such a text takes the
# descriptive-word check (`_treatment_text_failure`), as a text naming a regimen-defined cohort does.
_TREATMENT_NOUN: Final = re.compile(r"\b(?:regimens?|management)\b", re.IGNORECASE)

# A run's own name that carries a clinical word (a regimen-defined cohort, a regimen value the plan filters on) is a
# name only where the text uses it as the name of a group, and the sentence around it then only describes that
# group's data (G-5). The reading has two steps:
#
# 1. Each mention's place (`_name_uses`): a population or compared group ("in <name>", "survival of <name>",
#    "between <name> and …", "<name> and the remaining cases", "cases receiving <name>", "whose regimen name is
#    <name>"), the subject or topic ("<name> was the most common regimen", "the <name> cases may be older"), or the
#    object of a verb. It is not a name, and its clinical word is advice, where the sentence answers with it ("It is
#    <name>", "The next step is <name> (ORION)"), where it stands alone, or where a verb other than a cohort operation
#    takes the bare regimen as its object ("Consider <regimen> and …", "Test <regimen>"); "Consider" takes a whole
#    cohort name only to set it apart ("Consider the <regimen> (ORION) cohort separately").
# 2. The text (`_treatment_text_failure`): every word is one of the plain descriptive words (lexicon.DESCRIPTIVE_WORDS),
#    so a benefit, outcome, management or advice claim fails whatever its wording, and code reads the frames that
#    descriptive words can still build: the regimen offered to a group ("<name> (ORION) for these cases", "Examine
#    <name> (ORION) in the high-risk patients"), cases placed in its group ("could be among those receiving
#    <name>"), its effect ("<name> (ORION) may lower …"), and a survival comparison that names no comparison group
#    ("Patients receiving <name> may have higher survival").
#
# In a run that names such a name, every model text takes step 2, so a pronoun cannot carry the advice to another
# sentence ("…; consider it for the mutated cases"); elsewhere a text takes it when it names one of those names or a
# treatment noun.
_GROUP_WORDS: Final = r"(?:cases?|patients?|groups?|cohorts?|subgroups?|arms?|samples?)"
_GROUP_NOUNS: Final = rf"(?:{_GROUP_WORDS}|those|people|participants|subjects|individuals|ones|others|rest)"
_GROUP_NOUN: Final = re.compile(rf"{_GROUP_NOUNS}", re.IGNORECASE)
_ORION_AFTER: Final = re.compile(r"^[\"”’']?\s*\(ORION\)", re.IGNORECASE)
_GROUP_AFTER: Final = re.compile(rf"^[\"”’']?\s+{_GROUP_WORDS}\b", re.IGNORECASE)
_ENDS_AS_NAME: Final = re.compile(rf"(?:\b{_GROUP_WORDS}|\(ORION\))\s*$", re.IGNORECASE)
_OPENING: Final = re.compile(r"[\"“‘'\[(]\s*$")
# "cases receiving <name>", "patients who were on <name>", "the mutated cases mostly received <name>", "cases whose
# first regimen was <name>": a group described by what it had. The group noun and the verb belong to the name's span.
_DESCRIBED: Final = re.compile(
    rf"\b{_GROUP_NOUNS}\s+(?:(?:who|that|which)\s+)?(?:(?:have|has|had|were|was|are|is)\s+)?(?:been\s+)?"
    r"(?:(?:\w+ly|all|also|only|still|ever|first|both)\s+)?"
    r"(?:receiv(?:ed|ing)|took|tak(?:en|ing)|start(?:ed|ing)|given|on|with|had|got|"
    r"(?:assigned|exposed)\s+to|(?:stayed|remained|continued)\s+on|"
    r"whose\s+(?:(?:first|initial|last|latest|current|previous)\s+)?regimen(?:\s+name)?\s+(?:is|was))"
    r"\s+(?:the\s+)?$",
    re.IGNORECASE,
)
_SKIPPED: Final = frozenset({"the", "a", "an", "this", "these", "its", "their", "our"})
_CLAUSE_MARKS: Final = frozenset({".", ";", ":", "?", "!", ",", "(", "[", "—", "–"})
_POPULATION_WORDS: Final = frozenset(
    {"in", "within", "among", "amongst", "across", "inside", "of", "for", "between", "from", "than", "versus", "vs",
     "and", "or", "nor", "on", "to", "with", "without", "by", "except", "outside", "beyond", "against", "unlike", "both",
     "either", "neither"}
)
_COPULAS: Final = frozenset(
    {"is", "are", "was", "were", "be", "been", "being", "become", "becomes", "became", "remain", "remains", "remained",
     "seem", "seems", "seemed", "appear", "appears", "appeared", "look", "looks", "looked", "s"}
)
_QUESTION_OPENERS: Final = _COPULAS | frozenset(
    {"do", "does", "did", "can", "could", "may", "might", "would", "should", "will", "has", "have", "had"}
)
# Cohort operations take a regimen-defined group as their object: "Compare <regimen> and the remaining cases".
_COHORT_OPERATIONS: Final = frozenset(
    {"compare", "compares", "compared", "comparing", "contrast", "contrasted", "restrict", "restricted", "split",
     "stratify", "stratified", "separate", "separated", "divide", "divided", "limit", "limited", "subset", "filter",
     "filtered"}
)
# Verbs that, with a regimen or a pronoun as their object, test it on a group: "Examine <name> in the mutated cases".
_TRIAL_VERBS: Final = frozenset(
    {"consider", "considering", "test", "testing", "explore", "exploring", "assess", "assessing", "evaluate",
     "evaluating", "examine", "examining", "investigate", "investigating", "review", "reviewing", "check", "checking",
     "inspect", "look", "looking", "study", "studying"}
)
# Other verbs that take a regimen-defined cohort as their object ("Show <name> (ORION) by ISS stage", "Look at …"), and
# the words that stand before a name without taking it as an object ("the same <name> cases", "only <name> (ORION)").
# Any other verb before a whole cohort name offers it ("…, which suggests <name> (ORION)", "may have <name> (ORION)").
_COHORT_VERBS: Final = (_COHORT_OPERATIONS | _TRIAL_VERBS | frozenset(
    {"open", "count", "plot", "chart", "show", "list", "describe", "summarize", "summarise", "rank", "repeat", "run",
     "measure", "at"}
)) - frozenset({"consider", "considering"})
_BEFORE_A_NAME: Final = frozenset(
    {"same", "only", "also", "even", "just", "all", "each", "every", "any", "some", "other", "older", "younger",
     "mutated", "entire", "whole", "which", "what", "whose", "more", "most", "fewer", "less", "larger", "smaller",
     "first", "second", "remaining", "matched", "profiled", "eligible", "tested", "female", "male", "high-risk",
     "standard-risk", "low-risk", "wild-type", "non-mutated", "unmutated", "not"}
)
_GROUP_MODIFIERS: Final = (
    r"(?:(?:the|these|those|all|other|more|some|remaining|high-risk|standard-risk|low-risk|older|younger|female|male|"
    r"[\w-]+ed)\s+){0,3}"
)
# A group a trial verb takes, put on the regimen: "Consider the mutated cases for <name>", "Test the high-risk patients
# on <name>".
_TRIAL_DESTINATION: Final = re.compile(
    rf"\b(?:{'|'.join(sorted(_TRIAL_VERBS))})\s+{_GROUP_MODIFIERS}{_GROUP_NOUNS}\s+(?:(?:who|that)\s+(?:are|were)\s+)?"
    r"(?:for|with|to|into|onto|on)\s+(?:the\s+)?$",
    re.IGNORECASE,
)
_SET_APART: Final = re.compile(r"\b(?:separately|separate|apart|alone|alongside|isolation|own)\b", re.IGNORECASE)
_LIST_JOIN: Final = re.compile(r"^[\"”’']?\s*,?\s+(?:and|or|nor|versus|vs\.?)\s+", re.IGNORECASE)
_LIST_MODIFIERS: Final = frozenset(
    {"the", "these", "those", "other", "all", "remaining", "both", "each", "any", "some", "its", "their", "older",
     "younger", "female", "male", "matched", "comparison", "reference", "control", "high-risk", "standard-risk",
     "low-risk", "wild-type", "non-mutated", "unmutated"}
)
# Marks that stand for what the descriptive-word check does not read: a name used as a regimen (N) or as a population
# (P), a fact placeholder (F), a cohort name of the run or the ledger (C), and another allowed term or gene (T).
_N, _P, _F, _C, _T = "\ue000", "\ue001", "\ue002", "\ue003", "\ue004"
_WORD: Final = re.compile(r"[A-Za-z]+(?:[-'’][A-Za-z]+)*")
_RECIPIENT_GROUP: Final = (
    rf"(?:(?:the|these|those|all|other|such|some|each|any|both|its|their|more|most|many|several|every)\s+)*"
    rf"(?:(?:[\w-]+\s+){{0,3}}?{_GROUP_NOUNS}\b|[\w-]+(?:ed|risk|type)\s*(?=[?.!,;]|$))"
)
_RECIPIENT: Final = re.compile(rf"\b(?:for|on|in|into|onto|to|with)\s+{_RECIPIENT_GROUP}", re.IGNORECASE)
_FILLERS: Final = (
    r"(?:(?:\w+ly|first|next|instead|now|early|again|too|here|there|alone|only|then|also|even|still|perhaps|maybe|"
    r"(?:may|might|could|would|can|should|will)(?:\s+(?:also|only|still))?\s+be|is|are|was|were)\s*)"
)
# "<name> (ORION) for these cases", "<name> (ORION) may be for the mutated cases", "<name> (ORION) — the mutated cases".
_RECIPIENT_NEXT: Final = re.compile(
    rf"^\s*[,—–-]?\s*(?:{_FILLERS}[,—–-]?\s*){{0,3}}(?:(?:for|on|in|into|onto|to|with)\s+|(?:[—–:→>]|-+>?)\s*){_RECIPIENT_GROUP}",
    re.IGNORECASE,
)
# "For the mutated cases, <name> (ORION) …": a group put first, then the regimen.
_RECIPIENT_FIRST: Final = re.compile(rf"(?:^|[.;?!]\s*)\W*(?:for|in|on|with)\s+{_RECIPIENT_GROUP}\s*[,—–:]", re.IGNORECASE)
# "<name> (ORION) could be tested in the mutated cases", "The mutated cases may be considered for <name>": the regimen
# under trial.
_TRIED: Final = r"(?:considered|tested|tried|examined|evaluated|explored|assessed|reviewed|checked|investigated|inspected)"
_TRIED_NEXT: Final = re.compile(
    rf"^\s*(?:(?:may|might|could|would|can|should|will|also|only|still|not)\s+)*(?:be|being|been)\s+(?:also\s+)?{_TRIED}\b",
    re.IGNORECASE,
)
_PRIORITY_NEXT: Final = re.compile(r"^\s*(?:first|next|instead|now|early|again)\b", re.IGNORECASE)
_EFFECT_NEXT: Final = re.compile(
    r"^\s*(?:(?:may|might|could|would|can|will|should|also|alone)\s+)*(?:not\s+)?"
    r"(?:lower|lowers|lowered|raise\w*|increase\w*|decrease\w*|reduce\w*|extend\w*|prolong\w*|improve\w*|delay\w*|"
    r"shorten\w*|lengthen\w*|boost\w*|cut|cuts|explain\w*|change\w*)\b",
    re.IGNORECASE,
)
# "<name> (ORION) is where the mutated cases could be", "… may be the ones …": the regimen as the answer.
_ANSWER_NEXT: Final = re.compile(
    r"^\s*(?:(?:may|might|could|would|can)(?:\s+(?:also|only|still))?\s+be|is|are|was|were|seems?|appears?)\s+(?:also\s+)?"
    r"(?:where|what|whom|who|the\s+ones?)\b",
    re.IGNORECASE,
)
# "Patients receiving <name> earlier …", "… for a longer time …" with a survival comparison: when to give it.
_TIMING_NEXT: Final = re.compile(
    r"^\s*(?:earlier|later|sooner|early|late|first|next|for\s+(?:a\s+)?(?:longer|shorter)(?:\s+time)?|longer|shorter)\b",
    re.IGNORECASE,
)
_PLACES: Final = r"(?:among|in|on|into|within|with|inside|part\s+of|one\s+of|added\s+to)"
_MEMBERSHIP: Final = re.compile(
    rf"\b(?:(?:be|remain\w*|persist\w*|stay\w*)\s+(?:not\s+)?{_PLACES}"
    rf"|\bif\b[^.;:?!]*?\b(?:were|was)\s+(?:also\s+)?{_PLACES}"
    rf"|(?:to|may|might|could|would|should|can)\s+(?:not\s+)?have(?:\s+(?:had|received|taken|been\s+(?:on|given|offered|started)))?"
    rf"\s+{_GROUP_MODIFIERS}"
    rf"|\bhave\s+(?:them|{_GROUP_MODIFIERS}{_GROUP_NOUNS})\s+{_PLACES}"
    rf"|\bbe\s+{_TRIED}\s+(?:for|on|with|in)\b)"
    rf"\s*(?:(?:the|these|those)\s+)?[{_N}{_P}]"
    rf"|\b(?:remain\w*|persist\w*|stay\w*)\s+(?:there|so|with\s+(?:it|them)|on\s+(?:it|them))\b"
    rf"|\b(?:to|may|might|could|would|should|can)\s+(?:not\s+)?(?:also\s+)?have\s+(?:it|them)\b"
    rf"|\bchange\s+(?:regimens?|course|it|them|over|to)\b"
    # "Adjust the regimen", "hold it", "repeat <name> (ORION)": a change to the treatment itself.
    rf"|\b(?:adjust|change|increase|decrease|reduce|lower|raise|hold|alter|modify|extend|shorten|repeat)(?:s|ed|ing)?\s+"
    rf"(?:(?:the|this|that|its|their|each|every)\s+)?(?:[\w-]+\s+)?(?:regimens?\b|it\b|them\b|[{_N}])"
    # "…, which suggests it": the regimen recommended.
    rf"|\b(?:suggest|indicate)\w*\s+(?:it|them|the\s+same(?:\s+one)?|(?:this|that)\s+(?:one|regimen)|the\s+regimen)"
    rf"\s*(?=$|[.;:?!,]|\s+(?:for|to|in|on|with|as|here|first|instead|now)\b)",
    re.IGNORECASE,
)
_CLAUSE_OPENER: Final = re.compile(
    r"(?:^|[.;:?!]\s*)[\"“‘'(\[]*\s*(have|make|let|be|hold|change|increase|decrease|raise|lower|reduce|remain|persist)\b",
    re.IGNORECASE,
)
# A survival comparison of a regimen-defined group names the group it is compared with: "… than the remaining
# cases", "Within <name>, the mutated cases have shorter survival". Without one ("Patients receiving <name> may have
# higher survival") it reads as a benefit claim.
_OUTCOME: Final = re.compile(r"\b(?:survival|events?|os|pfs|progression-free|event-free|time to progression)\b", re.IGNORECASE)
_COMPARATIVE: Final = re.compile(
    r"\b(?:longer|shorter|higher|lower|greater|smaller|larger|more|less|fewer|most|least|later|earlier|increase[sd]?|"
    r"decrease[sd]?|reduced|elevated|rises|falls|fell)\b",
    re.IGNORECASE,
)
_EXPLICITLY_COMPARED: Final = re.compile(r"\b(?:than|between|versus|vs|compared|relative|unlike|against)\b", re.IGNORECASE)
# "Would the mutated cases have longer survival in <name>?": a group placed in the regimen's population after the
# comparison, with no comparison group named, asks what the regimen would do for it.
_PLACED_AFTER: Final = re.compile(
    rf"{_COMPARATIVE.pattern}[^.;:?!]*?\b(?:in|on|with|under|within|among)\s+(?:the\s+)?[{_N}{_P}]", re.IGNORECASE
)
_COMPARED_WITH: Final = re.compile(
    rf"\b(?:than|between|versus|vs|compared|relative|unlike|against|similar|same|differ\w*|difference\w*|comparisons?)\b"
    rf"|\b(?:the\s+)?(?:other|remaining|rest|mutated|non-mutated|unmutated|wild-type|older|younger|female|male|"
    rf"high-risk|standard-risk|low-risk|[\w-]+ed)\s+(?:cases|patients|groups?|subgroups?|arms?|samples|ones)\b"
    rf"|\bthe\s+(?:others|rest)\b|[{_C}]",
    re.IGNORECASE,
)
# Words and phrases the descriptive-word check reads as one: "genes of interest" (but "interest in <regimen>" fails),
# "a driver role" (negated, as V-SEC's causal check requires), "to see whether", "to determine whether", "candidates
# for further study", "the ranking is stable", "ahead of {{gene}}", "followed by {{gene}}", a measure that varies with
# age or stage ("survival does not appear to improve with age", never "patients … improve"), the survival measures,
# the risk classifications, and statements that no group is favoured ("with no advantage for either group", "does
# not favour either group", "so no group is favoured"), which name no group as the better one.
_PHRASES: Final = re.compile(
    "|".join((
        r"\bof\s+interest\b",
        r"\bto\s+see\s+(?:whether|if|how|its|their|the|which|what)\b",
        r"\bto\s+(?:determine|confirm|establish|demonstrate|prove)\b",
        r"\bdriver\s+roles?\b",
        r"\bcandidates?\s+for\s+(?:(?:further|follow-?up)\s+)?"
        r"(?:validation|study|studies|analysis|analyses|testing|comparison|comparisons|work|research)\b",
        rf"\b(?:rankings?|order|results?|differences?|associations?|patterns?|estimates?)"
        rf"(?:\s+(?:in|of|for|within)\s+(?:the\s+)?(?:[\w-]+|[{_P}{_C}{_T}{_F}]))?"
        r"\s+(?:is|are|was|were|remains?|(?:may|might|could)\s+be|(?:appears?|seems?)\s+to\s+be)\s+stable\b",
        r"\b(?:survival|expression|frequency|ranking|difference|gap)\s+(?:(?:does|did|do|may|might|could|can)\s+(?:not\s+)?)?"
        r"(?:(?:appear|seem|tend)\s+to\s+)?(?:improve|worsen|decline)s?\s+with\s+(?:increasing\s+|older\s+)?"
        r"(?:age|(?:iss\s+)?stage)\b",
        rf"\b(?:ahead\s+of|followed\s+by)\s+{_F}",
        r"\btime\s+to\s+progression\b",
        r"\bhazard\s+ratios?\b",
        r"\b(?:cgs\s+)?risk\s+(?:category|categories|criteria|groups?|class(?:es)?)\b",
        r"\b(?:high|standard|low)\s+risk\b",
        r"\bfold\s+changes?\b",
        r"\bp\s+values?\b",
        r"\b(?:advantage|favou?r(?:s|ed|ing)?)\s+(?:for\s+|to\s+)?neither\b(?:\s+(?:group|cohort|side)s?)?",
        r"\b(?:no|not|never|without)\b(?:\s+[\w-]+){0,4}?\s+(?:advantage|favou?r(?:s|ed|ing)?)\s+(?:for\s+|to\s+)?(?:either|any)\b"
        r"(?:\s+(?:of\s+the\s+)?(?:groups?|cohorts?|sides?))?",
        r"\bno\s+(?:group|cohort|side)\s+(?:is|was|appears?|seems?)\s+(?:to\s+be\s+)?favou?red\b",
    )),
    re.IGNORECASE,
)
# A vocabulary label naming an outcome is not read as a descriptive phrase ("best overall response", "days to death").
_OUTCOME_LABEL: Final = re.compile(r"\b(?:death|dead|alive|vital|respon\w*|progress\w*|relapse\w*|remission|mortality|censor)\b")
_ACTION_VERBS: Final = frozenset(
    {"compare", "check", "test", "look", "explore", "ask", "examine", "assess", "see", "review", "rank", "count", "split",
     "repeat", "run", "open", "plot", "measure", "evaluate", "investigate", "consider", "inspect", "contrast", "restrict"}
)
_HEDGES: Final = frozenset({"whether", "if", "may", "might", "could", "would"})
_QUOTED: Final = re.compile(r"[\"“”]([^\"“”]{2,120})[\"“”]")
_BRACES: Final = re.compile(r"\{\{|\}\}|\{f\.")


# --- Per-run context -----------------------------------------------------------------------------


@dataclass
class _Context:
    facts: dict[str, FactRecord]
    terms: list[str]
    acronyms: frozenset[str]
    names: frozenset[str]  # casefolded known names (cohorts, vocabulary, apps)
    genes: frozenset[str]  # genes a follow-up may name in plain text
    findings: list[RuleFinding] = field(default_factory=list)
    # The six-gene panel: a symbol of it in any case ("kras") still needs a placeholder outside follow-ups.
    panel: frozenset[str] = frozenset({"KRAS", "NRAS", "TP53", "BRAF", "DIS3", "MYC"})
    # Names this run uses (its cohorts, the ledger's, apps, and vocabulary values its scope, cohorts or settings
    # name) with a clinical word in them or that are regimen values, such as a cohort defined by the regimen
    # "Bortezomib, lenalidomide, dexamethasone": V-SEC reads them as names only where the text uses them as names
    # (`_name_uses`). Names, terms and labels here are canonical_text, casefolded where noted.
    clinical_names: tuple[str, ...] = ()
    # The vocabulary field labels of those names that are field values ("reason for discontinuation" for
    # "completed planned therapy"), casefolded: "… whose <label> is <value>" uses the value as a name.
    clinical_labels: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Whether the run's own plan names one of them: every model text of the run then takes the descriptive-word check.
    treatment_run: bool = False
    # The vocabulary's regimen values, casefolded ("vrd", "bortezomib, lenalidomide, dexamethasone"): clinical words.
    regimen_values: tuple[str, ...] = ()
    # The run's and the ledger's cohort names, casefolded: a group the descriptive-word check may compare with.
    cohort_names: frozenset[str] = frozenset()
    # The vocabulary's field labels of more than one word that name no outcome ("iss stage", "reason for
    # discontinuation"), casefolded, and each field's "<label> is <value>" pattern: read as names by that check.
    label_phrases: tuple[str, ...] = ()
    field_values: tuple[re.Pattern[str], ...] = ()


def _step_names(step: StepSpec) -> list[str]:
    names: list[str] = []
    args = step.args
    if step.tool == "save_cohorts":
        for cohort in json_list(args.get("cohorts")):
            if isinstance(cohort, dict) and isinstance(cohort.get("name"), str):
                names.append(str(cohort["name"]))
    if step.tool == "de_run":
        for group in json_list(args.get("groups")):
            if isinstance(group, dict) and isinstance(group.get("name"), str):
                names.append(str(group["name"]))
    if step.tool == "survival_compare":
        for group in json_list(args.get("groups")):
            if isinstance(group, dict) and isinstance(group.get("label"), str):
                names.append(str(group["label"]))
    return names


def plan_cohort_names(plan: CompiledPlan) -> list[str]:
    """The run's cohort names: the scope, the alternative scope and every name a step carries."""
    names = [plan.scope.name] + ([plan.alt_scope.name] if plan.alt_scope else [])
    for step in plan.steps:
        names += _step_names(step)
    return names


def ledger_cohort_names(ledger: SessionLedger | None) -> list[str]:
    if ledger is None:
        return []
    return [run.scope.name for run in ledger.runs] + [c.name for run in ledger.runs for c in run.cohorts]


def _viz_names(observed: ObservedResult) -> list[str]:
    names: list[str] = []
    for spec in observed.viz_specs:
        pinned = spec.pinned
        groups = getattr(pinned, "groups", None) if pinned is not None else None
        for group in groups or []:
            name = getattr(group, "name", None)
            if isinstance(name, str):
                names.append(name)
    return names


def _with_labels(names: Iterable[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        if name.strip():
            out.append(name)
            label = cohort_label(name)
            if label != name and label.strip():
                out.append(label)
    return out


def _resolve(text: str, facts: Mapping[str, FactRecord]) -> list[Segment]:
    """Text and fact segments; a fact segment's text is format_fact(value, unit), as Observed writes it."""
    segments: list[Segment] = []
    position = 0
    for match in PLACEHOLDER.finditer(text):
        if match.start() > position:
            segments.append(Segment(text=text[position : match.start()]))
        fact = facts[match.group(1)]
        segments.append(Segment(text=format_fact(fact.value, fact.unit), fact_id=fact.id))
        position = match.end()
    if position < len(text):
        segments.append(Segment(text=text[position:]))
    merged: list[Segment] = []
    for segment in segments:
        if segment.fact_id is None and merged and merged[-1].fact_id is None:
            merged[-1] = Segment(text=merged[-1].text + segment.text)
        elif segment.fact_id is not None or segment.text:
            merged.append(segment)
    return merged


def _plain(text: str, facts: Mapping[str, FactRecord]) -> str:
    return PLACEHOLDER.sub(lambda m: format_fact(facts[m.group(1)].value, facts[m.group(1)].unit), text)


# --- Rules on model text -------------------------------------------------------------------------


def _check_ref(text: str, cites: Sequence[str] | None, ctx: _Context) -> str | None:
    if _BRACES.search(without_placeholders(text)):
        return "a placeholder is malformed; write {{<fact id>}} with a fact id from the request"
    unknown = [p for p in placeholders(text) if p not in ctx.facts]
    if unknown:
        return f"placeholder {unknown[0]} is not a fact of a finished step"
    if cites is None:
        return None
    if not cites:
        return "the statement cites no fact"
    missing = [c for c in cites if c not in ctx.facts]
    if missing:
        return f"cite {missing[0]} is not a fact of a finished step"
    uncited = [p for p in placeholders(text) if p not in cites]
    if uncited:
        return f"placeholder {uncited[0]} is not in cites"
    return None


def _check_ent(stripped: str, ctx: _Context) -> str | None:
    if ORION_SUFFIX.strip() in stripped:
        return "the text names a cohort that is not in this run, its ledger or the vocabulary"
    for match in _QUOTED.finditer(stripped):
        if match.group(1).strip().casefold() not in ctx.names:
            return f"the quoted name {match.group(1)!r} is not a cohort, field value or app of this run"
    return None


def _check_num(stripped: str, ctx: _Context) -> str | None:
    if has_digit(stripped):
        digits = [t for t in tokens(stripped) if has_digit(t)]
        return f"{digits[0] if digits else 'a digit'} is written outside a fact placeholder"
    words = number_words(stripped)
    if words:
        return f"the number word {words[0]!r} is written outside a fact placeholder"
    numerals = roman_numerals(stripped)
    if numerals:
        return f"the numeral {numerals[0]!r} is written outside a fact placeholder"
    genes = gene_like_tokens(stripped, ctx.acronyms)
    if genes:
        return f"{genes[0]} looks like a gene symbol written outside a fact placeholder"
    mentions = panel_gene_mentions(stripped, ctx.panel)
    if mentions:
        return f"{mentions[0]} names a panel gene outside a fact placeholder"
    return None


def _causal(text: str) -> str | None:
    for match in _CAUSAL.finditer(text):
        before = [w.casefold() for w in tokens(text[: match.start()])]
        if any(w in _NEGATORS for w in before[-_NEGATION_WINDOW:]):
            continue
        term = match.group(1).casefold()
        if term in _PURPOSE_VERBS and before[-1:] == ["to"]:
            continue
        return term
    return None


def _clause(text: str) -> str:
    """`text` up to the end of its clause."""
    end = re.search(r"[.;:?!]", text)
    return text[: end.start()] if end else text


def _clinical_term(text: str, ctx: _Context) -> str | None:
    """A clinical word, a regimen abbreviation or a vocabulary regimen value in `text`."""
    match = _CLINICAL.search(text)
    if match:
        return match.group(1)
    abbreviation = _REGIMEN_ABBREVIATION.search(text)
    if abbreviation:
        return abbreviation.group(0)
    for value in ctx.regimen_values:
        found = re.search(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", text, re.IGNORECASE)
        if found:
            return found.group(0)
    return None


def _label_before(label: str) -> str:
    """The text before a field value that uses it as the value of its field: "cases whose <label> is", "with a <label>
    of", "<label>:" opening the text, "the most common <label> was"."""
    escaped = re.escape(label)
    return (rf"(?:\b(?:whose|with|where|by)\s+(?:(?:a|an|the|their|its)\s+)?{escaped}\s*(?:(?:is|was|of|=|:)\s*)?"
            rf"|^\W*{escaped}\s*[:=]\s*|\b(?:most|least)\s+(?:common|frequent)\s+{escaped}\s+(?:is|was)\s+)$")


def _listed_with_groups(after: str, names: frozenset[str]) -> bool:
    """Whether the text right after a name lists it with other groups: "and", "or" or "versus" and then a known name
    or a noun phrase that ends in a group word ("the remaining cases", "other high-risk patients", "the rest"). A verb
    phrase after "and" ("<name> and see whether …") lists nothing."""
    join = _LIST_JOIN.match(after)
    if not join:
        return False
    rest = after[join.end() :].lstrip("\"“‘'")
    folded = rest.casefold()
    if any(folded.startswith(name) and not folded[len(name) : len(name) + 1].isalnum() for name in names):
        return True
    for raw in rest.split()[:5]:
        word = raw.strip("\"“”‘’'.,;:!?").casefold()
        if _GROUP_NOUN.fullmatch(word):
            return True
        if not (word in _LIST_MODIFIERS or word.endswith("ed") or "-" in word or raw[:1].isupper()):
            return False
    return False


def _name_uses(text: str, ctx: _Context) -> tuple[list[tuple[int, int, str]], str | None]:
    """The spans where `text` uses one of the run's clinical-word names as a name, each marked as a population (_P)
    or as the regimen itself (_N), and the detail of the first mention that is no name use. Longer names are matched
    first and own their span, so a name's label ("<regimen>" inside "<regimen> (ORION)") is never matched again
    inside it. A span takes in the name's " (ORION)" and group word, and a described group's noun and verb."""
    uses: list[tuple[int, int, str]] = []
    for name in sorted(ctx.clinical_names, key=len, reverse=True):
        for match in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.IGNORECASE):
            start, end = match.span()
            if any(start < e and s < end for s, e, _ in uses):
                continue
            whole = bool(_ENDS_AS_NAME.search(match.group(0)))
            for part in (_ORION_AFTER, _GROUP_AFTER):
                extra = part.match(text[end:])
                if extra:
                    end, whole = end + extra.end(), True
            before, after = text[:start], text[end:]
            opening = _OPENING.search(before)
            core = before[: opening.start()] if opening else before
            term = _clinical_term(match.group(0), ctx) or match.group(0)
            advice = f"{term!r} reads as clinical or treatment advice"
            if _TRIAL_DESTINATION.search(core):
                return uses, f"{advice}: the sentence puts a group on the regimen; compare the groups instead"
            described = _DESCRIBED.search(core)
            labels = ctx.clinical_labels.get(name, ())
            if described:
                uses.append((described.start(), end, _P))
                continue
            if any(re.search(_label_before(label), core, re.IGNORECASE) for label in labels):
                uses.append((start, end, _P))
                continue
            words = [w.casefold() for w in re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)*|[.;:?!,(\[—–]", core)]
            while words and words[-1] in _SKIPPED:
                words.pop()
            prev = words[-1] if words else None
            opens_question = len(words) == 1 or (len(words) > 1 and words[-2] in _CLAUSE_MARKS)
            if prev in _POPULATION_WORDS:
                mark = _P
            elif prev is None or prev in _CLAUSE_MARKS or (prev in _QUESTION_OPENERS and opens_question):
                mark = _N
            elif prev in _COPULAS:
                return uses, (f"{advice}: the sentence answers with the regimen ('{prev} <name>'); make the group the "
                              "subject or the population ('in <name>')")
            elif prev in _COHORT_OPERATIONS or (prev not in ("consider", "considering") and _listed_with_groups(after, ctx.names)):
                mark = _P
            elif not whole:
                return uses, (f"{advice}: '{prev}' takes the regimen itself as its object; write the group ('the <name> "
                              "cases') or the population ('in <name>')")
            elif prev in ("consider", "considering"):
                if not _SET_APART.search(_clause(after)):
                    return uses, (f"{advice}: 'consider' offers the regimen unless the group is set apart ('consider the "
                                  "<name> cohort separately')")
                mark = _N
            elif prev in _COHORT_VERBS or prev in _BEFORE_A_NAME:
                mark = _N
            else:
                return uses, (f"{advice}: '{prev}' takes the regimen as its object; write the group as the population "
                              "('in <name>') or compare it")
            uses.append((start, end, mark))
    return uses, None


def _with_marks(text: str, uses: Sequence[tuple[int, int, str]]) -> str:
    out = text
    for start, end, mark in sorted(uses, reverse=True):
        out = f"{out[:start]} {mark} {out[end:]}"
    return out


def _treatment_text_failure(marked: str, ctx: _Context) -> str | None:
    """The descriptive-word check on a text about a treatment (G-5), with its name uses and placeholders marked."""
    text = marked
    for term in sorted({t for t in (*ctx.terms, *ctx.genes) if t.strip()}, key=lambda t: (-len(t), t)):
        mark = f" {_C} " if term.casefold() in ctx.cohort_names else f" {_T} "
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", lambda _m: mark, text, flags=re.IGNORECASE)
    for pattern in ctx.field_values:
        text = pattern.sub(" ", text)
    for label in ctx.label_phrases:
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(label)}(?![A-Za-z0-9])", " ", text, flags=re.IGNORECASE)
    words = [w.group(0).casefold().replace("’", "'") for w in _WORD.finditer(_PHRASES.sub(" ", text))]
    for word in words:
        base = word[:-2] if word.endswith("'s") else word
        if base in DESCRIPTIVE_WORDS or ("-" in base and all(part in DESCRIPTIVE_WORDS for part in base.split("-"))):
            continue
        return (f"{word!r} is not one of the plain descriptive words a sentence about a regimen or a regimen-defined "
                "group may use; describe the group's data (its size, its members, a comparison with a named group), "
                "never how it may fare or what it should get")
    said = _PHRASES.search(text) or any(mark in text for mark in (_F, _C, _T))
    if not said and all(w in FUNCTION_WORDS for w in (m.group(0).casefold() for m in _WORD.finditer(text))):
        return "a regimen named with nothing said about its group reads as a recommendation; write what the data shows"
    opener = _CLAUSE_OPENER.search(text.strip())
    if opener:
        return f"{opener.group(1)!r} opens an instruction in a sentence about a regimen-defined group"
    compared = bool(_OUTCOME.search(text) and _COMPARATIVE.search(text))
    for index, mark in ((i, ch) for i, ch in enumerate(text) if ch in (_N, _P)):
        after = text[index + 1 :]
        timing = _TIMING_NEXT.match(after)
        timed = bool(timing and _OUTCOME.search(text) and _COMPARATIVE.search(text[:index] + after[timing.end() :]))
        if mark == _P:
            if timed:
                return ("a survival comparison by when or how long a group had the regimen judges its timing; compare the "
                        "groups as defined")
            if _ANSWER_NEXT.match(after):
                return "the sentence singles out the regimen's group as the answer ('the ones …'); describe its data instead"
            continue
        offered = _RECIPIENT_NEXT.match(after)
        if offered:
            return (f"the regimen is offered to a group ({offered.group(0).strip()!r}); write it as the population a result "
                    "is read in ('in <name>')")
        sentence = re.split(r"[.;?!]", text[:index])[-1]
        if _RECIPIENT_FIRST.search(sentence) or _TRIED_NEXT.match(after):
            return "the sentence offers the regimen to a group or tries it on one; write it as the population a result is read in"
        if _PRIORITY_NEXT.match(after) or _EFFECT_NEXT.match(after) or _ANSWER_NEXT.match(after) or timed:
            return "the sentence gives the regimen a place, a timing or an effect in care; describe its group's data instead"
    for verb in _WORD.finditer(text):
        if verb.group(0).casefold() not in _TRIAL_VERBS:
            continue
        clause = _clause(text[verb.end() :])
        if re.match(r"\s*(?:at\s+)?(?:whether|if|how|what|which|why|where|when)\b", clause, re.IGNORECASE):
            continue
        recipient = _RECIPIENT.search(clause)
        target = clause[: recipient.start()] if recipient else ""
        pronoun = re.search(r"\b(?:it|them|regimens?)\b|\b(?:this|that|the\s+same)\s+one\b", target, re.IGNORECASE)
        if recipient and (_N in target or pronoun):
            return f"'{verb.group(0)}' tries the regimen on a group ({recipient.group(0)!r}); compare groups instead"
    placed = _MEMBERSHIP.search(text)
    if placed:
        found = placed.group(0).replace(_N, "<name>").replace(_P, "<name>").strip()
        return f"{found!r} places cases on the regimen or changes it, which reads as advice"
    if compared and not _EXPLICITLY_COMPARED.search(text) and (_PLACED_AFTER.search(text) or not _COMPARED_WITH.search(text)):
        return ("a survival comparison of a regimen-defined group names the group it is compared with ('than the "
                "remaining cases'); without one it reads as a benefit claim")
    return None


def _clinical(text: str, ctx: _Context) -> str | None:
    """V-SEC's clinical or treatment finding on `text` (canonical_text), as a detail. A clinical word inside a name the
    run uses is exempt only where the text uses that name as a name (`_name_uses`), and a text that does so, any text
    of a run that names one, and any text naming a treatment noun take the descriptive-word check
    (`_treatment_text_failure`)."""
    marked = PLACEHOLDER.sub(f" {_F} ", text)
    uses: list[tuple[int, int, str]] = []
    if ctx.clinical_names:
        uses, misuse = _name_uses(marked, ctx)
        if misuse:
            return misuse
    rest = _with_marks(marked, uses)
    term = _clinical_term(rest, ctx)
    if term:
        return f"{term!r} reads as clinical or treatment advice"
    if uses or ctx.treatment_run or _TREATMENT_NOUN.search(rest):
        return _treatment_text_failure(rest, ctx)
    return None


def _check_sec(text: str, form: str | None, ctx: _Context) -> str | None:
    clinical = _clinical(text, ctx)
    if clinical:
        return clinical
    term = _causal(text)
    if term:
        return f"{term!r} states a causal or certain claim; hedge it"
    if form == "question" and not text.rstrip().endswith("?"):
        return "a question hypothesis ends with a question mark"
    if form == "action":
        first = next(iter(tokens(without_placeholders(text))), "").casefold()
        hedged = any(w.casefold() in _HEDGES for w in tokens(text))
        if first not in _ACTION_VERBS and not hedged:
            return "an action hypothesis starts with a verb such as Compare or Check, or is hedged"
    return None


def _statement_failure(text: str, cites: Sequence[str] | None, form: str | None, ctx: _Context) -> tuple[VerifierRule, str] | None:
    detail = _check_ref(text, cites, ctx)
    if detail:
        return "V-REF", detail
    text = canonical_text(text)
    stripped = remove_terms(without_placeholders(text), ctx.terms)
    detail = _check_ent(stripped, ctx)
    if detail:
        return "V-ENT", detail
    detail = _check_num(stripped, ctx)
    if detail:
        return "V-NUM", detail
    detail = _check_sec(text, form, ctx)
    if detail:
        return "V-SEC", detail
    return None


def _record(ctx: _Context, part: VerifiedPart, index: int, failure: tuple[VerifierRule, str] | None) -> None:
    failed_rule = failure[0] if failure else None
    for rule in MODEL_RULES:
        if failed_rule is not None and MODEL_RULES.index(rule) > MODEL_RULES.index(failed_rule):
            break  # later rules were not reached
        passed = rule != failed_rule
        detail = failure[1] if failure and not passed else "passed"
        ctx.findings.append(RuleFinding(rule=rule, part=part, index=index, passed=passed, detail=detail))


def _statements(
    items: Sequence[ComposerStatement | ComposerHypothesis], part: VerifiedPart, ctx: _Context
) -> tuple[list[Sentence], bool]:
    sentences: list[Sentence] = []
    failed = False
    for index, item in enumerate(items):
        form = item.form if isinstance(item, ComposerHypothesis) else None
        failure = _statement_failure(item.text, item.cites, form, ctx)
        _record(ctx, part, index, failure)
        if failure:
            failed = True
            sentences.append(Sentence(segments=[], dropped=failure[0]))
        else:
            sentences.append(Sentence(segments=_resolve(item.text, ctx.facts), cites=list(item.cites)))
    return sentences, failed


def _follow_up_failure(item: ComposerFollowUp, ctx: _Context) -> tuple[VerifierRule, str] | None:
    if item.kind not in ANALYSIS_KINDS:
        return "V-ENT", f"{item.kind} is not an analysis kind ORION runs"
    for text in (item.label, item.question):
        detail = _check_ref(text, None, ctx)
        if detail:
            return "V-REF", detail
    texts = (canonical_text(item.label), canonical_text(item.question))
    numeric = [p for text in texts for p in placeholders(text) if not isinstance(ctx.facts[p].value, str)]
    stripped = [remove_terms(without_placeholders(text), [*ctx.terms, *sorted(ctx.genes)]) for text in texts]
    for text in stripped:
        detail = _check_ent(text, ctx)
        if detail:
            return "V-ENT", detail
        unknown = gene_like_tokens(remove_terms(text, [*ctx.acronyms]), ctx.acronyms)
        if unknown:
            return "V-ENT", f"{unknown[0]} is not a panel gene or a gene of this run"
    if numeric:
        return "V-NUM", f"{numeric[0]} puts a number in a follow-up chip, which shows no citation"
    found = sorted({n for text in stripped for n in integers(text)})
    allowed = found if item.kind in TOP_N_KINDS and len(found) == 1 and 1 <= found[0] <= MAX_TOP_N_DE else []
    for text in stripped:
        rest = text
        for n in allowed:
            rest = re.sub(rf"(?<![A-Za-z0-9.]){n}(?![A-Za-z0-9]|\.[0-9])", " ", rest)
        if has_digit(rest):
            return "V-NUM", "a follow-up carries a number other than one top N from 1 to 100"
        words = number_words(rest)
        if words:
            return "V-NUM", f"the number word {words[0]!r} is in a follow-up"
        numerals = roman_numerals(rest)
        if numerals:
            return "V-NUM", f"the numeral {numerals[0]!r} is in a follow-up"
    for text in texts:
        clinical = _clinical(text, ctx)
        if clinical:
            return "V-SEC", clinical
    for text in texts:
        term = _causal(text)
        if term:
            return "V-SEC", f"{term!r} states a causal or certain claim; hedge it"
    return None


def _follow_ups(items: Sequence[ComposerFollowUp], ctx: _Context) -> tuple[list[FollowUp], bool]:
    kept: list[FollowUp] = []
    seen: set[str] = set()
    failed = False
    for index, item in enumerate(items):
        failure = _follow_up_failure(item, ctx)
        _record(ctx, "follow_ups", index, failure)
        if failure:
            failed = True
            continue
        label, question = _plain(item.label, ctx.facts), _plain(item.question, ctx.facts)
        key = question.casefold()
        if key in seen or len(label) > 60 or len(question) > 200:
            continue
        seen.add(key)
        kept.append(FollowUp(label=label, question=question, kind=item.kind))
    return kept, failed


def _open_links(app_ids: Sequence[str]) -> list[NearestApp]:
    """Open <app> links: catalog apps ORION opens on a cohort; driven and out-of-scope apps are left out."""
    links: list[NearestApp] = []
    for app_id in app_ids:
        app = next((a for key, a in APPS.items() if key == app_id), None)
        if app is not None and app.role == "opens" and all(link.app_id != app.app_id for link in links):
            links.append(NearestApp.of(app.app_id))
    return links


# --- V-COMP and V-LIM on Observed and Limitations -------------------------------------------------


def _cites(sentence: Sentence) -> set[str]:
    return {s.fact_id for s in sentence.segments if s.fact_id}


def _text_of(sentence: Sentence) -> str:
    return "".join(s.text for s in sentence.segments)


def _render(
    template_id: str,
    facts: Mapping[str, FactRecord],
    *,
    steps: Mapping[str, str] | None = None,
    values: Mapping[str, str] | None = None,
    items: Sequence[tuple[str, Mapping[str, str]]] = (),
) -> list[Segment] | None:
    """A template's segments, or None when a fact it cites does not exist (the template does not apply)."""
    try:
        parts = schemas.template_parts(schemas.TEMPLATES[template_id], steps=steps, values=values, items=items)
    except (KeyError, ValueError):
        return None
    segments: list[Segment] = []
    for kind, text in parts:
        if kind == "fact":
            fact = facts.get(text)
            if fact is None:
                return None
            segments.append(Segment(text=format_fact(fact.value, fact.unit), fact_id=fact.id))
        else:
            segments.append(Segment(text=text))
    return segments


def _limitation(template_id: str, segments: list[Segment], values: Mapping[str, str] | None = None) -> Limitation:
    template = schemas.TEMPLATES[template_id]
    assert template.code is not None
    text = "".join(s.text for s in segments)
    unblock = schemas.fill_text(template.unblock, values or {}) if template.unblock else None
    has_fact = any(s.fact_id for s in segments)
    if has_fact:
        return Limitation(code=template.code, text=text, unblock=unblock, segments=segments)
    return Limitation(code=template.code, text=text, unblock=unblock)


def _refs(value: JsonValue) -> list[str]:
    if isinstance(value, dict):
        if schemas.is_step_ref(value):
            ref = value.get("ref")
            return [ref] if isinstance(ref, str) else []
        return [r for item in value.values() for r in _refs(item)]
    if isinstance(value, list):
        return [r for item in value for r in _refs(item)]
    return []


def _positions(facts: Mapping[str, FactRecord], step_id: str) -> int:
    n = 0
    while f"f.{step_id}.top.{n + 1}.gene" in facts:
        n += 1
    return n


def _lowered(label: str) -> str:
    return label[:1].lower() + label[1:] if label[:2] != label[:2].upper() else label


@dataclass
class _Observed:
    facts: Mapping[str, FactRecord]
    plan: CompiledPlan
    observed: ObservedResult
    findings: list[RuleFinding] = field(default_factory=list)
    sentences: list[Sentence] = field(default_factory=list)
    limitations: list[Limitation] = field(default_factory=list)

    def cited(self, fact_id: str) -> bool:
        return any(fact_id in _cites(s) for s in [*self.observed.observed, *self.sentences])

    def has_code(self, code: str) -> bool:
        return any(lim.code == code for lim in [*self.observed.limitations, *self.limitations])

    def comp(self, need: str, present: bool, segments: list[Segment] | None) -> None:
        if present:
            self.findings.append(RuleFinding(rule="V-COMP", part="observed", index=None, passed=True, detail=f"{need}: present"))
        elif segments is None:
            detail = f"{need}: missing, and its facts are absent, so no sentence was inserted"
            self.findings.append(RuleFinding(rule="V-COMP", part="observed", index=None, passed=False, detail=detail))
        else:
            self.sentences.append(Sentence(segments=segments))
            self.findings.append(RuleFinding(rule="V-COMP", part="observed", index=None, passed=False, detail=f"{need}: inserted"))

    def lim(self, need: str, code: str, make: Limitation | None) -> None:
        if self.has_code(code):
            self.findings.append(RuleFinding(rule="V-LIM", part="limitations", index=None, passed=True, detail=f"{need}: present"))
        elif make is None:
            detail = f"{need}: missing, and its facts are absent, so no line was inserted"
            self.findings.append(RuleFinding(rule="V-LIM", part="limitations", index=None, passed=False, detail=detail))
        else:
            self.limitations.append(make)
            self.findings.append(RuleFinding(rule="V-LIM", part="limitations", index=None, passed=False, detail=f"{need}: inserted"))

    def steps(self, tool: str) -> list[StepSpec]:
        return [s for s in self.plan.steps if s.tool == tool]

    def step(self, step_id: str) -> StepSpec | None:
        return next((s for s in self.plan.steps if s.id == step_id), None)


def _survival(o: _Observed) -> None:
    for step in o.steps("survival_compare"):
        groups = [g for g in json_list(step.args.get("groups")) if isinstance(g, dict)]
        labels = [str(g.get("label", "")) for g in groups]
        keys = schemas.group_keys(labels) if labels else []
        counts = step.depends_on if len(step.depends_on) == len(groups) else []
        ran = f"f.{step.id}.p" in o.facts or any(f"f.{step.id}.events.{k}" in o.facts for k in keys)
        for i, (label, key) in enumerate(zip(labels, keys)):
            events = f"f.{step.id}.events.{key}"
            if events not in o.facts:
                continue
            segments = None
            if counts:
                segments = _render("survival.group", o.facts, steps={"count": counts[i], "survival": step.id},
                                   values={"group": label, "key": key})
            o.comp(f"J1 {label} cases and events", o.cited(events), segments)
        p_fact = o.facts.get(f"f.{step.id}.p")
        if p_fact is not None:
            o.comp("J1 log-rank p", o.cited(p_fact.id), _render("survival.p", o.facts, steps={"survival": step.id}))
            card = any(v.launcher == "survival" and step.id in v.cites_steps for v in o.observed.viz_specs)
            if card and isinstance(p_fact.value, (int, float)) and p_fact.value < schemas.VIEWER_P_FLOOR:
                o.lim("viewer rounding", "VIEWER_P_ROUNDED", _limitation("survival.viewer_p", _render("survival.viewer_p", o.facts) or []))
        if ran:
            o.lim("no hazard ratio", "STAT_UNAVAILABLE",
                  _limitation("survival.no_hazard_ratio", _render("survival.no_hazard_ratio", o.facts) or []))


def _ranking_source(o: _Observed, ranking: StepSpec) -> StepSpec | None:
    rows = ranking.args.get("rows")
    return o.step(rows) if isinstance(rows, str) else None


def _rank_limits(o: _Observed, ranking: StepSpec, oncomatrix: bool) -> None:
    n_value = ranking.args.get("n")
    n = n_value if isinstance(n_value, int) else None
    shown = _positions(o.facts, ranking.id)
    count = o.facts.get(f"f.{ranking.id}.count")
    if n is None:
        return
    if count is not None and isinstance(count.value, int) and count.value < n:
        if count.value == 0:
            template = "rank.none_genes" if oncomatrix else "rank.none_de"
            o.lim("an empty ranking", "FEWER_THAN_N", _limitation(template, _render(template, o.facts) or []))
        else:
            segments = _render("rank.fewer", o.facts, steps={"ranking": ranking.id}, values={"n": str(n)})
            o.lim("fewer rows than asked", "FEWER_THAN_N", _limitation("rank.fewer", segments) if segments else None)
    if shown > n:
        template = "rank.ties_oncomatrix" if oncomatrix else "rank.ties"
        tied = [("tied", {"pos": str(pos)}) for pos in range(n + 1, shown + 1)]
        segments = _render(template, o.facts, steps={"ranking": ranking.id}, values={"n": str(n)}, items=tied)
        o.lim("ties at the cutoff", "TIES_AT_CUTOFF", _limitation(template, segments) if segments else None)


def _genes(o: _Observed) -> None:
    for ranking in o.steps("rank_top_n"):
        source = _ranking_source(o, ranking)
        if source is None or source.tool != "top_mutated_genes":
            continue
        refs = _refs(source.args)
        count_id = next((f"f.{r}.count" for r in refs if f"f.{r}.count" in o.facts), None)
        if count_id is None and not refs and "f.s1.cases" in o.facts:
            count_id = "f.s1.cases"
        if count_id is not None:
            alone = any(_cites(s) == {count_id} for s in [*o.observed.observed, *o.sentences])
            o.comp("J2 cohort size", alone, _render("genes.cohort", o.facts, steps={"cohort_count": count_id}))
        shown = _positions(o.facts, ranking.id)
        if shown and count_id is not None:
            template = "genes.top" if shown > 1 else "genes.top_one"
            items = [("first" if pos == 1 else "rest", {"pos": str(pos)}) for pos in range(1, shown + 1)]
            segments = _render(template, o.facts, steps={"ranking": ranking.id, "cohort_count": count_id}, items=items)
            o.comp("J2 ranking counts", o.cited(f"f.{ranking.id}.top.1.gene"), segments)
        o.lim("panel coverage", "PANEL_COVERAGE", _limitation("panel.ranking", _render("panel.ranking", o.facts) or []))
        _rank_limits(o, ranking, oncomatrix=True)


def _de_names(o: _Observed, de: StepSpec) -> tuple[str, str] | None:
    for spec in o.observed.viz_specs:
        if spec.launcher in ("deDirect", "deInput") and de.id in spec.cites_steps and spec.pinned is not None:
            groups = getattr(spec.pinned, "groups", None) or []
            names = [getattr(g, "name", None) for g in groups]
            if len(names) == 2 and all(isinstance(n, str) for n in names):
                return str(names[0]), str(names[1])
    groups = [g for g in json_list(de.args.get("groups")) if isinstance(g, dict)]
    names = [g.get("name") for g in groups]
    if len(names) == 2 and all(isinstance(n, str) for n in names):
        return str(names[0]), str(names[1])
    return None


def _de(o: _Observed) -> None:
    for ranking in o.steps("rank_top_n"):
        de = _ranking_source(o, ranking)
        if de is None or de.tool != "de_run":
            continue
        names = _de_names(o, de)
        pre = next((s for s in reversed(o.plan.steps[: o.plan.steps.index(de)]) if s.tool == "de_preanalysis"), None)
        if pre is not None and names is not None and f"f.{pre.id}.eligible.control" in o.facts:
            segments = _render("de.groups", o.facts, steps={"pre": pre.id}, values={"control": names[0], "case": names[1]})
            o.comp("J3 group sizes", o.cited(f"f.{pre.id}.eligible.control"), segments)
        rank_arg = ranking.args.get("rankBy")
        rank_by = next((r for r in schemas.RANK_LABELS if r == rank_arg), None)
        method = de.args.get("method")
        if names is not None and rank_by is not None and isinstance(method, str):
            values = {
                "method": schemas.DE_METHOD_LABELS.get(method, method),
                "rank": schemas.RANK_LABELS[rank_by],
                "control": names[0],
                "case": names[1],
            }
            present = any(_text_of(s).startswith("Method: ") for s in [*o.observed.observed, *o.sentences])
            o.comp("J3 method, ranking rule and direction", present, _render("de.method", o.facts, values=values))
            shown = _positions(o.facts, ranking.id)
            item = schemas.DE_TOP_ITEM.get(rank_by)
            if shown and item:
                items = [(item, {"pos": str(pos)}) for pos in range(1, shown + 1)]
                segments = _render("de.top", o.facts, steps={"ranking": ranking.id}, values={"rank": values["rank"]}, items=items)
                o.comp("J3 top-N table", o.cited(f"f.{ranking.id}.top.1.gene"), segments)
        _rank_limits(o, ranking, oncomatrix=False)


def _panel_genes(o: _Observed) -> None:
    for step in o.steps("gene_cases"):
        gene = step.args.get("gene")
        if isinstance(gene, str):
            o.lim(f"{gene} panel coverage", "PANEL_COVERAGE",
                  _limitation("panel.gene", _render("panel.gene", o.facts, values={"gene": gene}) or []))


def _subsets(o: _Observed) -> None:
    for step in o.steps("check_subset"):
        outside = o.facts.get(f"f.{step.id}.outside")
        if outside is None or not isinstance(outside.value, (int, float)) or outside.value <= 0:
            continue
        inner, outer = step.args.get("inner"), step.args.get("outer")
        inner_fact = o.facts.get(f"f.{inner}.count") if isinstance(inner, str) else None
        outer_fact = o.facts.get(f"f.{outer}.count") if isinstance(outer, str) else None
        segments = None
        if inner_fact is not None and outer_fact is not None:
            values = {"inner": inner_fact.label, "outer": _lowered(outer_fact.label)}
            segments = _render("subset.outside", o.facts, steps={"subset": step.id}, values=values)
        o.lim("cases outside the tested cases", "NOT_SUBSET", _limitation("subset.outside", segments) if segments else None)


_COUNT_TOOLS: Final = frozenset({"cohort_count", "cases_query", "gene_cases", "set_operation"})


def _empties(o: _Observed) -> None:
    for step in o.plan.steps:
        fact = o.facts.get(f"f.{step.id}.count")
        if step.tool in _COUNT_TOOLS and fact is not None and fact.value == 0:
            values = {"filter": step.input_summary or "the plan's filter"}
            segments = _render("empty.cohort", o.facts, values=values) or []
            o.lim(f"{step.id} found no case", "EMPTY_COHORT", _limitation("empty.cohort", segments, values))


def _plan_limitations(o: _Observed) -> None:
    """The compiler's limitations (a NAMED_TOOL_MISMATCH that stays after consent, a fallen-back scope)."""
    for limitation in o.plan.limitations:
        o.lim(f"plan limitation {limitation.code}", limitation.code, limitation)


def check_observed(facts: Sequence[FactRecord], plan: CompiledPlan, observed: ObservedResult) -> tuple[list[RuleFinding], list[Sentence], list[Limitation]]:
    """V-COMP and V-LIM: what code inserts into Observed and Limitations, with one finding per need."""
    o = _Observed(facts={f.id: f for f in facts}, plan=plan, observed=observed)
    _plan_limitations(o)
    _survival(o)
    _genes(o)
    _de(o)
    _panel_genes(o)
    _subsets(o)
    _empties(o)
    return o.findings, o.sentences, o.limitations


def _run_words(plan: CompiledPlan, ledger: SessionLedger | None) -> str:
    """The run's own words, case-folded: its scope, its and the ledger's cohort names, and the plan's settings."""
    texts = [plan.scope.name, *plan_cohort_names(plan), *ledger_cohort_names(ledger)]
    for editable in plan.editable:
        value: object = editable.value
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        texts += [str(v) for v in value.values()] if isinstance(value, dict) else [str(value)]
    return "\n".join(texts).casefold()


# --- The verifier ---------------------------------------------------------------------------------


class CodeVerifier:
    """interfaces.Verifier: code rules only, no model call."""

    def treatment_defined(
        self, facts: Sequence[FactRecord], ledger: SessionLedger | None, vocab: Vocabulary, *,
        plan: CompiledPlan, observed: ObservedResult, allow_list: Sequence[str],
    ) -> bool:
        """The run itself names a treatment (a regimen value or a clinical name) in its scope, cohorts or settings.

        Skeptic rounds showed that, for such a run, advice can be written in descriptive words alone ("<regimen>
        could be the plan for the mutated cases", "Match the mutated cases to it"), so the answer path withholds
        model text for these runs entirely instead of trying to tell description from advice word by word."""
        return self._context(facts, ledger, vocab, plan, observed, allow_list).treatment_run

    def allow_list(self, vocab: Vocabulary, ledger: SessionLedger | None, plan: CompiledPlan) -> list[str]:
        """Terms model text may write outside placeholders (V-NUM): ORION's terms with digits
        (log2FC, Gen3 Analysis, six-gene panel), app names, the vocabulary's terms with numerals or
        upper-case codes ("Stage II"), and the run's and the ledger's cohort names. A vocabulary term
        with a digit ("t(4;14): Present") only when the run itself names it, in its scope, a cohort
        name or a plan setting: the contract ("Numbers on screen") allows a digit outside a fact chip
        only in the run's own words, and the evals' cited-numbers gate counts any other one."""
        named = _run_words(plan, ledger)
        terms = [
            *FIXED_TERMS,
            *app_titles(),
            *(term for term in vocabulary_terms(vocab) if not has_digit(term) or term.casefold() in named),
            *sorted(vocabulary_acronyms(vocab)),
            *_with_labels(plan_cohort_names(plan)),
            *_with_labels(ledger_cohort_names(ledger)),
        ]
        genes = {g.casefold() for g in panel_genes(vocab)}
        unique: dict[str, str] = {}
        for term in terms:
            text = term.strip()
            # A bare gene symbol or number never becomes an allowed term, even as a cohort name.
            if text and text.casefold() not in genes and not re.fullmatch(r"[0-9.,\s]+", text):
                unique.setdefault(text.casefold(), text)
        return sorted(unique.values(), key=str.casefold)

    def _context(
        self, facts: Sequence[FactRecord], ledger: SessionLedger | None, vocab: Vocabulary, plan: CompiledPlan,
        observed: ObservedResult, allow_list: Sequence[str],
    ) -> _Context:
        by_id = {f.id: f for f in facts}
        viz = _with_labels(_viz_names(observed))
        terms = [canonical_text(t) for t in [*allow_list, *viz]]

        def folded(items: Iterable[str]) -> set[str]:
            return {canonical_text(n).casefold() for n in items}

        plan_own = folded([*_with_labels(plan_cohort_names(plan)), *viz])
        cohorts = plan_own | folded(_with_labels(ledger_cohort_names(ledger)))
        own = cohorts | folded(app_titles())
        names = set(own)
        labels: dict[str, set[str]] = {}
        regimen_values: set[str] = set()
        label_phrases: list[str] = []
        field_values: list[re.Pattern[str]] = []
        for spec in vocabulary_fields(vocab):
            label = canonical_text(spec.label).casefold()
            values = [canonical_text(str(v)).casefold() for v in spec.values or ()]
            names.add(label)
            for value in values:
                names.add(value)
                labels.setdefault(value, set()).add(label)
            if "regimen" in spec.id.rsplit(".", 1)[-1]:
                regimen_values.update(values)
            if _OUTCOME_LABEL.search(label):
                continue
            if len(label.split()) > 1:
                label_phrases.append(label)
            if values and spec.id not in GENE_FIELDS:
                # "cases whose reason for discontinuation is adverse event": a subgroup named by a field's value.
                alternatives = "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))
                field_values.append(re.compile(
                    rf"\b(?:whose|with|where|by)\s+(?:(?:a|an|the|their|its)\s+)?{re.escape(label)}\s*(?:(?:is|was|of|=|:)\s*)?"
                    rf"[\"“‘']?(?:{alternatives})(?![A-Za-z0-9])",
                    re.IGNORECASE,
                ))
        # A clinical word is exempt only inside a name this run uses: its own names, or a vocabulary label or
        # value its scope, cohorts or settings name (as the allow-list admits a digit term), never every value.
        named = canonical_text(_run_words(plan, ledger))
        plan_named = canonical_text(_run_words(plan, None))
        clinical_names = tuple(sorted(
            n for n in names if (_CLINICAL.search(n) or n in regimen_values) and (n in own or n in named)
        ))
        clinical_labels = {n: tuple(sorted(labels[n])) for n in clinical_names if n in labels}
        gene_facts = {str(f.value) for f in facts if f.unit == "gene" and isinstance(f.value, str)}
        name_genes = {t for n in [*plan_cohort_names(plan), *ledger_cohort_names(ledger), *viz] for t in gene_like_tokens(n)}
        genes = frozenset({*panel_genes(vocab), *gene_facts, *name_genes})
        return _Context(
            facts=by_id, terms=terms, acronyms=ACRONYMS | vocabulary_acronyms(vocab), names=frozenset(names), genes=genes,
            panel=frozenset(panel_genes(vocab)), clinical_names=clinical_names, clinical_labels=clinical_labels,
            treatment_run=any(n in plan_own or n in plan_named for n in clinical_names),
            regimen_values=tuple(sorted(regimen_values, key=len, reverse=True)), cohort_names=frozenset(cohorts),
            label_phrases=tuple(sorted(label_phrases, key=len, reverse=True)), field_values=tuple(field_values),
        )

    def verify(
        self,
        composer_output: ComposerOutput,
        facts: Sequence[FactRecord],
        ledger: SessionLedger | None,
        vocab: Vocabulary,
        *,
        plan: CompiledPlan,
        observed: ObservedResult,
        attempt: int,
        allow_list: Sequence[str],
    ) -> VerifierResult:
        ctx = self._context(facts, ledger, vocab, plan, observed, allow_list)
        interpretation, bad_i = _statements(composer_output.interpretation, "interpretation", ctx)
        hypotheses, bad_h = _statements(composer_output.hypotheses, "hypotheses", ctx)
        follow_ups, bad_f = _follow_ups(composer_output.follow_ups, ctx)
        open_links = _open_links(composer_output.open_app_ids)
        code_findings, inserted_observed, inserted_limitations = check_observed(facts, plan, observed)
        model_failed = bad_i or bad_h or bad_f
        return VerifierResult(
            passed=not model_failed and not inserted_observed and not inserted_limitations,
            retry=model_failed and attempt <= 1,
            findings=ctx.findings + code_findings,
            interpretation=interpretation,
            hypotheses=hypotheses,
            follow_ups=follow_ups,
            open_links=open_links,
            inserted_observed=inserted_observed,
            inserted_limitations=inserted_limitations,
            observed_only=not any(s.dropped is None for s in interpretation),
        )


def model_text_feedback(result: VerifierResult) -> list[RuleFinding]:
    """The failed model-text findings, for the one re-compose."""
    return [f for f in result.findings if not f.passed and f.part in ("interpretation", "hypotheses", "follow_ups")]
