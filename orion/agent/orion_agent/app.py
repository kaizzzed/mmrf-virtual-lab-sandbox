"""AgentCore AG-UI entry point: POST /invocations (SSE), GET /ping and /ws on port 8080."""

import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing

from ag_ui.core import BaseEvent, RunAgentInput
from bedrock_agentcore.runtime import AGUIApp, PingStatus
from bedrock_agentcore.runtime.context import RequestContext

from .compiler import Compiler
from .controller import Controller
from .run_store import RunStore
from .settings import Settings
from .stubs import StubExecutor, StubPlanner
from .vocab import Vocabulary

log = logging.getLogger("orion.app")


def build_controller(settings: Settings) -> Controller:
    vocab = Vocabulary.load(settings.fields_path)
    return Controller(
        settings=settings,
        store=RunStore(settings.run_dir),
        compiler=Compiler(vocab, settings.default_scope),
        planner=StubPlanner(),
        executor=StubExecutor(),
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
    app = create_app(build_controller(settings))
    app.run(port=settings.port)


if __name__ == "__main__":
    main()
