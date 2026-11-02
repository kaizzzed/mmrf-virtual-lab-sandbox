"""Offline launcher regressions. No subprocess or AWS endpoint is contacted."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch


def module(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


launcher = module("aws_sandbox")
connection = module("sandbox_connection")
CONFIG = {"profile": "unit-test", "account_id": "123456789012", "region": "us-east-1",
          "role_arn": "arn:aws:iam::123456789012:role/unit-test", "name": "mmrf-synthetic",
          "operator_cidr": "192.0.2.1/32", "vpc_cidr": "10.86.0.0/16", "kubernetes_version": "1.35"}


def json_response(value):
    """Return a context-managed HTTP response without performing I/O."""
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps(value).encode()
    return response


class LauncherTests(unittest.TestCase):
    def test_live_acceptance_refuses_anonymous_application_access(self):
        opener = Mock()
        credentials = {"origin": "https://unitfixture.cloudfront.net", "username": "sandbox", "password": "unit-test-only"}
        with patch.object(launcher.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "reachable without team authentication"):
                launcher.check_site(credentials, attempts=1)
        self.assertEqual(opener.open.call_count, 1)

    def test_live_acceptance_requires_expected_authenticated_fixture(self):
        opener = Mock()
        projects = [{"project_id": name, "disease_type": ["Synthetic"], "primary_site": ["Synthetic"],
                     "summary": {"case_count": 500, "data_categories": [{"data_category": "Sequencing"}]}}
                    for name in ["SYNTHETIC-MM", "SYNTHETIC-RNA"]]
        opener.open.side_effect = [urllib.error.HTTPError("https://unitfixture.cloudfront.net/sandbox/status", 401, "Unauthorized", {}, None),
                                  json_response({"synthetic": True, "cases": 1000}),
                                  json_response({"data": {"Project_project": projects}})]
        credentials = {"origin": "https://unitfixture.cloudfront.net", "username": "sandbox", "password": "unit-test-only"}
        with patch.object(launcher.urllib.request, "build_opener", return_value=opener):
            launcher.check_site(credentials, attempts=1)
        request = opener.open.call_args_list[1].args[0]
        self.assertEqual(request.full_url, credentials["origin"] + "/sandbox/status")
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        projects_request = opener.open.call_args_list[2].args[0]
        self.assertEqual(projects_request.full_url, credentials["origin"] + "/guppy/graphql")
        self.assertEqual(projects_request.get_header("Authorization"), request.get_header("Authorization"))

    def test_live_acceptance_rejects_missing_or_invalid_project_schema(self):
        """A healthy identity response must not conceal a broken Projects page."""
        for result in [{"errors": [{"message": "String cannot represent value"}], "data": None},
                       {"data": {"Project_project": []}},
                       {"data": {"Project_project": [{"project_id": name, "disease_type": "scalar"}
                                                     for name in ["SYNTHETIC-MM", "SYNTHETIC-RNA"]]}}]:
            with self.subTest(result=result):
                opener = Mock()
                opener.open.side_effect = [urllib.error.HTTPError("https://unitfixture.cloudfront.net/sandbox/status", 401, "Unauthorized", {}, None),
                                          json_response({"synthetic": True, "cases": 1000}), json_response(result)]
                credentials = {"origin": "https://unitfixture.cloudfront.net", "username": "sandbox", "password": "unit-test-only"}
                with patch.object(launcher.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(ValueError, "Projects"):
                        launcher.check_site(credentials, attempts=1)

    def test_live_acceptance_rejects_incorrect_project_counts(self):
        for project_id in ["SYNTHETIC-MM", "SYNTHETIC-RNA"]:
            for count in [None, 0, 499, "500"]:
                with self.subTest(project=project_id, count=count):
                    projects = [{"project_id": name, "disease_type": ["Synthetic"], "primary_site": ["Synthetic"],
                                 "summary": {"case_count": 500, "data_categories": [{"data_category": "Sequencing"}]}}
                                for name in ["SYNTHETIC-MM", "SYNTHETIC-RNA"]]
                    summary = next(p["summary"] for p in projects if p["project_id"] == project_id)
                    if count is None:
                        del summary["case_count"]
                    else:
                        summary["case_count"] = count
                    opener = Mock()
                    opener.open.side_effect = [urllib.error.HTTPError("https://unitfixture.cloudfront.net/sandbox/status", 401, "Unauthorized", {}, None),
                                              json_response({"synthetic": True, "cases": 1000}),
                                              json_response({"data": {"Project_project": projects}})]
                    credentials = {"origin": "https://unitfixture.cloudfront.net", "username": "sandbox", "password": "unit-test-only"}
                    with patch.object(launcher.urllib.request, "build_opener", return_value=opener):
                        with self.assertRaisesRegex(ValueError, "500 synthetic cases"):
                            launcher.check_site(credentials, attempts=1)

    def test_wrong_account_prevents_build_plan_and_apply(self):
        operator = launcher.Operator(CONFIG)
        operator.aws = Mock(return_value={"Account": "999999999999", "Arn": "test"})
        operator.build = Mock()
        operator.tf = Mock()
        with self.assertRaisesRegex(ValueError, "configured sandbox account"):
            operator.deploy(yes=True)
        operator.build.assert_not_called()
        operator.tf.assert_not_called()

    def test_profile_environment_cannot_be_overridden_by_ambient_credentials(self):
        with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "unit-test", "AWS_SECRET_ACCESS_KEY": "unit-test",
                                    "AWS_ENDPOINT_URL": "https://unexpected.invalid", "TF_VAR_sandbox_account_id": "other"}):
            env = launcher.environment(CONFIG)
        self.assertEqual(env["AWS_PROFILE"], "unit-test")
        for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL", "TF_VAR_sandbox_account_id"]:
            self.assertNotIn(key, env)

    def test_named_role_is_required(self):
        operator = launcher.Operator(CONFIG)
        operator.aws = Mock(return_value={"Account": CONFIG["account_id"], "Arn": "arn:aws:sts::123456789012:assumed-role/OtherRole/session"})
        with self.assertRaisesRegex(ValueError, "configured operator IAM role"):
            operator.identity()

    def test_secrets_are_private_and_origin_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            launcher.private_json(path, {"origin": "https://unitfixture.cloudfront.net", "username": "sandbox", "password": "unit-test-only"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            headers, opener = connection.connection("https://unitfixture.cloudfront.net", path)
            self.assertIn("Authorization", headers)
            with self.assertRaises(ValueError):
                connection.connection("https://anotherfixture.cloudfront.net", path)
            handler = next(h for h in opener.handlers if type(h).__name__ == "SameOriginRedirect")
            with self.assertRaises(ValueError):
                handler.redirect_request(None, None, 302, "Found", {}, "https://unexpected.invalid/file")

    def test_smoke_tests_cannot_target_production_or_unbound_hosted_sites(self):
        for origin in ["https://virtuallab.themmrf.org", "https://fixture.cloudfront.net", "https://fixture.cloudfront.net.evil.invalid"]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                connection.connection(origin)

    def test_empty_ecr_deletes_parent_manifest_before_child(self):
        operator = launcher.Operator(CONFIG)
        operator.aws = Mock(side_effect=[
            {"imageDetails": [{"imageDigest": "child", "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json"},
                              {"imageDigest": "parent", "imageManifestMediaType": "application/vnd.oci.image.index.v1+json"}]},
            {"failures": []}, {"failures": []}, {"imageDetails": []},
        ])
        operator.empty_repository("frontend")
        self.assertEqual(operator.aws.call_args_list[1].args[-1], "imageDigest=parent")
        self.assertEqual(operator.aws.call_args_list[2].args[-1], "imageDigest=child")

    def test_destroy_refuses_repository_outside_expected_state_namespace(self):
        operator = launcher.Operator(CONFIG)
        operator.identity = Mock()
        operator.init_terraform = Mock()
        operator.empty_repository = Mock()
        state = {"values": {"root_module": {"resources": [{"type": "aws_ecr_repository", "name": "service",
                  "values": {"name": "production/frontend", "arn": "arn:aws:ecr:us-east-1:123456789012:repository/production/frontend"}}]}}}
        operator.tf = Mock(side_effect=[None, json.dumps(state)])
        with self.assertRaisesRegex(ValueError, "unexpected repository"):
            operator.destroy(yes=True)
        operator.empty_repository.assert_not_called()
        self.assertFalse(any(call.args[0] == "apply" for call in operator.tf.call_args_list))

    def test_lock_ignores_leftover_file_and_refuses_held_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            # A file left by an interrupted run holds no OS lock.
            (Path(directory) / "operator.lock").write_text("12345")
            with launcher.lock(Path(directory)):
                with self.assertRaisesRegex(ValueError, "Another launcher command is running"):
                    with launcher.lock(Path(directory)):
                        pass
            with launcher.lock(Path(directory)):
                pass

    def test_deploy_applies_exactly_the_saved_plan(self):
        operator = launcher.Operator(CONFIG)
        for name in ["preflight", "build", "init_terraform", "outputs", "publish", "install"]:
            setattr(operator, name, Mock())
        operator.tf = Mock()
        operator.deploy(yes=True)
        self.assertEqual(operator.tf.call_args_list[0].args, ("plan", "-input=false", "-out=sandbox.tfplan"))
        self.assertEqual(operator.tf.call_args_list[1].args, ("apply", "-input=false", "sandbox.tfplan"))
