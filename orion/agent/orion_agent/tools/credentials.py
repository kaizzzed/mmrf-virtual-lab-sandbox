"""The team Basic credential the tools send to a hosted sandbox (RFA rev 25, A4 "Security at sandbox level").

Locally the identity gateway needs no credential: it issues its own synthetic token. A hosted
sandbox sits behind CloudFront and the access gateway, which checks the team Basic password. The
credential is read at run time from where the repo keeps it and is never logged:

- an access file, the JSON `{"origin", "username", "password"}` that `scripts/aws_sandbox.py`
  writes and `scripts/sandbox_connection.py` reads (`ORION_VLAB_ACCESS_FILE`; developer runs);
- the Secrets Manager secret `orion/vlab-basic` (`ORION_VLAB_BASIC_SECRET_ARN`), holding the same
  JSON (`origin` optional), read by the Gateway Lambda (and by the Runtime only if Gateway is cut).

`authorization()` returns the header value or None. Values live only in `_Secret`, whose repr is
redacted, and no error message quotes them.
"""

import asyncio
import base64
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol

SECRET_TTL_S: Final = 300.0


class CredentialError(Exception):
    """The credential source is missing, unreadable or for another origin (never quotes a value)."""


class _Secret:
    """A header value that never prints."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


class CredentialSource(Protocol):
    async def authorization(self) -> str | None: ...


class NoCredential:
    """The local stack: the identity gateway needs no credential."""

    async def authorization(self) -> str | None:
        return None

    def __repr__(self) -> str:
        return "NoCredential()"


def basic_from_access_json(text: str, origin: str) -> _Secret:
    """The `Basic` header value from an access-file JSON, checked against the origin it belongs to."""
    try:
        access = json.loads(text)
    except ValueError as error:
        raise CredentialError("the access credential is not JSON") from error
    if not isinstance(access, dict):
        raise CredentialError("the access credential is not a JSON object")
    username, password, bound = access.get("username"), access.get("password"), access.get("origin")
    if not isinstance(username, str) or not username or ":" in username:
        raise CredentialError("the access credential has no valid username")
    if not isinstance(password, str) or len(password) < 24:
        raise CredentialError("the access credential has no valid password (the generated one has at least 24 characters)")
    if bound is not None and (not isinstance(bound, str) or bound.rstrip("/") != origin.rstrip("/")):
        raise CredentialError("the access credential belongs to another origin")
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return _Secret(f"Basic {token}")


class AccessFileCredential:
    """The repo's access file, re-read when it changes (developer runs against a hosted sandbox)."""

    def __init__(self, path: Path, origin: str) -> None:
        self._path = path
        self._origin = origin
        self._cached: tuple[float, _Secret] | None = None

    async def authorization(self) -> str | None:
        try:
            stamp = self._path.stat().st_mtime
        except OSError as error:
            raise CredentialError("the access file cannot be read") from error
        if self._cached is None or self._cached[0] != stamp:
            try:
                text = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
            except OSError as error:
                raise CredentialError("the access file cannot be read") from error
            self._cached = (stamp, basic_from_access_json(text, self._origin))
        return self._cached[1].reveal()

    def __repr__(self) -> str:
        return f"AccessFileCredential({self._path.name!r})"


SecretReader = Callable[[str], str]


def boto3_secret_reader(region: str | None = None) -> SecretReader:
    """GetSecretValue through boto3 (in the Lambda runtime and the agent image). Never used in tests."""

    def read(arn: str) -> str:
        import boto3  # type: ignore[import-untyped]

        client = boto3.client("secretsmanager", region_name=region or os.environ.get("AWS_REGION"))
        value = client.get_secret_value(SecretId=arn).get("SecretString")
        if not isinstance(value, str):
            raise CredentialError("the secret has no SecretString")
        return value

    return read


class SecretsManagerCredential:
    """The Basic credential from Secrets Manager, cached for SECRET_TTL_S so a warm Lambda reads it once."""

    def __init__(
        self, secret_arn: str, origin: str, *, reader: SecretReader | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if not secret_arn.startswith("arn:"):
            raise CredentialError("ORION_VLAB_BASIC_SECRET_ARN is not an ARN")
        self._arn = secret_arn
        self._origin = origin
        self._reader = reader or boto3_secret_reader()
        self._clock = clock
        self._cached: tuple[float, _Secret] | None = None

    async def authorization(self) -> str | None:
        # No lock: two concurrent refreshes read the same secret twice, which is harmless, and a
        # lock would bind to one event loop while a warm Lambda runs each invocation in a new one.
        now = self._clock()
        if self._cached is None or now - self._cached[0] >= SECRET_TTL_S:
            try:
                text = await asyncio.to_thread(self._reader, self._arn)
            except CredentialError:
                raise
            except Exception as error:
                raise CredentialError(f"the secret could not be read ({type(error).__name__})") from error
            self._cached = (now, basic_from_access_json(text, self._origin))
        return self._cached[1].reveal()

    def __repr__(self) -> str:
        return "SecretsManagerCredential(<arn>)"
