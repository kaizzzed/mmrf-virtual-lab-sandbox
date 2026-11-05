"""The tool layer's HTTP client, the allow-listed Virtual Lab client and the Basic credential sources."""

import asyncio
import base64
import gzip
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

from test_tools_fakes import ORIGIN, FakeTransport, LocalServer, Received, Reply, fixed, free_port, raises

from orion_agent.models import canonical_json, sha256_hex
from orion_agent.tools.credentials import (
    AccessFileCredential,
    CredentialError,
    NoCredential,
    SecretsManagerCredential,
    basic_from_access_json,
)
from orion_agent.tools.http import (
    H11Transport,
    HttpBodyTooLarge,
    HttpConnectError,
    HttpProtocolError,
    HttpTimeout,
    Target,
)
from orion_agent.tools.vlab import ALLOWED_REQUESTS, OriginError, VlabClient, VlabRequestError, validate_origin

T = TypeVar("T")
PASSWORD = "p" * 30  # a synthetic test value; the generated team password has at least 24 characters


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


class H11TransportTests(unittest.TestCase):
    def test_post_round_trip_sends_one_closed_request_and_reads_the_json(self) -> None:
        async def go() -> None:
            async with LocalServer(lambda r: Reply(200, [("Content-Type", "application/json")], b'{"ok":true}')) as server:
                response = await H11Transport().send(
                    "POST", server.url("/guppy/graphql?x=1"), [("Content-Type", "application/json")], b'{"a":1}', timeout_s=5, max_bytes=1000
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.body), {"ok": True})
                self.assertEqual(response.media_type, "application/json")
                sent = server.received[0]
                self.assertEqual((sent.method, sent.target, sent.body), ("POST", "/guppy/graphql?x=1", b'{"a":1}'))
                self.assertEqual(sent.headers["host"], f"127.0.0.1:{server.port}")
                self.assertEqual(sent.headers["connection"], "close")
                self.assertEqual(sent.headers["accept-encoding"], "identity")
                self.assertEqual(sent.headers["content-length"], "7")

        run(go())

    def test_get_sends_no_body_and_reads_a_chunked_response(self) -> None:
        body = json.dumps({"rows": list(range(50))}).encode()

        async def go() -> None:
            async with LocalServer(lambda r: Reply(200, [], body, chunked=True)) as server:
                response = await H11Transport().send("GET", server.url("/sandbox/status"), [], None, timeout_s=5, max_bytes=10_000)
                self.assertEqual(response.body, body)
                self.assertNotIn("content-length", server.received[0].headers)

        run(go())

    def test_gzip_is_decoded_and_capped_after_decoding(self) -> None:
        body = b'{"x": "' + b"a" * 5000 + b'"}'

        async def go() -> None:
            async with LocalServer(lambda r: Reply(200, [], body, gzip_body=True)) as server:
                response = await H11Transport().send("GET", server.url("/"), [], None, timeout_s=5, max_bytes=10_000)
                self.assertEqual(response.body, body)
                with self.assertRaises(HttpBodyTooLarge):
                    await H11Transport().send("GET", server.url("/"), [], None, timeout_s=5, max_bytes=1000)

        run(go())

    def test_a_body_over_the_cap_is_refused(self) -> None:
        async def go() -> None:
            async with LocalServer(lambda r: Reply(200, [], b"x" * 5000, chunked=True)) as server:
                with self.assertRaises(HttpBodyTooLarge):
                    await H11Transport().send("GET", server.url("/"), [], None, timeout_s=5, max_bytes=4999)

        run(go())

    def test_the_deadline_covers_the_whole_response(self) -> None:
        async def go() -> None:
            async with LocalServer(lambda r: Reply(200, [], b"{}", delay_s=2.0)) as server:
                started = time.monotonic()
                with self.assertRaises(HttpTimeout):
                    await H11Transport().send("GET", server.url("/"), [], None, timeout_s=0.2, max_bytes=100)
                self.assertLess(time.monotonic() - started, 1.5)

        run(go())

    def test_connection_refused_and_early_close(self) -> None:
        async def go() -> None:
            with self.assertRaises(HttpConnectError):
                await H11Transport().send("GET", f"http://127.0.0.1:{free_port()}/", [], None, timeout_s=2, max_bytes=100)
            async with LocalServer(lambda r: Reply(200, [], b"0123456789" * 10, close_early=True)) as server:
                with self.assertRaises(HttpProtocolError):
                    await H11Transport().send("GET", server.url("/"), [], None, timeout_s=2, max_bytes=1000)

        run(go())

    def test_cancellation_aborts_the_request_at_once(self) -> None:
        async def go() -> None:
            async with LocalServer(lambda r: Reply(200, [], b"{}", delay_s=5.0)) as server:
                task = asyncio.create_task(H11Transport().send("GET", server.url("/"), [], None, timeout_s=30, max_bytes=100))
                await asyncio.sleep(0.2)
                started = time.monotonic()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertLess(time.monotonic() - started, 0.5)

        run(go())

    def test_targets_refuse_credentials_in_urls_and_keep_explicit_ports(self) -> None:
        with self.assertRaises(ValueError):
            Target.parse("http://user:pw@identity:8080/")
        with self.assertRaises(ValueError):
            Target.parse("ftp://identity/")
        self.assertEqual(Target.parse("https://abc.cloudfront.net/x?y=1").host_header, "abc.cloudfront.net")
        self.assertEqual(Target.parse("http://identity:8080/x").host_header, "identity:8080")
        self.assertEqual(Target.parse("http://[::1]:8080/").host_header, "[::1]:8080")

    @unittest.skipUnless(shutil.which("openssl"), "openssl is not installed")
    def test_https_verifies_the_server_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp, "cert.pem"), Path(tmp, "key.pem")
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                 "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)],
                check=True, capture_output=True,
            )
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(cert, key)
            trusting = ssl.create_default_context(cafile=str(cert))

            async def go() -> None:
                async with LocalServer(lambda r: Reply(200, [], b'{"tls":true}'), ssl_context=server_ctx) as server:
                    url = server.url("/", scheme="https", host="localhost")
                    response = await H11Transport(ssl_context=trusting).send("GET", url, [], None, timeout_s=5, max_bytes=100)
                    self.assertEqual(json.loads(response.body), {"tls": True})
                    with self.assertRaises(HttpConnectError):
                        await H11Transport().send("GET", url, [], None, timeout_s=5, max_bytes=100)  # untrusted certificate

            run(go())


class VlabClientTests(unittest.TestCase):
    def test_origins_are_the_local_stack_or_cloudfront(self) -> None:
        for good in ("http://identity:8080", "http://localhost:3080/", "http://127.0.0.1:9", "https://d1abc.cloudfront.net"):
            self.assertEqual(validate_origin(good), good.rstrip("/"))
        for bad in ("http://example.com", "https://example.com", "http://d1abc.cloudfront.net", "https://d1abc.cloudfront.net/x",
                    "http://u:p@identity:8080", "https://d1abc.cloudfront.net:8443"):
            with self.assertRaises(OriginError, msg=bad):
                validate_origin(bad)

    def test_only_allow_listed_requests_are_sent(self) -> None:
        transport = FakeTransport({"/files/x": fixed(200, {})})
        client = VlabClient(ORIGIN, transport)
        with self.assertRaises(PermissionError):
            run(client.send("POST", "/analysis/v0/cohorts/query", {}, timeout_s=1))
        with self.assertRaises(PermissionError):
            run(client.send("GET", "/files/x", None, timeout_s=1))
        self.assertEqual(transport.sent, [])
        self.assertIn(("POST", "/guppy/graphql"), ALLOWED_REQUESTS)
        self.assertNotIn(("POST", "/analysis/v0/cohorts/query"), ALLOWED_REQUESTS)

    def test_provenance_hashes_the_payload_and_the_parsed_response(self) -> None:
        transport = FakeTransport({"/guppy/graphql": fixed(200, {"data": {"b": 1, "a": [2]}}), "/sandbox/status": fixed(200, {"s": 1})})
        client = VlabClient(ORIGIN, transport)
        payload: dict[str, Any] = {"query": "q", "variables": {"filter": {"and": []}}}
        exchange = run(client.send("POST", "/guppy/graphql", payload, timeout_s=1))
        prov = exchange.provenance
        self.assertEqual(prov.request_sha256, sha256_hex(canonical_json(payload, portable=False)))
        self.assertEqual(prov.response_sha256, sha256_hex(canonical_json({"data": {"a": [2], "b": 1}}, portable=False)))
        self.assertEqual((prov.method, prov.path, prov.http_status), ("POST", "/guppy/graphql", 200))
        self.assertEqual(json.loads(transport.sent[0].body or b""), payload)
        self.assertEqual(transport.sent[0].header("content-type"), "application/json")
        get = run(client.send("GET", "/sandbox/status", None, timeout_s=1)).provenance
        self.assertEqual(get.request_sha256, sha256_hex("null"))
        self.assertIsNone(transport.sent[1].body)

    def test_a_request_without_a_response_still_yields_provenance(self) -> None:
        client = VlabClient(ORIGIN, FakeTransport({"/guppy/graphql": raises(HttpTimeout("slow"))}))
        with self.assertRaises(VlabRequestError) as caught:
            run(client.send("POST", "/guppy/graphql", {"q": 1}, timeout_s=1))
        self.assertIsInstance(caught.exception.cause, HttpTimeout)
        self.assertIsNone(caught.exception.provenance.http_status)
        self.assertIsNone(caught.exception.provenance.response_sha256)

    def test_a_non_json_body_has_no_response_hash(self) -> None:
        from orion_agent.tools.http import HttpResponse

        client = VlabClient(ORIGIN, FakeTransport({"/sandbox/status": lambda r: HttpResponse(502, (), b"<html>bad gateway</html>")}))
        exchange = run(client.send("GET", "/sandbox/status", None, timeout_s=1))
        self.assertFalse(exchange.is_json)
        self.assertIsNone(exchange.provenance.response_sha256)

    def test_the_credential_goes_only_over_https_or_loopback(self) -> None:
        access = json.dumps({"username": "sandbox", "password": PASSWORD})

        class Static:
            async def authorization(self) -> str | None:
                return basic_from_access_json(access, "x").reveal()

        transport = FakeTransport({"/sandbox/status": fixed(200, {})})
        with self.assertRaises(PermissionError):
            run(VlabClient("http://identity:8080", transport, credential=Static()).send("GET", "/sandbox/status", None, timeout_s=1))
        self.assertEqual(transport.sent, [])
        run(VlabClient("https://d1abc.cloudfront.net", transport, credential=Static()).send("GET", "/sandbox/status", None, timeout_s=1))
        expected = "Basic " + base64.b64encode(f"sandbox:{PASSWORD}".encode()).decode()
        self.assertEqual(transport.sent[0].header("authorization"), expected)
        self.assertEqual(transport.sent[0].url, "https://d1abc.cloudfront.net/sandbox/status")
        run(VlabClient("http://127.0.0.1:3080", transport, credential=Static()).send("GET", "/sandbox/status", None, timeout_s=1))
        self.assertEqual(transport.sent[1].header("authorization"), expected)


class CredentialTests(unittest.TestCase):
    def test_access_json_is_checked_and_never_printed(self) -> None:
        secret = basic_from_access_json(json.dumps({"origin": "https://d1.cloudfront.net/", "username": "u", "password": PASSWORD}), "https://d1.cloudfront.net")
        self.assertNotIn(PASSWORD, repr(secret) + str(secret))
        for bad in (
            "not json", "[]", json.dumps({"username": "u:x", "password": PASSWORD}), json.dumps({"username": "u", "password": "short"}),
            json.dumps({"origin": "https://other.cloudfront.net", "username": "u", "password": PASSWORD}),
        ):
            with self.assertRaises(CredentialError) as caught:
                basic_from_access_json(bad, "https://d1.cloudfront.net")
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_the_access_file_is_reread_when_it_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "access.json")
            path.write_text(json.dumps({"origin": "https://d1.cloudfront.net", "username": "a", "password": PASSWORD}))
            source = AccessFileCredential(path, "https://d1.cloudfront.net")
            first = run(source.authorization())
            path.write_text(json.dumps({"origin": "https://d1.cloudfront.net", "username": "b", "password": PASSWORD}))
            os.utime(path, (time.time() + 10, time.time() + 10))
            second = run(source.authorization())
            self.assertNotEqual(first, second)
            self.assertNotIn(PASSWORD, repr(source))
            path.unlink()
            with self.assertRaises(CredentialError):
                run(source.authorization())

    def test_secrets_manager_is_read_once_per_ttl_with_an_injected_reader(self) -> None:
        calls: list[str] = []
        now = [0.0]

        def reader(arn: str) -> str:
            calls.append(arn)
            return json.dumps({"username": "sandbox", "password": PASSWORD})

        arn = "arn:aws:secretsmanager:us-east-1:000000000000:secret:orion/vlab-basic-AbCdEf"
        source = SecretsManagerCredential(arn, "https://d1.cloudfront.net", reader=reader, clock=lambda: now[0])
        self.assertTrue(str(run(source.authorization())).startswith("Basic "))
        run(source.authorization())
        self.assertEqual(len(calls), 1)
        now[0] = 301.0
        run(source.authorization())
        self.assertEqual(len(calls), 2)
        self.assertNotIn(PASSWORD, repr(source))
        with self.assertRaises(CredentialError):
            SecretsManagerCredential("orion/vlab-basic", "https://d1.cloudfront.net", reader=reader)

        def broken(arn: str) -> str:
            raise RuntimeError(f"boom {PASSWORD}")

        with self.assertRaises(CredentialError) as caught:
            run(SecretsManagerCredential(arn, "https://d1.cloudfront.net", reader=broken).authorization())
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertIsNone(run(NoCredential().authorization()))


if __name__ == "__main__":
    unittest.main()
