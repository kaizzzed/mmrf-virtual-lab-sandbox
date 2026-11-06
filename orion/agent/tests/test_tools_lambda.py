"""The Gateway Lambda target: tool name from the Gateway context, the same handlers, the Secrets
Manager credential (injected, never called), the Lambda timeout and the response cap; and the
package the build script makes."""

import hashlib
import importlib.util
import json
import logging
import platform
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

from test_tools_fakes import FakeTransport, fake_stack, goldens

from orion_agent.tools.base import TOOL_HTTP_TIMEOUT_S
from orion_agent.tools.credentials import SecretsManagerCredential

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "lambda"
PASSWORD = "q" * 32  # a synthetic test value
ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:orion/vlab-basic-AbCdEf"
HIGH: dict[str, Any] = {"and": [{"in": {"cgs_risk_key_criteria.cgs_risk_category": ["High risk"]}}]}


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"orion_lambda_{name}", LAMBDA_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handler = load("handler")


def context(tool: str | None, remaining_ms: int = 58_000, request_id: str = "req-1") -> SimpleNamespace:
    custom = {"bedrockAgentCoreToolName": tool, "bedrockAgentCoreAwsRequestId": "gw-1"} if tool else {}
    return SimpleNamespace(
        aws_request_id=request_id, client_context=SimpleNamespace(custom=custom), get_remaining_time_in_millis=lambda: remaining_ms
    )


class HandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = fake_stack()
        handler.configure(handler.Runtime.build("http://identity:8080", handler.NoCredential(), transport=self.transport, goldens=goldens()))

    def tearDown(self) -> None:
        handler.configure(None)

    def test_the_target_prefix_is_stripped_from_the_gateway_tool_name(self) -> None:
        self.assertEqual(handler.tool_name(context("vlab___cohort_count")), "cohort_count")
        self.assertIsNone(handler.tool_name(context("kg___kg_query")))
        self.assertIsNone(handler.tool_name(context("cohort_count")))
        self.assertIsNone(handler.tool_name(SimpleNamespace(client_context=None)))

    def test_a_tool_call_returns_the_tool_result_json(self) -> None:
        result = handler.handler({"filter": HIGH}, context("vlab___cohort_count"))
        self.assertEqual((result["status"], result["observed"], result["error"]), ("ok", {"count": 7}, None))
        self.assertEqual(result["provenance"][0]["path"], "/guppy/graphql")
        self.assertEqual(set(result), {"status", "observed", "warnings", "limitation", "provenance", "error"})
        json.dumps(result)

    def test_every_vlab_tool_is_served_and_nothing_else(self) -> None:
        self.assertEqual(len(handler.VLAB_TOOLS), 9)
        for name in ("kg_query", "set_operation", "save_cohorts", "nope"):
            result = handler.handler({}, context(f"vlab___{name}"))
            self.assertEqual((result["status"], result["error"]["code"]), ("error", "INVALID_INPUT"), name)
        self.assertEqual(handler.handler({}, context(None))["error"]["code"], "INVALID_INPUT")
        self.assertEqual(handler.handler(["not", "an", "object"], context("vlab___cohort_count"))["error"]["code"], "INVALID_INPUT")
        self.assertEqual(self.transport.sent, [])

    def test_guards_run_in_the_lambda_too(self) -> None:
        result = handler.handler({"filter0": {"and": [{"in": {"gene.symbol": ["KRAS"]}}]}, "geneCount": 3}, context("vlab___top_mutated_genes"))
        self.assertEqual((result["error"]["code"], result["limitation"]), ("GUARD_FAILED", "UNSUPPORTED_FIELD"))

    def test_the_attempt_stays_inside_the_lambda_timeout(self) -> None:
        handler.handler({"filter": HIGH}, context("vlab___cohort_count", remaining_ms=58_000))
        self.assertAlmostEqual(self.transport.sent[-1].timeout_s, TOOL_HTTP_TIMEOUT_S, delta=0.5)
        handler.handler({"filter": HIGH}, context("vlab___cohort_count", remaining_ms=10_000))
        self.assertLessEqual(self.transport.sent[-1].timeout_s, 9.0)
        sent = len(self.transport.sent)
        result = handler.handler({"filter": HIGH}, context("vlab___cohort_count", remaining_ms=900))
        self.assertEqual((result["error"]["code"], result["error"]["retryable"], len(self.transport.sent)), ("TIMEOUT", True, sent))

    def test_a_result_over_the_cap_becomes_an_upstream_shape_error(self) -> None:
        with mock.patch.object(handler, "MAX_TOOL_RESPONSE_BYTES", 200):
            result = handler.handler({"gene": "KRAS", "cohortFilter": {"and": []}}, context("vlab___gene_cases"))
        self.assertEqual((result["status"], result["error"]["code"], result["observed"]), ("error", "UPSTREAM_SHAPE", None))
        self.assertEqual(len(result["provenance"]), 1)

    def test_the_basic_credential_comes_from_the_secret_and_is_never_logged(self) -> None:
        reads: list[str] = []

        def reader(arn: str) -> str:
            reads.append(arn)
            return json.dumps({"username": "sandbox", "password": PASSWORD})

        transport = fake_stack()
        origin = "https://d1abc.cloudfront.net"
        handler.configure(handler.Runtime.build(origin, SecretsManagerCredential(ARN, origin, reader=reader), transport=transport, goldens=goldens()))
        with self.assertLogs("orion_agent", level=logging.INFO) as logs:
            handler.handler({"filter": HIGH}, context("vlab___cohort_count"))
            handler.handler({"filter": HIGH}, context("vlab___cohort_count", request_id="req-2"))
        self.assertEqual(reads, [ARN])
        self.assertTrue(all((r.header("authorization") or "").startswith("Basic ") for r in transport.sent))
        self.assertTrue(all(r.url.startswith(origin) for r in transport.sent))
        self.assertNotIn(PASSWORD, "\n".join(logs.output))
        self.assertNotIn("Basic ", "\n".join(logs.output))

    def test_a_misconfigured_function_answers_with_the_reason(self) -> None:
        handler.configure(None)
        with mock.patch.dict("os.environ", {"ORION_VLAB_ORIGIN": "http://example.com"}, clear=False):
            result = handler.handler({"filter": HIGH}, context("vlab___cohort_count"))
        self.assertEqual((result["status"], result["error"]["code"], result["error"]["retryable"]), ("error", "INVALID_INPUT", False))
        self.assertIn("misconfigured", result["error"]["message"])

    def test_from_env_reads_the_origin_and_the_secret_arn(self) -> None:
        with mock.patch.dict("os.environ", {"ORION_VLAB_ORIGIN": "https://d1abc.cloudfront.net", "ORION_VLAB_BASIC_SECRET_ARN": ARN}):
            runtime = handler.Runtime.from_env()
        tool = runtime.tools["cohort_count"]
        self.assertEqual(tool._vlab.origin, "https://d1abc.cloudfront.net")  # noqa: SLF001
        self.assertIsInstance(tool._vlab._credential, SecretsManagerCredential)  # noqa: SLF001


def _can_build_from_image() -> bool:
    return sysconfig.get_platform().startswith("linux") and platform.machine().lower() in ("aarch64", "arm64") and sys.version_info[:2] == (3, 12)


@unittest.skipUnless(_can_build_from_image(), "the package is built from the agent image's own packages (linux arm64, Python 3.12)")
class PackageTests(unittest.TestCase):
    def build(self, out: Path) -> dict[str, Any]:
        subprocess.run([sys.executable, str(LAMBDA_DIR / "build.py"), "--deps", "installed", "--out", str(out)], check=True, capture_output=True)
        manifest: dict[str, Any] = json.loads((out / "manifest.json").read_text())
        return manifest

    def test_the_zip_is_reproducible_self_contained_and_minimal(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = self.build(Path(a)), self.build(Path(b))
            self.assertEqual(first["sha256"], second["sha256"])
            zip_path = Path(a) / first["zip"]
            self.assertEqual(hashlib.sha256(zip_path.read_bytes()).hexdigest(), first["sha256"])
            names = set(zipfile.ZipFile(zip_path).namelist())
            for required in ("handler.py", "orion_agent/tools/server.py", "orion_agent/tools/golden_snapshot.json", "h11/__init__.py", "pydantic/__init__.py"):
                self.assertIn(required, names)
            for absent in ("orion_agent/controller.py", "orion_agent/tools/client.py", "orion_agent/tools/gateway.py", "orion_agent/settings.py"):
                self.assertNotIn(absent, names)
            self.assertFalse(any("__pycache__" in n or n.startswith("boto") for n in names))
            self.assertEqual((first["handler"], first["timeoutSeconds"], first["runtime"]), ("handler.handler", 58, "python3.12"))
            tools = json.loads((Path(a) / "gateway-tools.json").read_text())
            self.assertEqual([t["name"] for t in tools], sorted(handler.VLAB_TOOLS))
            self.assertTrue(all(t["inputSchema"]["type"] == "object" and "$ref" not in json.dumps(t) for t in tools))
            with tempfile.TemporaryDirectory() as unpacked:
                zipfile.ZipFile(zip_path).extractall(unpacked)
                probe = "import sys; sys.path[:0] = [sys.argv[1]]; import handler; print(len(handler.VLAB_TOOLS))"
                done = subprocess.run([sys.executable, "-I", "-S", "-c", probe, unpacked], check=True, capture_output=True, text=True)
                self.assertEqual(done.stdout.strip(), "9")


if __name__ == "__main__":
    unittest.main()
