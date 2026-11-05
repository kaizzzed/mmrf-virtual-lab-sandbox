# What the BFF (contract "BFF routes": ORION_* environment) and the sandbox patch need, plus the
# names an operator uses to push the image, fill the secret and read logs.

output "region" {
  value = var.region
}

output "runtime_arn" {
  description = "ORION_RUNTIME_ARN."
  value       = var.create_runtime ? aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_arn : null
}

output "runtime_id" {
  value = var.create_runtime ? aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_id : null
}

output "runtime_version" {
  value = var.create_runtime ? aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_version : null
}

output "runtime_qualifier" {
  description = "ORION_RUNTIME_QUALIFIER: the endpoint name."
  value       = var.create_runtime ? aws_bedrockagentcore_agent_runtime_endpoint.agent[0].name : null
}

output "runtime_endpoint_arn" {
  value = var.create_runtime ? aws_bedrockagentcore_agent_runtime_endpoint.agent[0].agent_runtime_endpoint_arn : null
}

output "run_bucket" {
  description = "ORION_RUN_BUCKET."
  value       = aws_s3_bucket.runs.bucket
}

output "run_prefix" {
  description = "ORION_RUN_PREFIX."
  value       = var.run_prefix
}

output "ecr_repository_url" {
  description = "Push the arm64 agent image here, tagged agent_image_tag."
  value       = aws_ecr_repository.agent.repository_url
}

output "gateway_id" {
  value = local.use_gateway ? aws_bedrockagentcore_gateway.vlab[0].gateway_id : null
}

output "gateway_url" {
  description = "ORION_GATEWAY_URL (also set on the Runtime)."
  value       = local.use_gateway ? aws_bedrockagentcore_gateway.vlab[0].gateway_url : null
}

output "gateway_target_id" {
  value = local.use_gateway ? aws_bedrockagentcore_gateway_target.vlab[0].target_id : null
}

output "tool_lambda_name" {
  value = local.create_lambda ? aws_lambda_function.tools[0].function_name : null
}

output "tool_lambda_arn" {
  value = local.create_lambda ? aws_lambda_function.tools[0].arn : null
}

output "vlab_basic_secret_arn" {
  description = "Fill once with JSON {username, password} (orion/infra/README.md); its value never appears in Terraform."
  value       = aws_secretsmanager_secret.vlab_basic.arn
}

output "runtime_role_arn" {
  value = aws_iam_role.runtime.arn
}

output "bff_role_arn" {
  value = aws_iam_role.bff.arn
}

output "pod_identity_association_id" {
  value = aws_eks_pod_identity_association.bff.association_id
}

output "log_groups" {
  description = "Log groups this module owns. Runtime stdout goes to the service-created /aws/bedrock-agentcore/runtimes/<runtime_id>-<endpoint> group."
  value = compact([
    local.create_lambda ? aws_cloudwatch_log_group.lambda[0].name : "",
    var.create_runtime && var.enable_log_delivery ? aws_cloudwatch_log_group.runtime_app[0].name : "",
    local.use_gateway && var.enable_log_delivery ? aws_cloudwatch_log_group.gateway_app[0].name : "",
  ])
}

output "bff_environment" {
  description = "The frontend's ORION_* environment in mode A (contract \"BFF routes\")."
  value = var.create_runtime ? {
    ORION_MODE              = "A"
    ORION_RUNTIME_ARN       = aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_arn
    ORION_RUNTIME_QUALIFIER = aws_bedrockagentcore_agent_runtime_endpoint.agent[0].name
    ORION_RUN_BUCKET        = aws_s3_bucket.runs.bucket
    ORION_RUN_PREFIX        = var.run_prefix
    AWS_REGION              = var.region
  } : null
}

output "helm_values" {
  description = "Values for the sandbox patch's orion block: terraform output -json helm_values > generated/aws/orion-values.json."
  value = var.create_runtime ? {
    orion = {
      enabled            = true
      mode               = "A"
      runtimeArn         = aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_arn
      runtimeQualifier   = aws_bedrockagentcore_agent_runtime_endpoint.agent[0].name
      runBucket          = aws_s3_bucket.runs.bucket
      runPrefix          = var.run_prefix
      region             = var.region
      serviceAccountName = var.frontend_service_account
    }
  } : null
}

output "iam_actions" {
  description = "Every action each principal is granted, for review against A4's principal table."
  value = {
    runtime = sort(distinct(flatten([for s in data.aws_iam_policy_document.runtime.statement : tolist(s.actions)])))
    bff     = sort(distinct(flatten([for s in data.aws_iam_policy_document.bff.statement : tolist(s.actions)])))
    gateway = local.use_gateway ? sort(distinct(flatten([for s in data.aws_iam_policy_document.gateway[0].statement : tolist(s.actions)]))) : []
    lambda  = local.create_lambda ? sort(distinct(flatten([for s in data.aws_iam_policy_document.lambda[0].statement : tolist(s.actions)]))) : []
  }
}
