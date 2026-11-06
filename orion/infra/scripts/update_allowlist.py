"""Point the sandbox EKS API allowlist at this machine's current egress /32, and nothing else.

Uses scripts/aws_sandbox.py's own Operator (profile, tfvars, terraform init/validate), saves a plan,
applies it only if it is exactly one in-place update of aws_eks_cluster.sandbox
vpc_config.public_access_cidrs to [operator_cidr] (guarded_deploy.plan_ok), and never runs helm or
publishes images, so ORION's Helm values stay as they are. Prints no address or account id.
Exit 0: updated or already current; 3: plan refused; other: error.
"""
import ipaddress
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # the sandbox repo root
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "scripts"), str(HERE)]

import aws_sandbox  # noqa: E402

# guarded_deploy reads its log path from argv at import time; it writes nothing unless main() runs.
sys.argv = [sys.argv[0], str(HERE / "allowlist-guard.log")]
from guarded_deploy import plan_ok  # noqa: E402


def main() -> int:
    config_path = ROOT / "generated/aws/config.json"
    config = json.loads(config_path.read_text())
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=15) as response:
        ip = response.read().decode().strip()
    cidr = str(ipaddress.ip_network(ip + "/32"))
    operator = aws_sandbox.Operator(config)
    operator.identity()
    live = operator.aws("eks", "describe-cluster", "--name", config["name"])["cluster"]["resourcesVpcConfig"]["publicAccessCidrs"]
    if config["operator_cidr"] == cidr and live == [cidr]:
        print("allowlist already matches the current egress IP")
        return 0
    config["operator_cidr"] = cidr
    aws_sandbox.private_json(config_path, config)
    operator.config = config
    operator.init_terraform()
    operator.tf("plan", "-input=false", "-out=sandbox.tfplan")
    ok, why = plan_ok(config)
    print("[allowlist guard]", why)
    if not ok:
        return 3
    operator.tf("apply", "-input=false", "sandbox.tfplan")
    live = operator.aws("eks", "describe-cluster", "--name", config["name"])["cluster"]["resourcesVpcConfig"]["publicAccessCidrs"]
    print("allowlist now matches the current egress IP:", live == [cidr])
    operator.connect_cluster()
    return 0


if __name__ == "__main__":
    sys.exit(main())
