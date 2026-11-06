"""Package the Gateway Lambda `orion-vlab-tools` as a zip under orion/agent/lambda/dist (gitignored).

The zip holds `handler.py` at its root (handler `handler.handler`, runtime python3.12), the part of
`orion_agent` the server tools need (no controller, model or AWS code), the packaged golden
snapshot, and the pinned pure-Python and binary dependencies for the target architecture. boto3
comes with the Lambda runtime. Next to the zip it writes `gateway-tools.json`, the nine tools'
names, descriptions and input schemas for the Gateway target, and `manifest.json` with the zip's
sha256 and the settings the function needs (Terraform reads both).

    # inside the agent image (linux/arm64, Python 3.12: copies the image's own pinned packages, no download)
    docker run --rm -v $PWD/orion/agent:/app -w /app orion-agent:dev python lambda/build.py --deps installed --verify
    # any machine with pip and network access, for either architecture
    python lambda/build.py --arch x86_64 --deps pip

The zip is reproducible: entries sorted, fixed timestamps and modes.
"""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Final

HERE: Final = Path(__file__).resolve().parent
AGENT: Final = HERE.parent
PACKAGE: Final = AGENT / "orion_agent"
DIST: Final = HERE / "dist"
RUNTIME: Final = "python3.12"
PYTHON_VERSION: Final = "3.12"
HANDLER: Final = "handler.handler"
LAMBDA_TIMEOUT_S: Final = 58  # tools/base.py LAMBDA_TIMEOUT_S (A4 timeout budget)
MEMORY_MB: Final = 512
ZIP_LIMIT_BYTES: Final = 50 * 1024 * 1024  # Lambda's direct-upload limit
UNZIPPED_LIMIT_BYTES: Final = 250 * 1024 * 1024
FIXED_TIME: Final = (1980, 1, 1, 0, 0, 0)

# The modules the Lambda imports: handler -> tools.server and its helpers -> models, catalog, ir.
MODULES: Final = (
    "__init__.py",
    "catalog.py",
    "ir.py",
    "models.py",
    "tools/__init__.py",
    "tools/base.py",
    "tools/schemas.py",
    "tools/http.py",
    "tools/credentials.py",
    "tools/vlab.py",
    "tools/goldens.py",
    "tools/golden_snapshot.json",
    "tools/bodies.py",
    "tools/normalize.py",
    "tools/guards.py",
    "tools/server.py",
)
# The versions the agent image runs (orion/agent/requirements.txt resolves them on python:3.12-slim).
PINS: Final = {
    "pydantic": "2.14.2",
    "pydantic-core": "2.50.2",
    "annotated-types": "0.8.0",
    "typing-extensions": "4.16.0",
    "typing-inspection": "0.4.5",
    "h11": "0.16.0",
}
PLATFORMS: Final = {"arm64": "manylinux2014_aarch64", "x86_64": "manylinux2014_x86_64"}
MACHINES: Final = {"arm64": ("aarch64", "arm64"), "x86_64": ("x86_64", "amd64")}


def _copy_installed(target: Path, arch: str) -> None:
    """Copy each pinned distribution from this interpreter, which must be Linux, Python 3.12 and `arch`."""
    machine = platform.machine().lower()
    if not sysconfig.get_platform().startswith("linux") or machine not in MACHINES[arch] or sys.version_info[:2] != (3, 12):
        raise SystemExit(f"--deps installed needs Linux {arch} Python 3.12 (this is {sysconfig.get_platform()} {machine}); use --deps pip")
    for name, version in PINS.items():
        dist = importlib.metadata.distribution(name)
        if dist.version != version:
            raise SystemExit(f"{name} is {dist.version} here, not the pinned {version}")
        for file in dist.files or ():
            source = Path(str(dist.locate_file(file)))
            parts = Path(str(file)).parts
            if parts and parts[0] == ".." or "__pycache__" in parts or source.suffix == ".pyc" or not source.is_file():
                continue
            destination = target / Path(str(file))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


def _pip_install(target: Path, arch: str) -> None:
    command = [
        sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-compile", "--only-binary=:all:",
        "--platform", PLATFORMS[arch], "--implementation", "cp", "--python-version", PYTHON_VERSION,
        "--target", str(target), *(f"{name}=={version}" for name, version in PINS.items()),
    ]
    subprocess.run(command, check=True)


def _copy_package(target: Path) -> None:
    for module in MODULES:
        source = PACKAGE / module
        if not source.is_file():
            raise SystemExit(f"orion_agent/{module} is missing")
        destination = target / "orion_agent" / module
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    shutil.copyfile(HERE / "handler.py", target / "handler.py")


def _zip(source: Path, output: Path) -> int:
    files = sorted(p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    unzipped = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            data = path.read_bytes()
            unzipped += len(data)
            archive.writestr(info, data)
    return unzipped


def _simplify(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """A plain JSON Schema for the Gateway's tool listing: refs inlined, nullable and union values
    left untyped, constraints and enums folded into the description. The Lambda validates every
    argument against the tool's strict Input model, so this schema only guides the caller."""
    if "$ref" in schema:
        name = str(schema["$ref"]).rsplit("/", 1)[-1]
        merged = {**defs[name], **{k: v for k, v in schema.items() if k != "$ref"}}
        return _simplify(merged, defs)
    out: dict[str, Any] = {}
    notes: list[str] = []
    if "enum" in schema or "const" in schema:
        values = schema.get("enum") or [schema.get("const")]
        notes.append("One of: " + ", ".join(str(v) for v in values) + ".")
    kind = schema.get("type")
    if "anyOf" in schema or "oneOf" in schema or isinstance(kind, list):
        notes.append("May be null." if any(s.get("type") == "null" for s in schema.get("anyOf", [])) else "")
    elif kind == "object":
        out["type"] = "object"
        properties = schema.get("properties")
        if isinstance(properties, dict):
            out["properties"] = {key: _simplify(value, defs) for key, value in properties.items()}
            if schema.get("required"):
                out["required"] = list(schema["required"])
    elif kind == "array":
        out["type"] = "array"
        items = schema.get("items")
        if isinstance(items, dict):
            out["items"] = _simplify(items, defs)
    elif isinstance(kind, str):
        out["type"] = kind
    description = " ".join(part for part in [str(schema.get("description", "")).strip(), *notes] if part)
    if description:
        out["description"] = description
    return out


def gateway_tools() -> list[dict[str, Any]]:
    sys.path.insert(0, str(AGENT))
    from orion_agent.tools.schemas import TOOL_REGISTRY, input_json_schema
    from orion_agent.tools.server import VLAB_TOOLS

    tools: list[dict[str, Any]] = []
    for name in sorted(VLAB_TOOLS):
        schema: dict[str, Any] = dict(input_json_schema(name))
        defs: dict[str, Any] = dict(schema.pop("$defs", None) or {})
        simplified = _simplify(schema, defs)
        simplified.setdefault("properties", {})
        tools.append({"name": name, "description": TOOL_REGISTRY[name].summary, "inputSchema": simplified})
    return tools


def _verify(zip_path: Path) -> None:
    """Import the handler from the unpacked zip alone (no site-packages) and list its tools."""
    with tempfile.TemporaryDirectory() as unpacked:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(unpacked)
        probe = (
            "import sys; sys.path[:0] = [sys.argv[1]]; import handler; "
            "print(sorted(handler.VLAB_TOOLS)); print(handler.tool_name(type('C', (), {'client_context': type('X', (), "
            "{'custom': {'bedrockAgentCoreToolName': 'vlab___cohort_count'}})()})()))"
        )
        done = subprocess.run([sys.executable, "-I", "-S", "-c", probe, unpacked], check=True, capture_output=True, text=True)
        print("verified:", done.stdout.strip().replace("\n", " | "))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64")
    parser.add_argument("--deps", choices=("installed", "pip"), default="pip")
    parser.add_argument("--out", type=Path, default=DIST)
    parser.add_argument("--verify", action="store_true", help="import the handler from the zip alone (same architecture only)")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    zip_path = args.out / f"orion-vlab-tools-{args.arch}.zip"
    with tempfile.TemporaryDirectory() as staging:
        root = Path(staging)
        (_copy_installed if args.deps == "installed" else _pip_install)(root, args.arch)
        _copy_package(root)
        unzipped = _zip(root, zip_path)
    size = zip_path.stat().st_size
    if size > ZIP_LIMIT_BYTES or unzipped > UNZIPPED_LIMIT_BYTES:
        raise SystemExit(f"{zip_path.name} is {size} bytes ({unzipped} unzipped), over Lambda's limits")
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    tools = gateway_tools()
    (args.out / "gateway-tools.json").write_text(json.dumps(tools, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "function": "orion-vlab-tools", "zip": zip_path.name, "sha256": digest, "bytes": size, "unzippedBytes": unzipped,
        "runtime": RUNTIME, "architecture": args.arch, "handler": HANDLER, "timeoutSeconds": LAMBDA_TIMEOUT_S,
        "memoryMb": MEMORY_MB, "environment": ["ORION_VLAB_ORIGIN", "ORION_VLAB_BASIC_SECRET_ARN"],
        "gatewayTarget": "vlab", "tools": [t["name"] for t in tools], "pins": PINS,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {zip_path} ({size} bytes, sha256 {digest}), gateway-tools.json and manifest.json")
    if args.verify:
        _verify(zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
