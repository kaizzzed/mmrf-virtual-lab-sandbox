"""AgentCore AG-UI entry point: POST /invocations (SSE), GET /ping and /ws on port 8080.

`build_controller` wires the run controller from settings (orion/agent/README.md):

- ORION_MODE: L keeps records under ORION_RUN_DIR; A and H write them to S3 (ORION_RUN_BUCKET,
  ORION_RUN_PREFIX), the same store the session ledger reads.
- ORION_PLANNER, ORION_COMPOSER (stub | bedrock): `orion_agent.model.factory.make_planner(settings)`,
  `make_composer(settings)` and `make_verifier(settings)`.
- ORION_EXECUTOR (engine | stub) and ORION_TOOLS (inprocess | gateway): the step engine over
  `orion_agent.tools.client.make_tool_client(settings)`.

The factories are imported only when the controller is built, never at import time, so a missing
module fails startup with a message naming the setting that needs it.
"""

import importlib
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from types import ModuleType
from typing import Any

from ag_ui.core import BaseEvent, RunAgentInput
from bedrock_agentcore.runtime import AGUIApp, PingStatus
from bedrock_agentcore.runtime.context import RequestContext

from .compiler import Compiler
from .controller import Controller
from .executor import Engine
from .interfaces import Composer, Executor, Planner, Verifier
from .ledger import StoreLedger
from .observed import TemplateRenderer
from .run_store import RecordStore, RunStore, S3Client, S3RunStore
from .settings import Settings
from .stubs import StubExecutor
from .vocab import Vocabulary

log = logging.getLogger("orion.app")

TOOLS_FACTORY = "orion_agent.tools.client"
MODEL_FACTORY = "orion_agent.model.factory"
Importer = Callable[[str], ModuleType]


class StartupError(RuntimeError):
    """The settings ask for a part this image does not have; the message names the setting."""


def _factory(importer: Importer, module: str, name: str, setting: str) -> Callable[[Settings], Any]:  # Any: the factory's own type
    try:
        loaded = importer(module)
    except ModuleNotFoundError as error:
        raise StartupError(f"{setting} needs {module}.{name}, but {module} is not installed: {error}") from error
    factory = getattr(loaded, name, None)
    if not callable(factory):
        raise StartupError(f"{setting} needs {module}.{name}, which {module} does not define")
    return factory  # type: ignore[no-any-return]


def build_store(settings: Settings, *, s3_client: S3Client | None = None) -> RecordStore:
    if settings.mode == "L":
        return RunStore(settings.run_dir)
    if not settings.run_bucket:
        raise StartupError(f"ORION_MODE={settings.mode} writes run records to S3: set ORION_RUN_BUCKET")
    if s3_client is None:
        import boto3  # type: ignore[import-untyped]  # only in modes A and H, at startup

        s3_client = boto3.client("s3", region_name=settings.aws_region)
    return S3RunStore(s3_client, settings.run_bucket, settings.run_prefix)


def build_controller(
    settings: Settings, *, importer: Importer = importlib.import_module, s3_client: S3Client | None = None
) -> Controller:
    vocab = Vocabulary.load(settings.fields_path)
    store = build_store(settings, s3_client=s3_client)
    model_setting = f"ORION_PLANNER={settings.planner} / ORION_COMPOSER={settings.composer}"
    planner: Planner = _factory(importer, MODEL_FACTORY, "make_planner", model_setting)(settings)
    composer: Composer = _factory(importer, MODEL_FACTORY, "make_composer", model_setting)(settings)
    verifier: Verifier = _factory(importer, MODEL_FACTORY, "make_verifier", model_setting)(settings)
    if settings.executor == "stub":
        executor: Executor = StubExecutor()
    else:
        tools = _factory(importer, TOOLS_FACTORY, "make_tool_client", f"ORION_EXECUTOR=engine with ORION_TOOLS={settings.tools}")(settings)
        executor = Engine(tools, vocab=vocab)
    log.info(
        "controller: mode %s, planner %s, composer %s, executor %s, tools %s, records %s",
        settings.mode, settings.planner, settings.composer, settings.executor, settings.tools, type(store).__name__,
    )
    return Controller(
        settings=settings,
        store=store,
        compiler=Compiler(vocab, settings.default_scope),
        planner=planner,
        executor=executor,
        composer=composer,
        verifier=verifier,
        renderer=TemplateRenderer(vocab, de_launcher=settings.de_launcher),
        ledger_store=StoreLedger(store),
        vocab=vocab,
    )


def create_app(controller: Controller) -> AGUIApp:
    app = AGUIApp()

    # AGUIApp passes the RequestContext only when the second parameter is named exactly `context`.
    async def invoke(input_data: RunAgentInput, context: RequestContext) -> AsyncGenerator[BaseEvent, None]:
        log.debug("invocation thread %s session %s", input_data.thread_id, context.session_id)
        async with aclosing(controller.handle(input_data)) as stream:
            async for event in stream:
                yield event

    # Must stay synchronous: AGUIApp calls it without awaiting and reads `.value`.
    def ping() -> PingStatus:
        return PingStatus.HEALTHY_BUSY if controller.busy() else PingStatus.HEALTHY

    app.entrypoint(invoke)
    app.ping(ping)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    try:
        controller = build_controller(settings)
    except StartupError as error:
        log.error("ORION cannot start: %s", error)
        raise SystemExit(2) from error
    create_app(controller).run(port=settings.port)


if __name__ == "__main__":
    main()
