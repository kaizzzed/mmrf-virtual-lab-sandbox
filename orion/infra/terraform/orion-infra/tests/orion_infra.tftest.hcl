# Plan-only checks with a mocked AWS provider: no credentials, no AWS call. Run with
# `terraform test` in this directory (orion/infra/README.md, "Checks"). Every value is synthetic.
mock_provider "aws" {
  # A mocked policy document's json is a random string, which the IAM and S3 policy arguments
  # reject at plan time; the statements the assertions read are configuration, so they stay real.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  sandbox_account_id  = "123456789012"
  vlab_origin         = "https://d1234abcd.cloudfront.net"
  agent_image_tag     = "0123abc"
  lambda_package_type = "Image"
  lambda_image_uri    = "123456789012.dkr.ecr.us-east-1.amazonaws.com/orion/tools:0123abc"
}

run "mode_a_defaults" {
  command = plan
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.agent[0].protocol_configuration[0].server_protocol == "AGUI" && aws_bedrockagentcore_agent_runtime.agent[0].network_configuration[0].network_mode == "PUBLIC"
    error_message = "The Runtime serves AG-UI in PUBLIC network mode (A4)."
  }
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_artifact[0].container_configuration[0].container_uri != null
    error_message = "The Runtime runs the agent image from ECR."
  }
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_MODE"] == "A" && aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_TOOLS"] == "gateway" && aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_TOOL_TRANSPORT"] == "gateway" && aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_RUN_PREFIX"] == "orion/"
    error_message = "The Runtime runs mode A with Gateway tools and the contract's record prefix."
  }
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_PLANNER"] == "bedrock" && aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_PLANNER_MODEL"] == "us.anthropic.claude-opus-5-5" && aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_FALLBACK_MODEL"] == "us.anthropic.claude-opus-5"
    error_message = "The Runtime plans with Opus 5.5 through its inference profile, with Opus 5 as the refusal fallback."
  }
  assert {
    condition     = !contains(keys(aws_bedrockagentcore_agent_runtime.agent[0].environment_variables), "ORION_FAULTS") && !contains(keys(aws_bedrockagentcore_agent_runtime.agent[0].environment_variables), "ORION_VLAB_BASIC_SECRET_ARN")
    error_message = "The demo Runtime has no faults and never names the Basic secret."
  }
  assert {
    condition     = aws_bedrockagentcore_gateway.vlab[0].authorizer_type == "AWS_IAM" && aws_bedrockagentcore_gateway.vlab[0].protocol_type == "MCP"
    error_message = "The Gateway speaks MCP with IAM inbound auth."
  }
  assert {
    condition     = aws_bedrockagentcore_gateway_target.vlab[0].name == "vlab" && length(aws_bedrockagentcore_gateway_target.kg) == 0
    error_message = "One Lambda target vlab; the stretch kg target is off by default."
  }
  assert {
    condition     = aws_lambda_function.tools[0].timeout == 58 && tolist(aws_lambda_function.tools[0].architectures) == tolist(["arm64"])
    error_message = "The tool Lambda runs on arm64 at the A4 timeout of 58 s."
  }
  assert {
    condition     = aws_lambda_function.tools[0].environment[0].variables["ORION_VLAB_ORIGIN"] == "https://d1234abcd.cloudfront.net"
    error_message = "The tool Lambda reaches the sandbox through CloudFront."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.runs.block_public_acls && aws_s3_bucket_public_access_block.runs.block_public_policy && aws_s3_bucket_public_access_block.runs.ignore_public_acls && aws_s3_bucket_public_access_block.runs.restrict_public_buckets
    error_message = "The run bucket blocks all public access."
  }
  assert {
    condition     = alltrue([for r in aws_s3_bucket_server_side_encryption_configuration.runs.rule : alltrue([for d in r.apply_server_side_encryption_by_default : d.sse_algorithm == "AES256"])])
    error_message = "Run records are encrypted with SSE-S3."
  }
  assert {
    condition     = aws_s3_bucket_versioning.runs.versioning_configuration[0].status == "Enabled"
    error_message = "Run records are versioned."
  }
  assert {
    condition     = alltrue([for r in aws_s3_bucket_lifecycle_configuration.runs.rule : r.id != "expire-run-records" || alltrue([for e in r.expiration : e.days == 30])])
    error_message = "Run records expire after 30 days."
  }
  assert {
    condition     = output.iam_actions.bff == tolist(["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession", "s3:GetObject"])
    error_message = "The BFF may only invoke and stop the Runtime and read run records."
  }
  assert {
    condition     = contains(output.iam_actions.runtime, "bedrock-agentcore:InvokeGateway") && contains(output.iam_actions.runtime, "s3:PutObject") && !contains(output.iam_actions.runtime, "secretsmanager:GetSecretValue")
    error_message = "The Runtime invokes the Gateway and writes records, and cannot read the Basic secret."
  }
  assert {
    condition     = alltrue([for s in data.aws_iam_policy_document.runtime.statement : s.sid != "ListRunRecords" || length(s.condition) == 0])
    error_message = "The Runtime's ListBucket has no s3:prefix condition, so a missing record or ledger reads as 404, not 403."
  }
  assert {
    condition     = output.iam_actions.gateway == tolist(["lambda:InvokeFunction", "s3:GetObject"])
    error_message = "The Gateway role only invokes the tool Lambda (and reads its tool schema)."
  }
  assert {
    condition     = contains(output.iam_actions.lambda, "secretsmanager:GetSecretValue") && length([for a in output.iam_actions.lambda : a if startswith(a, "bedrock") || startswith(a, "s3:")]) == 0
    error_message = "Only the tool Lambda reads the Basic secret, and it has no Bedrock or S3 access."
  }
  assert {
    condition     = aws_eks_pod_identity_association.bff.service_account == "orion-frontend" && aws_eks_pod_identity_association.bff.namespace == "mmrf-synthetic" && length(aws_eks_addon.pod_identity) == 1
    error_message = "Pod Identity binds the frontend's orion-frontend ServiceAccount."
  }
}

run "cut_1_in_process_tools" {
  command = plan
  variables {
    tool_transport = "in_process"
  }
  assert {
    condition     = length(aws_bedrockagentcore_gateway.vlab) == 0 && length(aws_lambda_function.tools) == 0
    error_message = "Cut 1 drops the Gateway and the Lambda."
  }
  assert {
    condition     = contains(output.iam_actions.runtime, "secretsmanager:GetSecretValue") && aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_TOOL_TRANSPORT"] == "in_process" && aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_TOOLS"] == "inprocess"
    error_message = "Under cut 1 the Runtime runs the tools and needs the Basic secret."
  }
}

run "stage_1_base" {
  command = plan
  variables {
    create_runtime  = false
    create_tools    = false
    agent_image_tag = null
  }
  assert {
    condition     = length(aws_bedrockagentcore_agent_runtime.agent) == 0 && length(aws_lambda_function.tools) == 0 && output.runtime_arn == null
    error_message = "Stage 1 creates no Runtime and no tools."
  }
  assert {
    condition     = output.iam_actions.bff == tolist(["s3:GetObject"])
    error_message = "Without a Runtime the BFF can only read records."
  }
}

run "test_runtime_with_faults" {
  command = plan
  variables {
    test_runtime   = true
    runtime_faults = "de_run:timeout,composer:refuse"
  }
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_FAULTS"] == "de_run:timeout,composer:refuse" && aws_bedrockagentcore_agent_runtime.agent[0].environment_variables["ORION_TEST_RUNTIME"] == "1"
    error_message = "A test Runtime carries ORION_TEST_RUNTIME and its faults."
  }
}

run "reject_faults_on_demo_runtime" {
  command = plan
  variables {
    runtime_faults = "de_run:timeout"
  }
  expect_failures = [aws_bedrockagentcore_agent_runtime.agent]
}

run "reject_lambda_timeout_outside_budget" {
  command = plan
  variables {
    lambda_timeout_s = 60
  }
  expect_failures = [var.lambda_timeout_s]
}

run "reject_origin_other_than_cloudfront" {
  command = plan
  variables {
    vlab_origin = "http://identity:8080"
  }
  expect_failures = [var.vlab_origin]
}

run "kg_stretch_target" {
  command = plan
  variables {
    enable_kg_target = true
    kg_lambda_arn    = "arn:aws:lambda:us-east-1:123456789012:function:ws1-kg"
  }
  assert {
    condition     = length(aws_bedrockagentcore_gateway_target.kg) == 1 && aws_bedrockagentcore_gateway_target.kg[0].name == "kg"
    error_message = "The stretch target is kg."
  }
}

run "global_inference_profile" {
  command = plan
  variables {
    planner_model_id = "global.anthropic.claude-opus-5-5"
  }
  assert {
    condition     = anytrue([for s in data.aws_iam_policy_document.runtime.statement : s.sid == "PlannerComposerFallbackModels" && contains(tolist(s.resources), "arn:aws:bedrock:us-east-1:123456789012:inference-profile/global.anthropic.claude-opus-5-5") && contains(tolist(s.resources), "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5")])
    error_message = "A cross-Region profile grants the profile and its foundation model in every Region."
  }
}
