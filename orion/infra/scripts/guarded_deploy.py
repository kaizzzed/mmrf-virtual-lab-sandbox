"""Run aws_sandbox.py deploy --skip-build and confirm only if the saved plan is the expected one.

The account ID is read from generated/aws/config.json and written only to the child's stdin.
All child output is redacted (12-digit numbers, /32 addresses) before it reaches the log.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # the sandbox repo root
LOG = Path(sys.argv[1])
PROMPT = b"Type the sandbox account ID to continue: "


def redact(text):
    text = re.sub(r"[0-9]{12}", "<ACCT>", text)
    return re.sub(r"[0-9]{1,3}(\.[0-9]{1,3}){3}/32", "<IP>/32", text)


def plan_ok(config):
    env = dict(os.environ, AWS_PROFILE=config["profile"], AWS_REGION=config["region"])
    raw = subprocess.run(["terraform", "show", "-json", "sandbox.tfplan"], cwd=ROOT / "terraform",
                         env=env, check=True, capture_output=True, text=True).stdout
    plan = json.loads(raw)
    changes = [r for r in plan.get("resource_changes", []) if r["change"]["actions"] not in (["no-op"], ["read"])]
    summary = [(r["address"], r["change"]["actions"]) for r in changes]
    if summary != [("aws_eks_cluster.sandbox", ["update"])]:
        return False, f"unexpected resource changes: {summary}"
    before, after = changes[0]["change"]["before"], changes[0]["change"]["after"]
    unknown = changes[0]["change"].get("after_unknown") or {}
    top = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k) and not unknown.get(k))
    if top != ["vpc_config"]:
        return False, f"unexpected top-level attribute changes: {top}"
    bv, av = before["vpc_config"][0], after["vpc_config"][0]
    inner = sorted(k for k in set(bv) | set(av) if bv.get(k) != av.get(k))
    if inner != ["public_access_cidrs"]:
        return False, f"unexpected vpc_config changes: {inner}"
    if av["public_access_cidrs"] != [config["operator_cidr"]]:
        return False, "public_access_cidrs after does not equal the configured operator_cidr"
    return True, "plan OK: 1 in-place update aws_eks_cluster.sandbox vpc_config.public_access_cidrs only; 0 add, 0 destroy"


def main():
    config = json.loads((ROOT / "generated/aws/config.json").read_text())
    env = dict(os.environ)
    env["PATH"] = str(Path.home() / ".docker/bin") + ":" + env.get("PATH", "")
    child = subprocess.Popen([sys.executable, "scripts/aws_sandbox.py", "deploy", "--skip-build"], cwd=ROOT,
                             env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    pending = b""
    answered = False
    with LOG.open("a") as log:
        def emit(chunk):
            log.write(redact(chunk.decode(errors="replace")))
            log.flush()
        while True:
            data = os.read(child.stdout.fileno(), 65536)
            if not data:
                break
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                emit(line + b"\n")
            if not answered and pending.endswith(PROMPT):
                emit(pending + b"\n")
                pending = b""
                answered = True
                try:
                    ok, why = plan_ok(config)
                except Exception as error:  # noqa: BLE001 - any failure means do not confirm
                    ok, why = False, f"plan inspection failed: {type(error).__name__}"
                log.write(f"[guard] {why}\n")
                log.flush()
                if ok:
                    child.stdin.write((config["account_id"] + "\n").encode())
                    child.stdin.flush()
                    log.write("[guard] confirmed; apply proceeds with the inspected sandbox.tfplan\n")
                else:
                    log.write("[guard] NOT confirmed; sending EOF so nothing is applied\n")
                log.flush()
                child.stdin.close()
        if pending:
            emit(pending)
        code = child.wait()
        log.write(f"[guard] deploy exit={code}\n")
    sys.exit(code)


if __name__ == "__main__":
    main()
