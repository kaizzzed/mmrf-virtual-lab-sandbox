"""Settings, app wiring, the S3 record store, the session ledger and the answer markdown."""

import io
import os
import unittest
from types import ModuleType
from typing import Any
from unittest import mock

from support import FIELDS_PATH, ask_body
from test_engine_fakes import J3_QUESTION, fixture_frames, j3_ir
from test_engine_streams import EngineCase

from orion_agent.app import StartupError, build_controller
from orion_agent.executor import Engine
from orion_agent.ids import new_id
from orion_agent.ledger import StoreLedger
from orion_agent.models import RunView
from orion_agent.observed import answer_markdown
from orion_agent.run_store import RunStore, S3RunStore, ThreadIndex
from orion_agent.settings import Settings
from orion_agent.stubs import StubExecutor, StubPlanner


class FakeS3:
    """boto3's S3 client, as far as S3RunStore uses it."""

    class NoSuchKey(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, **kwargs: Any) -> Any:  # Any: boto3's shapes
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise FakeS3.NoSuchKey()
        return {"Body": io.BytesIO(self.objects[key])}

    def put_object(self, **kwargs: Any) -> Any:
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]
        return {}


def importer(missing: str | None = None) -> Any:
    """An importer whose model and tools factories are stand-ins; `missing` raises ModuleNotFoundError."""

    def load(name: str) -> ModuleType:
        if name == missing:
            raise ModuleNotFoundError(f"No module named {name!r}")
        module = ModuleType(name)
        if name.endswith("model.factory"):
            module.make_planner = lambda settings: StubPlanner()  # type: ignore[attr-defined]
            module.make_composer = lambda settings: None  # type: ignore[attr-defined]
            module.make_verifier = lambda settings: None  # type: ignore[attr-defined]
        else:
            from test_engine_fakes import FakeTools

            module.make_tool_client = lambda settings: FakeTools()  # type: ignore[attr-defined]
        return module

    return load


class SettingsTests(unittest.TestCase):
    def test_role_tool_and_store_settings_come_from_the_environment(self) -> None:
        env = {
            "ORION_PLANNER": "bedrock", "ORION_COMPOSER": "bedrock", "ORION_EXECUTOR": "stub", "ORION_TOOLS": "gateway",
            "ORION_DE_LAUNCHER": "deInput", "ORION_MODE": "A", "ORION_RUN_BUCKET": "orion-runs", "ORION_RUN_PREFIX": "p/",
            "ORION_STOP_GRACE_S": "2.5",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            settings = Settings.from_env()
        self.assertEqual(
            (settings.planner, settings.composer, settings.executor, settings.tools, settings.de_launcher, settings.mode, settings.run_bucket,
             settings.run_prefix, settings.stop_grace_s, settings.tool_transport),
            ("bedrock", "bedrock", "stub", "gateway", "deInput", "A", "orion-runs", "p/", 2.5, "gateway"),
        )
        with mock.patch.dict(os.environ, {"ORION_TOOLS": "carrier-pigeon"}, clear=False), self.assertRaises(ValueError):
            Settings.from_env()


class WiringTests(unittest.TestCase):
    def test_build_controller_wires_the_engine_over_the_tool_client(self) -> None:
        controller = build_controller(Settings(run_dir=FIELDS_PATH.parent, fields_path=FIELDS_PATH), importer=importer())
        self.assertIsInstance(controller.executor, Engine)
        self.assertIsInstance(controller.planner, StubPlanner)
        self.assertIsInstance(controller.ledger_store, StoreLedger)
        stub = build_controller(Settings(fields_path=FIELDS_PATH, executor="stub"), importer=importer("orion_agent.tools.client"))
        self.assertIsInstance(stub.executor, StubExecutor)

    def test_a_missing_factory_fails_startup_naming_the_setting(self) -> None:
        with self.assertRaisesRegex(StartupError, "ORION_PLANNER=stub / ORION_COMPOSER=stub needs orion_agent.model.factory"):
            build_controller(Settings(fields_path=FIELDS_PATH), importer=importer("orion_agent.model.factory"))
        with self.assertRaisesRegex(StartupError, "ORION_EXECUTOR=engine with ORION_TOOLS=inprocess needs orion_agent.tools.client"):
            build_controller(Settings(fields_path=FIELDS_PATH), importer=importer("orion_agent.tools.client"))
        with self.assertRaisesRegex(StartupError, "ORION_RUN_BUCKET"):
            build_controller(Settings(fields_path=FIELDS_PATH, mode="A"), importer=importer())

    def test_modes_a_and_h_keep_records_in_s3(self) -> None:
        client = FakeS3()
        controller = build_controller(Settings(fields_path=FIELDS_PATH, mode="H", run_bucket="b"), importer=importer(), s3_client=client)
        self.assertIsInstance(controller.store, S3RunStore)


class S3StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_and_indexes_round_trip_under_the_prefix(self) -> None:
        client = FakeS3()
        store = S3RunStore(client, "bucket", "orion/")
        thread_id = new_id()
        self.assertIsNone(await store.load_run(new_id()))
        self.assertEqual((await store.load_thread(thread_id)).run_ids, [])
        await store.save_thread(ThreadIndex(thread_id=thread_id, run_ids=[new_id()]))
        self.assertIn(("bucket", f"orion/threads/{thread_id}.json"), client.objects)
        self.assertEqual(len((await store.load_thread(thread_id)).run_ids), 1)


class LedgerTests(EngineCase):
    async def test_the_ledger_lists_earlier_runs_from_their_records(self) -> None:
        _, app = self.build([j3_ir()])
        thread_id, first = new_id(), new_id()
        await self.turn(app, ask_body(thread_id, first, question=J3_QUESTION), first)
        second = new_id()
        ledger = await StoreLedger(RunStore(self.run_dir)).ledger(thread_id, before_run_id=second)
        self.assertEqual([r.run_id for r in ledger.runs], [first])
        run = ledger.runs[0]
        self.assertEqual((run.status, run.analyses, run.scope.name), ("AwaitingConfirmation", ["differential_expression"], "All cases"))
        self.assertEqual([c.ref for c in run.cohorts], ["tested", "kras_mut", "kras_wt"])
        mutated = next(c for c in run.cohorts if c.ref == "kras_mut")
        self.assertEqual((mutated.clauses[0].field, len(mutated.clauses[0].values)), ("case_id", 480))
        self.assertIn("KRAS-mutated cases", run.fact_labels)
        self.assertEqual((await StoreLedger(RunStore(self.run_dir)).ledger(thread_id, before_run_id=first)).runs, [])


class MarkdownTests(unittest.TestCase):
    def test_the_answer_markdown_matches_the_fixtures_text_message(self) -> None:
        for name in ("j1-complete", "j3-after-confirm", "oncomatrix"):
            with self.subTest(name):
                frames = fixture_frames(name)
                view = RunView.model_validate([f for f in frames if f["type"] == "STATE_SNAPSHOT"][-1]["snapshot"])
                text = "".join(f["delta"] for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT")
                self.assertEqual(
                    answer_markdown(view.observed, view.interpretation, view.hypotheses, view.withheld_reason, view.limitations, view.notice), text
                )
