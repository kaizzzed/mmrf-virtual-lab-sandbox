"""Operator-run AWS launcher. Importing this module never contacts AWS.

All AWS actions are explicit CLI commands run by the operator. No credentials
are stored here; the selected AWS CLI profile supplies them on that computer.
"""
import argparse
import base64
import hashlib
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("seed", "guppy", "analysis", "proteinpaint", "identity", "frontend")
NAMESPACE = "mmrf-synthetic"
CONFIG = ROOT / "generated/aws/config.json"


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def environment(config):
    env = dict(os.environ)
    # Use the named profile consistently across AWS CLI and Terraform. Never
    # print, read, copy or store the caller's actual credential values.
    for key in list(env):
        if key.startswith(("AWS_ENDPOINT_URL", "TF_CLI_ARGS", "TF_VAR_")) or key in {
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "AWS_SECURITY_TOKEN", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_DEFAULT_PROFILE", "TF_DATA_DIR", "TF_WORKSPACE",
        }:
            env.pop(key)
    env.update(AWS_PROFILE=config["profile"], AWS_REGION=config["region"],
               AWS_DEFAULT_REGION=config["region"], AWS_PAGER="", TF_IN_AUTOMATION="1")
    return env


class Operator:
    def __init__(self, config):
        self.config = config
        self.env = environment(config)
        self.directory = ROOT / "generated/aws"
        self.kubeconfig = self.directory / "kubeconfig"

    def run(self, args, capture=False, data=None, cwd=ROOT, quiet=False):
        if not quiet:
            print("+ " + " ".join(str(arg) for arg in args), flush=True)
        return subprocess.run([str(arg) for arg in args], cwd=cwd, env=self.env,
                              input=data, text=True, check=True,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else None).stdout

    def aws(self, *args):
        return json.loads(self.run(["aws", *args, "--profile", self.config["profile"],
                                   "--region", self.config["region"], "--output", "json"], capture=True))

    def identity(self):
        caller = self.aws("sts", "get-caller-identity")
        if caller["Account"] != self.config["account_id"]:
            raise ValueError("STOP: selected profile is not in the configured sandbox account")
        role_name = self.config["role_arn"].split("/")[-1]
        if f":assumed-role/{role_name}/" not in caller["Arn"]:
            raise ValueError("STOP: selected profile is not assuming the configured operator IAM role")
        return caller

    def tf(self, *args, capture=False):
        return self.run(["terraform", *args], capture=capture, cwd=ROOT / "terraform")

    def kube(self, *args, data=None, capture=False):
        return self.run(["kubectl", "--kubeconfig", self.kubeconfig,
                         "--context", self.config["name"], *args], data=data, capture=capture)

    def connect_cluster(self):
        self.run(["aws", "eks", "update-kubeconfig", "--name", self.config["name"],
                  "--profile", self.config["profile"], "--region", self.config["region"],
                  "--kubeconfig", self.kubeconfig, "--alias", self.config["name"]])
        os.chmod(self.kubeconfig, 0o600)

    def init_terraform(self):
        c = self.config
        private_json(ROOT / "terraform/sandbox.auto.tfvars.json", {
            "sandbox_account_id": c["account_id"], "operator_role_arn": c["role_arn"],
            "operator_cidrs": [c["operator_cidr"]], "region": c["region"], "name": c["name"],
            "vpc_cidr": c["vpc_cidr"], "kubernetes_version": c["kubernetes_version"],
            "enable_https": True,
        })
        self.tf("init", "-backend=false", "-input=false")
        if self.tf("workspace", "show", capture=True).strip() != "default":
            raise ValueError("Use the default Terraform workspace in this dedicated checkout")
        self.tf("validate")

    def outputs(self):
        return {key: value["value"] for key, value in json.loads(self.tf("output", "-json", capture=True)).items()}

    def preflight(self):
        self.identity()
        vpcs = self.aws("ec2", "describe-vpcs")["Vpcs"]
        state = ROOT / "terraform/terraform.tfstate"
        owned = set()
        if state.exists():
            for resource in json.loads(state.read_text()).get("resources", []):
                if resource["type"] == "aws_vpc" and resource["name"] == "sandbox":
                    owned.update(i["attributes"]["id"] for i in resource.get("instances", []))
        desired = ipaddress.ip_network(self.config["vpc_cidr"])
        for vpc in vpcs:
            for item in vpc.get("CidrBlockAssociationSet", []):
                if desired.overlaps(ipaddress.ip_network(item["CidrBlock"])) and vpc["VpcId"] not in owned:
                    raise ValueError("Configured CIDR overlaps an existing VPC. Reconfigure with another /16 before deployment")
        zones = self.aws("ec2", "describe-availability-zones", "--filters", "Name=state,Values=available")["AvailabilityZones"]
        if len(zones) < 2:
            raise ValueError("Two available AZs are required")
        offerings = self.aws("ec2", "describe-instance-type-offerings", "--location-type", "region",
                             "--filters", "Name=instance-type,Values=m6i.xlarge")["InstanceTypeOfferings"]
        if not offerings:
            raise ValueError("m6i.xlarge is not offered in this region")
        print("Read-only account/network/instance checks passed. IAM/SCP permissions, quotas and CloudFront VPC-origin support still require live provisioning.")

    def build(self):
        self.run([sys.executable, "scripts/fetch_sources.py"])
        self.run([sys.executable, "synthetic/generate.py"])
        override = self.directory / "build-compose.json"
        private_json(override, {"services": {name: {"image": f"mmrf-sandbox/{name}:aws-build"} for name in SERVICES}})
        # Separate build project/tags: never stop or replace a developer's local
        # containers, volumes, :local images, or checkout via Compose labels.
        for service in SERVICES:
            self.run(["docker", "compose", "--project-name", "mmrf-aws-build", "-f", ROOT / "compose.yaml",
                      "-f", override, "build", service])
            architecture = self.run(["docker", "image", "inspect", f"mmrf-sandbox/{service}:aws-build",
                                     "--format", "{{.Architecture}}"], capture=True).strip()
            if architecture != "amd64":
                raise ValueError(f"{service} is not amd64; refusing to publish")
        images = {name: self.run(["docker", "image", "inspect", f"mmrf-sandbox/{name}:aws-build",
                                 "--format", "{{.Id}}"], capture=True).strip() for name in SERVICES}
        private_json(self.directory / "build.json", {"tag": "build-" + uuid4().hex[:16], "images": images})

    def publish(self, outputs):
        build = json.loads((self.directory / "build.json").read_text())
        repositories = outputs["image_repositories"]
        registry = f'{self.config["account_id"]}.dkr.ecr.{self.config["region"]}.amazonaws.com'
        expected = {name: registry + "/" + self.config["name"] + "/" + name for name in SERVICES}
        if repositories != expected:
            raise ValueError("Terraform registry outputs do not match this sandbox account/name")
        token = self.run(["aws", "ecr", "get-login-password", "--profile", self.config["profile"],
                          "--region", self.config["region"]], capture=True, quiet=True)
        self.run(["docker", "login", "--username", "AWS", "--password-stdin", registry], data=token)
        images = {}
        for name, repository in repositories.items():
            tag = repository + ":" + build["tag"]
            try:
                detail = self.aws("ecr", "describe-images", "--repository-name", self.config["name"] + "/" + name,
                                  "--image-ids", "imageTag=" + build["tag"])["imageDetails"][0]
            except subprocess.CalledProcessError as error:
                if "ImageNotFoundException" not in (error.stderr or ""):
                    raise
                self.run(["docker", "tag", build["images"][name], tag])
                self.run(["docker", "push", tag])
                detail = self.aws("ecr", "describe-images", "--repository-name", self.config["name"] + "/" + name,
                                  "--image-ids", "imageTag=" + build["tag"])["imageDetails"][0]
            images[name] = repository + "@" + detail["imageDigest"]
        return images

    def credentials(self, origin):
        path = self.directory / "access.json"
        if path.exists():
            credentials = json.loads(path.read_text())
            credentials["origin"] = origin
        else:
            credentials = {"origin": origin, "username": "sandbox", "password": secrets.token_urlsafe(32)}
        if not isinstance(credentials.get("username"), str) or not credentials["username"] or ':' in credentials["username"] or not isinstance(credentials.get("password"), str) or len(credentials["password"]) < 24:
            raise ValueError("Invalid team access file; use the generated high-entropy credential")
        private_json(path, credentials)
        return credentials

    def install(self, outputs, images):
        origin = outputs["https_url"]
        validate_origin(origin)
        credentials = self.credentials(origin)
        self.connect_cluster()
        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}
        self.kube("apply", "-f", "-", data=json.dumps(namespace))
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "sandbox-access", "namespace": NAMESPACE},
                  "type": "Opaque", "data": {key: base64.b64encode(credentials[key].encode()).decode() for key in ("username", "password")}}
        self.kube("apply", "-f", "-", data=json.dumps(secret))
        values = {"images": images, "imagePullPolicy": "IfNotPresent", "access": {
            "enabled": True, "publicOrigin": origin, "originCidrs": outputs["origin_cidrs"], "existingSecret": "sandbox-access",
            "credentialRevision": hashlib.sha256(credentials["password"].encode()).hexdigest()}}
        private_json(self.directory / "values.json", values)
        self.run(["helm", "upgrade", "--install", "synthetic", "helm", "--namespace", NAMESPACE,
                  "--kubeconfig", self.kubeconfig, "--kube-context", self.config["name"],
                  "--values", self.directory / "values.json", "--wait", "--timeout", "20m"])
        self.status()

    def status(self):
        self.identity()
        self.connect_cluster()
        self.kube("get", "pods,services", "-n", NAMESPACE)
        outputs = self.outputs()
        health = self.aws("elbv2", "describe-target-health", "--target-group-arn", outputs["target_group_arn"])
        print("ALB target states:", [entry["TargetHealth"]["State"] for entry in health["TargetHealthDescriptions"]])
        credentials = json.loads((self.directory / "access.json").read_text())
        if credentials["origin"] != outputs["https_url"]:
            raise ValueError("Saved access credentials belong to another deployment URL; rerun deploy")
        check_site(credentials)
        print("HTTPS site:", outputs["https_url"])
        print("Team login is stored only in", self.directory / "access.json", "(mode 0600); do not paste that file into chat.")

    def deploy(self, yes=False, skip_build=False):
        self.preflight()
        if not skip_build:
            self.build()
        elif not (self.directory / "build.json").exists():
            raise ValueError("Run build first before --skip-build")
        self.init_terraform()
        self.tf("plan", "-input=false", "-out=sandbox.tfplan")
        confirm(self.config, yes, "CREATE/UPDATE the displayed sandbox resources")
        self.tf("apply", "-input=false", "sandbox.tfplan")
        outputs = self.outputs()
        self.install(outputs, self.publish(outputs))

    def empty_repository(self, name):
        repository = self.config["name"] + "/" + name
        for _ in range(10):
            details = self.aws("ecr", "describe-images", "--repository-name", repository)["imageDetails"]
            if not details:
                return
            # Delete OCI/Docker manifest lists before the layers they reference.
            details.sort(key=lambda item: 0 if "index" in item.get("imageManifestMediaType", "") or "manifest.list" in item.get("imageManifestMediaType", "") else 1)
            for item in details:
                result = self.aws("ecr", "batch-delete-image", "--repository-name", repository,
                                  "--image-ids", "imageDigest=" + item["imageDigest"])
                failures = result.get("failures", [])
                if any(f.get("failureCode") not in {"ImageReferencedByManifestList", "ImageNotFound"} for f in failures):
                    raise ValueError("Image deletion failed; repository retained: " + repository)
        raise ValueError("Repository is still nonempty; do not discard Terraform state")

    def destroy(self, yes=False):
        self.identity()
        self.init_terraform()
        self.tf("plan", "-destroy", "-input=false", "-out=destroy.tfplan")
        confirm(self.config, yes, "DELETE the displayed sandbox resources and their six image repositories")
        state = json.loads(self.tf("show", "-json", capture=True))
        for resource in state.get("values", {}).get("root_module", {}).get("resources", []):
            if resource["type"] == "aws_ecr_repository" and resource["name"] == "service":
                value = resource["values"]
                name = value["name"].split("/")[-1]
                expected_arn = f'arn:aws:ecr:{self.config["region"]}:{self.config["account_id"]}:repository/{self.config["name"]}/{name}'
                if name not in SERVICES or value["arn"] != expected_arn:
                    raise ValueError("Refusing to empty an unexpected repository from state")
                self.empty_repository(name)
        # Terraform owns the ALB/CloudFront/ASG links; there are no Kubernetes
        # load-balancer finalizers to clean up. Deleting EKS deletes its secrets.
        self.tf("apply", "-input=false", "destroy.tfplan")
        remaining = self.tf("state", "list", capture=True).strip()
        if remaining:
            raise ValueError("Resources remain in state; retain this directory and retry teardown")
        print("Terraform state is empty. Verify resource removal and billing in the AWS console. Keep this checkout until verified.")


def validate_origin(origin):
    from urllib.parse import urlsplit
    url = urlsplit(origin)
    import re
    if url.scheme != "https" or not re.fullmatch(r"[a-z0-9]+\.cloudfront\.net", url.netloc) or url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("Expected this deployment's CloudFront HTTPS origin")


def check_site(credentials, attempts=60):
    validate_origin(credentials["origin"])
    basic = base64.b64encode((credentials["username"] + ":" + credentials["password"]).encode()).decode()
    origin = credentials["origin"]
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("Unexpected redirect from the deployment status endpoint")
    opener = urllib.request.build_opener(NoRedirect)
    # Normal TLS verification stays on; credentials are never placed in URLs.
    for attempt in range(attempts):
        try:
            try:
                response = opener.open(origin + "/sandbox/status", timeout=20)
                response.close()
                raise ValueError("STOP: hosted application is reachable without team authentication")
            except urllib.error.HTTPError as error:
                error.close()
                if error.code != 401:
                    raise
            request = urllib.request.Request(origin + "/sandbox/status", headers={"Authorization": "Basic " + basic})
            with opener.open(request, timeout=30) as response:
                status = json.load(response)
            if not status.get("synthetic") or status.get("cases") != 1000:
                raise ValueError("HTTPS endpoint did not return the expected synthetic fixture")
            query = "{Project_project{project_id disease_type primary_site summary{case_count file_count data_categories{data_category}}}}"
            request = urllib.request.Request(origin + "/guppy/graphql",
                data=json.dumps({"query": query}).encode(),
                headers={"Authorization": "Basic " + basic, "Content-Type": "application/json"})
            with opener.open(request, timeout=30) as response:
                result = json.load(response)
            projects = (result.get("data") or {}).get("Project_project") or []
            if result.get("errors") or {p.get("project_id") for p in projects} != {"SYNTHETIC-MM", "SYNTHETIC-RNA"}:
                raise ValueError("Hosted Projects query failed; verify the Guppy schema and seeded project records")
            if any(not isinstance(p.get("disease_type"), list) or not isinstance(p.get("primary_site"), list)
                   or not (p.get("summary") or {}).get("data_categories") for p in projects):
                raise ValueError("Hosted Projects returned incompatible field shapes")
            if any(p["summary"].get("case_count") != 500 for p in projects):
                raise ValueError("Hosted Projects must each contain 500 synthetic cases")
            print("PASS: valid HTTPS, authenticated 1,000-case API and two projects; anonymous access denied")
            return
        except (OSError, urllib.error.HTTPError):
            if attempt == attempts - 1:
                raise
            time.sleep(10)


def confirm(config, yes, action):
    print(action + " in account " + config["account_id"] + ", region " + config["region"])
    if not yes and input("Type the sandbox account ID to continue: ").strip() != config["account_id"]:
        raise ValueError("Cancelled before resource changes")


@contextmanager
def lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    # The OS releases a flock when its process exits, so an interrupted run
    # (closed laptop, kill -9) cannot leave a stale lock. The file is never
    # deleted; removing a lock path would let two launchers lock different files.
    fd = os.open(directory / "operator.lock", os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another launcher command is running in this checkout") from None
        yield
    finally:
        os.close(fd)


def initialize(args):
    if CONFIG.exists():
        raise ValueError("Configuration already exists; use it for resume/teardown. Use a fresh checkout for another sandbox")
    if (ROOT / "terraform/terraform.tfstate").exists():
        raise ValueError("Terraform state already exists without operator configuration; recover its original configuration first")
    config = {"profile": args.profile, "account_id": args.account_id, "region": args.region,
              "name": args.name, "vpc_cidr": args.vpc_cidr, "kubernetes_version": args.kubernetes_version}
    import re
    if not re.fullmatch(r"\d{12}", args.account_id) or not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", args.name):
        raise ValueError("Use a twelve-digit account ID and a 3–31 character lowercase deployment name")
    if ipaddress.ip_network(args.vpc_cidr).version != 4 or ipaddress.ip_network(args.vpc_cidr).prefixlen != 16:
        raise ValueError("New VPC must be an IPv4 /16")
    operator = Operator(config)
    caller = operator.aws("sts", "get-caller-identity")
    if caller["Account"] != args.account_id:
        raise ValueError("STOP: selected profile is not in the expected sandbox account")
    if args.role_arn:
        config["role_arn"] = args.role_arn
    else:
        if ":assumed-role/" not in caller["Arn"]:
            raise ValueError("Use a profile assuming an IAM role, not an IAM user")
        role_name = caller["Arn"].split(":assumed-role/", 1)[1].split("/")[0]
        config["role_arn"] = operator.aws("iam", "get-role", "--role-name", role_name)["Role"]["Arn"]
    if not re.fullmatch(r"arn:aws:iam::" + args.account_id + r":role/.+", config["role_arn"]):
        raise ValueError("Operator role must be a full IAM role ARN in the expected account")
    cidr = args.operator_cidr
    if not cidr:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=15) as response:
            cidr = response.read().decode().strip() + "/32"
    network = ipaddress.ip_network(cidr)
    if network.version != 4 or network.prefixlen < 24:
        raise ValueError("Use your IPv4 egress /32 (or at most a /24 VPN range)")
    config["operator_cidr"] = str(network)
    operator.identity()
    private_json(CONFIG, config)
    print("Saved operator configuration to", CONFIG, "— no AWS credentials stored.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check local tool availability without AWS access")
    init = sub.add_parser("init", help="Read-only identity lookup and local configuration")
    init.add_argument("--profile", required=True)
    init.add_argument("--account-id", required=True)
    init.add_argument("--region", default="us-east-1")
    init.add_argument("--name", default="mmrf-synthetic")
    init.add_argument("--vpc-cidr", default="10.86.0.0/16")
    init.add_argument("--kubernetes-version", default="1.35")
    init.add_argument("--role-arn")
    init.add_argument("--operator-cidr")
    for command in ("check", "build", "deploy", "status", "destroy", "credentials"):
        p = sub.add_parser(command)
        if command in ("deploy", "destroy"):
            p.add_argument("--yes", action="store_true", help="Apply without the typed account-ID confirmation")
        if command == "deploy":
            p.add_argument("--skip-build", action="store_true", help="Reuse the previously recorded local image IDs")
    args = parser.parse_args()
    if args.command == "doctor":
        missing = []
        for tool in ("aws", "terraform", "docker", "kubectl", "helm"):
            path = shutil.which(tool)
            print(tool + ": " + (path or "MISSING"))
            if not path:
                missing.append(tool)
        if missing:
            raise ValueError("Install the missing tools listed above; see docs/aws-deployment.md")
        print("Local tools found. Verify versions and Docker memory against the guide before init.")
        return
    if args.command == "init":
        initialize(args)
        return
    if not CONFIG.exists():
        raise ValueError("Run init first in this dedicated checkout")
    config = json.loads(CONFIG.read_text())
    operator = Operator(config)
    for tool in ("aws", "terraform", "docker", "kubectl", "helm"):
        if not shutil.which(tool):
            raise ValueError("Missing local tool: " + tool)
    if args.command == "credentials":
        print((operator.directory / "access.json").read_text())
        return
    with lock(operator.directory):
        if args.command == "build":
            operator.env["DOCKER_DEFAULT_PLATFORM"] = "linux/amd64"
            operator.build()
        elif args.command == "deploy":
            operator.env["DOCKER_DEFAULT_PLATFORM"] = "linux/amd64"
            operator.deploy(args.yes, args.skip_build)
        elif args.command == "destroy":
            operator.destroy(args.yes)
        elif args.command == "check":
            operator.preflight()
        else:
            operator.status()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("STOP:", str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        print("Keep this checkout, generated/aws and Terraform state for resume or teardown.", file=sys.stderr)
        sys.exit(1)
