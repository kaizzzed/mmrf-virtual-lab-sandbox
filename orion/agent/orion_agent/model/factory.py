"""Builds the model roles from the runtime settings and the environment (model/config.py).

    planner = make_planner(settings)      # settings.planner (ORION_PLANNER): stub (orion_agent.stubs.StubPlanner) or bedrock
    composer = make_composer(settings)    # settings.composer (ORION_COMPOSER): stub (StubComposer) or bedrock
    verifier = make_verifier(settings)    # always code (CodeVerifier)

The role kinds come from `settings` (what app.py logs and the run record's environment names);
the Bedrock details (client, region, model ids, efforts, timeouts) from ModelConfig.from_env().
A `config` passed explicitly is used as it is, roles included (the tests build their own).

Fault injection (contract "Fault injection", E11): `planner:refuse` or `composer:refuse` in
ORION_FAULTS (settings.faults, honoured only in mode L or a test Runtime) makes that role answer
as if both models had refused, with no model call: the planner declines (the run ends Failed,
PLANNER_DECLINED) and the composer declines (the answer is Observed only).

The Bedrock roles are imported only when configured, so stub mode needs no Anthropic SDK.
"""

from collections.abc import Sequence
from typing import Final

from ..compiler import CompiledPlan, SessionLedger
from ..interfaces import Clarification, Composer, ComposerResult, ObservedResult, Planner, PlannerResult, RuleFinding, Verifier
from ..models import CohortContext, FactRecord
from ..settings import Settings
from ..vocab import Vocabulary
from .config import ModelConfig
from .stub_composer import StubComposer
from .verifier import CodeVerifier

FAULT_PLANNER_REASON: Final = "Both models declined to plan this question (injected fault planner:refuse)."
FAULT_COMPOSER_REASON: Final = "both models declined to interpret these results (injected fault composer:refuse)"


class RefusingPlanner:
    """`planner:refuse`: declines every plan, as a refusal from both models would, with no call."""

    async def plan(
        self,
        question: str,
        cohort_context: CohortContext,
        ledger: SessionLedger | None,
        *,
        clarification: Clarification | None = None,
    ) -> PlannerResult:
        return PlannerResult(
            output=None, model_calls=[], serving_model=None, declined=True, decline_reason=FAULT_PLANNER_REASON, repaired=False
        )


class RefusingComposer:
    """`composer:refuse`: declines every answer, as a refusal from both models would, with no call."""

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
        return ComposerResult(output=None, model_calls=[], serving_model=None, declined=True, decline_reason=FAULT_COMPOSER_REASON)


def _config(settings: Settings, config: ModelConfig | None) -> ModelConfig:
    """`config` as given, or the environment's with the role kinds `settings` chose."""
    if config is not None:
        return config
    return ModelConfig.from_env().model_copy(update={"planner": settings.planner, "composer": settings.composer})


def make_planner(settings: Settings, *, config: ModelConfig | None = None, vocab: Vocabulary | None = None) -> Planner:
    if settings.faults.get("planner") == "refuse":
        return RefusingPlanner()
    resolved = _config(settings, config)
    if resolved.planner == "stub":
        from ..stubs import StubPlanner  # the compiler builder's stub, imported lazily

        return StubPlanner()
    from .client import shared_client
    from .planner import BedrockPlanner

    return BedrockPlanner(shared_client(resolved), resolved, vocab if vocab is not None else Vocabulary.load(settings.fields_path))


def make_composer(settings: Settings, *, config: ModelConfig | None = None) -> Composer:
    if settings.faults.get("composer") == "refuse":
        return RefusingComposer()
    resolved = _config(settings, config)
    if resolved.composer == "stub":
        return StubComposer(effort=resolved.composer_effort)
    from .client import shared_client
    from .composer import BedrockComposer

    return BedrockComposer(shared_client(resolved), resolved)


def make_verifier(settings: Settings) -> Verifier:
    """The verifier is code in every mode; `settings` is taken for symmetry with the other roles."""
    del settings
    return CodeVerifier()
