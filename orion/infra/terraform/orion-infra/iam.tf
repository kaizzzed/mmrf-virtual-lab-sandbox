# A4 "Each principal gets only what its job needs":
#   orion-bff          InvokeAgentRuntime and StopRuntimeSession on the one Runtime; s3:GetObject on
#                      the run-record prefix. Not Bedrock, S3 writes, Gateway or secrets.
#   orion-runtime      The planner, composer and fallback models; InvokeGateway; run records read and
#                      write; logs, traces, ECR pull. Not the Basic secret (except under cut 1).
#   Gateway role       lambda:InvokeFunction on the tool Lambda (and reads its tool schema).
#   orion-vlab-tools   secretsmanager:GetSecretValue on orion/vlab-basic; logs.

# --- orion-runtime -----------------------------------------------------------------------------

# Only this module's Runtime may assume the role: its ARN is runtime/<runtime_name>-<10 characters>
# and its endpoints sit under it (runtime/<id>/runtime-endpoint/<name>). A Runtime name has no
# hyphen, so the prefix matches no other Runtime, and no Gateway, code interpreter or browser.
data "aws_iam_policy_document" "runtime_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${local.account}:runtime/${var.runtime_name}-*"]
    }
  }
}

resource "aws_iam_role" "runtime" {
  name               = var.runtime_role_name
  description        = "ORION AgentCore Runtime execution role"
  assume_role_policy = data.aws_iam_policy_document.runtime_trust.json
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid       = "PullAgentImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [aws_ecr_repository.agent.arn]
  }
  statement {
    sid       = "EcrToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "RuntimeLogGroups"
    actions   = ["logs:CreateLogGroup", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${var.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }
  statement {
    sid       = "DescribeLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${local.account}:log-group:*"]
  }
  statement {
    sid       = "RuntimeLogEvents"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
  }
  statement {
    sid       = "Traces"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }
  statement {
    sid = "WorkloadAccessToken"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${local.account}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.region}:${local.account}:workload-identity-directory/default/workload-identity/${var.runtime_name}-*",
    ]
  }
  # The inference profiles themselves (and any base id, unconditioned in var.region only).
  statement {
    sid       = "PlannerComposerFallbackModels"
    actions   = var.bedrock_invoke_actions
    resources = concat(local.inference_profile_arns, local.direct_model_arns)
  }
  # A profile's foundation model in the Regions it routes to, only through one of those profiles
  # (AWS's documented pattern: Bedrock sets bedrock:InferenceProfileArn on the routed call). A direct
  # InvokeModel on the base model, in any Region, carries no such key and is not allowed.
  dynamic "statement" {
    for_each = length(local.profile_ids) > 0 ? [1] : []
    content {
      sid       = "ModelsOnlyThroughProfiles"
      actions   = var.bedrock_invoke_actions
      resources = local.profile_model_arns
      condition {
        test     = "StringLike"
        variable = "bedrock:InferenceProfileArn"
        values   = local.inference_profile_arns
      }
    }
  }
  statement {
    sid     = "RunRecordsAndLedger"
    actions = ["s3:GetObject", "s3:PutObject"]
    resources = [
      "${aws_s3_bucket.runs.arn}/${local.runs_prefix}*",
      "${aws_s3_bucket.runs.arn}/${local.threads_prefix}*",
    ]
  }
  # Without ListBucket a missing record reads as 403, not 404, and run_store.py treats only 404 as
  # missing (a new thread's ledger read). No s3:prefix condition: a GetObject carries no s3:prefix
  # key, so S3's ListBucket check for the 404 would fail it. The bucket holds only ORION data.
  statement {
    sid       = "ListRunRecords"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.runs.arn]
  }
  dynamic "statement" {
    for_each = local.use_gateway ? [aws_bedrockagentcore_gateway.vlab[0].gateway_arn] : []
    content {
      sid       = "InvokeToolGateway"
      actions   = ["bedrock-agentcore:InvokeGateway"]
      resources = [statement.value]
    }
  }
  # Cut 1 only: with in-process tools the Runtime sends the Basic header itself (Delivery cut).
  dynamic "statement" {
    for_each = local.in_process ? [aws_secretsmanager_secret.vlab_basic.arn] : []
    content {
      sid       = "BasicSecretInProcessTools"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "orion-runtime"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}

# --- orion-bff (EKS Pod Identity) --------------------------------------------------------------

# Pod Identity on the sandbox cluster, and only for the frontend's ServiceAccount: EKS passes the
# kubernetes-namespace and kubernetes-service-account session tags on every AssumeRole (the EKS
# guide's trust conditions), so another association on this role, in any namespace, gets nothing.
# The association must keep session tags on (eks.tf, disable_session_tags = false).
data "aws_iam_policy_document" "bff_trust" {
  statement {
    sid     = "EksPodIdentity"
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.cluster_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-namespace"
      values   = [var.kubernetes_namespace]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = [var.frontend_service_account]
    }
  }
  # Mode H: the BFF runs locally and assumes this role from a developer profile.
  dynamic "statement" {
    for_each = length(var.bff_extra_trusted_principal_arns) > 0 ? [var.bff_extra_trusted_principal_arns] : []
    content {
      sid     = "ModeHDevelopers"
      actions = ["sts:AssumeRole"]
      principals {
        type        = "AWS"
        identifiers = statement.value
      }
    }
  }
}

resource "aws_iam_role" "bff" {
  name               = var.bff_role_name
  description        = "ORION BFF in the frontend pod: invoke and stop the Runtime, read run records"
  assume_role_policy = data.aws_iam_policy_document.bff_trust.json
}

data "aws_iam_policy_document" "bff" {
  dynamic "statement" {
    for_each = var.create_runtime ? [1] : []
    content {
      sid     = "InvokeAndStopRuntime"
      actions = ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"]
      resources = [
        aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_arn,
        aws_bedrockagentcore_agent_runtime_endpoint.agent[0].agent_runtime_endpoint_arn,
      ]
    }
  }
  statement {
    sid       = "ReadRunRecords"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.runs.arn}/${local.runs_prefix}*"]
  }
}

resource "aws_iam_role_policy" "bff" {
  name   = "orion-bff"
  role   = aws_iam_role.bff.id
  policy = data.aws_iam_policy_document.bff.json
}

# --- Gateway service role ----------------------------------------------------------------------

# Only this module's Gateway may assume the role (the AgentCore guide's gateway/<name>-* pattern).
# A Gateway id is its name in lower case, a hyphen and 10 characters, hence lower().
data "aws_iam_policy_document" "gateway_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${local.account}:gateway/${lower(var.gateway_name)}-*"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  count              = local.use_gateway ? 1 : 0
  name               = var.gateway_role_name
  description        = "ORION AgentCore Gateway: invoke the tool Lambda only"
  assume_role_policy = data.aws_iam_policy_document.gateway_trust.json
}

data "aws_iam_policy_document" "gateway" {
  count = local.use_gateway ? 1 : 0
  statement {
    sid       = "InvokeToolLambdas"
    actions   = ["lambda:InvokeFunction"]
    resources = compact([aws_lambda_function.tools[0].arn, var.enable_kg_target && var.kg_lambda_arn != null ? var.kg_lambda_arn : ""])
  }
  statement {
    sid       = "ReadToolSchema"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.runs.arn}/${local.tool_schema_key}"]
  }
}

resource "aws_iam_role_policy" "gateway" {
  count  = local.use_gateway ? 1 : 0
  name   = "orion-gateway"
  role   = aws_iam_role.gateway[0].id
  policy = data.aws_iam_policy_document.gateway[0].json
}

# --- orion-vlab-tools (the tool Lambda) --------------------------------------------------------

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  count              = local.create_lambda ? 1 : 0
  name               = var.lambda_role_name
  description        = "ORION tool Lambda: the Basic secret and its own logs"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "lambda" {
  count = local.create_lambda ? 1 : 0
  statement {
    sid       = "ReadBasicCredential"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.vlab_basic.arn]
  }
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda[0].arn}:*"]
  }
  statement {
    sid       = "Traces"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  count  = local.create_lambda ? 1 : 0
  name   = "orion-vlab-tools"
  role   = aws_iam_role.lambda[0].id
  policy = data.aws_iam_policy_document.lambda[0].json
}
