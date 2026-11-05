"""The composer role on Bedrock (RFA rev 25, A2 "Agent topology", A4).

Claude Opus 5.5 at effort medium writes Interpretation and Hypotheses and next steps through
structured output (`interfaces.ComposerOutput`), naming numbers and gene symbols only through
fact placeholders. The prompt carries the verifier's allow-list and, on the one re-compose, the
verifier's failed findings.

The schema the API takes has no length or count constraints, so the reply is clipped to the
caps of `interfaces.py` (at most 4 interpretation and 3 hypothesis statements of 300 characters,
3 follow-ups with labels of 60 and questions of 200 characters, 3 apps) before validation: an
over-long item is dropped, not the whole answer. Both models declining, or a reply that still
does not validate, is a decline: the answer is Observed only (E11, MODEL_DECLINED). Any other
failure raises ModelRoleError carrying every call made.
"""

import json
import re
import time
from collections.abc import Sequence
from typing import Final

from pydantic import JsonValue

from ..compiler import CompiledPlan, SessionLedger
from ..interfaces import ComposerOutput, ComposerResult, ObservedResult, RuleFinding
from ..models import FactRecord
from .client import CallResult, ModelClient, ModelRequest
from .config import WARMUP_MAX_TOKENS, ModelConfig
from .lexicon import model_display_name
from .prompts import composer_system_prefix, composer_user_message
from .schema import composer_output_schema
from .warmup import WarmUpResult, warm_up_result

_FACT_ID: Final = re.compile(r"^f\.s[1-9][0-9]*(\.[A-Za-z0-9_-]+)+$")
MAX_TEXT: Final = 300
MAX_LABEL: Final = 60
MAX_QUESTION: Final = 200
CAPS: Final[dict[str, int]] = {"interpretation": 4, "hypotheses": 3, "follow_ups": 3, "open_app_ids": 3}


def _statement(item: JsonValue, *, hypothesis: bool) -> dict[str, JsonValue] | None:
    if not isinstance(item, dict):
        return None
    text, cites = item.get("text"), item.get("cites")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or not isinstance(cites, list):
        return None
    kept: list[JsonValue] = [c for c in dict.fromkeys(c for c in cites if isinstance(c, str) and _FACT_ID.match(c))]
    if not kept:
        return None
    out: dict[str, JsonValue] = {"text": text.strip(), "cites": kept}
    if hypothesis:
        out["form"] = item.get("form")
    return out


def clip_composer_output(data: JsonValue) -> dict[str, JsonValue]:
    """The reply cut to the caps: items over a length limit or citing nothing are dropped."""
    if not isinstance(data, dict):
        raise ValueError("the reply is not a JSON object")
    out: dict[str, JsonValue] = {}
    for key, hypothesis in (("interpretation", False), ("hypotheses", True)):
        raw = data.get(key)
        items = [s for s in (_statement(i, hypothesis=hypothesis) for i in (raw if isinstance(raw, list) else [])) if s]
        out[key] = list(items[: CAPS[key]])
    follow_ups: list[JsonValue] = []
    raw_follow = data.get("follow_ups")
    for item in raw_follow if isinstance(raw_follow, list) else []:
        if not isinstance(item, dict):
            continue
        label, question = item.get("label"), item.get("question")
        if isinstance(label, str) and isinstance(question, str) and 0 < len(label) <= MAX_LABEL and 0 < len(question) <= MAX_QUESTION:
            follow_ups.append({"kind": item.get("kind"), "label": label, "question": question})
    out["follow_ups"] = follow_ups[: CAPS["follow_ups"]]
    apps = data.get("open_app_ids")
    app_ids: list[JsonValue] = [a for a in dict.fromkeys(a for a in (apps if isinstance(apps, list) else []) if isinstance(a, str))]
    out["open_app_ids"] = app_ids[: CAPS["open_app_ids"]]
    return out


def parse_composer_reply(text: str) -> ComposerOutput:
    return ComposerOutput.model_validate(clip_composer_output(json.loads(text)))


def decline_reason(result: CallResult[ComposerOutput]) -> str:
    """The withheld line's reason, naming the models that declined (A3 edge cases)."""
    models = [model_display_name(m) for m in result.declined_models]
    if len(models) >= 2:
        return f"{models[0]} and its fallback, {models[-1]}, declined to interpret these results"
    if models:
        return f"{models[0]} declined to interpret these results"
    return "the model declined to interpret these results"


class BedrockComposer:
    """interfaces.Composer on Bedrock. Run its calls inside `calls.model_call_scope(run_id, ...)`."""

    def __init__(self, client: ModelClient, config: ModelConfig) -> None:
        self._client = client
        self._config = config
        self._prefix = composer_system_prefix()
        self._schema = composer_output_schema()

    @property
    def system_prefix(self) -> str:
        return self._prefix

    def _request(self, user_text: str, plan_version: int | None, *, max_tokens: int | None = None) -> ModelRequest:
        return ModelRequest(
            role="composer",
            model=self._config.composer_model,
            effort=self._config.composer_effort,
            system_prefix=self._prefix,
            user_text=user_text,
            schema=self._schema,
            max_tokens=max_tokens or self._config.max_tokens,
            timeout_s=self._config.composer_timeout_s,
            cache_ttl=self._config.cache_ttl,
            plan_version=plan_version,
        )

    async def compose(
        self,
        question: str,
        facts: Sequence[FactRecord],
        plan: CompiledPlan,
        observed: ObservedResult,
        *,
        allow_list: Sequence[str],
        ledger: SessionLedger | None,
        feedback: Sequence[RuleFinding] = (),
    ) -> ComposerResult:
        user = composer_user_message(question, facts, plan, observed, allow_list=allow_list, ledger=ledger, feedback=feedback)
        result = await self._client.call(self._request(user, plan.version), parse_composer_reply)
        if result.refused:
            return ComposerResult(
                output=None, model_calls=result.records, serving_model=None, declined=True, decline_reason=decline_reason(result)
            )
        if result.value is None:
            return ComposerResult(
                output=None, model_calls=result.records, serving_model=result.served_model, declined=True,
                decline_reason="the interpretation did not match its schema",
            )
        return ComposerResult(
            output=result.value, model_calls=result.records, serving_model=result.served_model, declined=False, decline_reason=None
        )

    async def warm_up(self) -> WarmUpResult:
        """A small real call at the composer's effort, writing the cached prefix (C-41). Not recorded."""
        data: dict[str, JsonValue] = {
            "question": "Warm-up call: how many cases are in the active cohort?",
            "facts": [{"id": "f.s1.cases", "label": "Cases in the sandbox", "value": "1,000", "unit": "cases"}],
            "observed": ["The scope has {{f.s1.cases}} cases."],
            "allowedTerms": [],
        }
        user = "<request>\n" + json.dumps(data, ensure_ascii=False, indent=1) + "\n</request>\nAnswer with the JSON object only."
        started = time.monotonic()
        return await warm_up_result(
            "composer", self._config.composer_model, self._config.composer_effort, started,
            self._client.call(self._request(user, None, max_tokens=WARMUP_MAX_TOKENS), parse_composer_reply, record=False),
        )
