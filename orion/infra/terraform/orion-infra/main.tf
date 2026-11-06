locals {
  account = var.sandbox_account_id

  # Stages and the cut-1 switch (RFA rev 25, "Cut first, if behind" 1: Gateway to in-process tools).
  use_gateway   = var.create_tools && var.tool_transport == "gateway"
  create_lambda = local.use_gateway
  in_process    = var.tool_transport == "in_process"

  run_bucket     = coalesce(var.run_bucket_name, "orion-runs-${var.sandbox_account_id}-${var.region}")
  runs_prefix    = "${var.run_prefix}runs/"
  threads_prefix = "${var.run_prefix}threads/"

  # The Gateway's tool schema sits outside the run prefix, so the record lifecycle never expires it.
  tool_schema_key  = "gateway/${var.gateway_target_name}-tools.json"
  tool_schema_path = coalesce(var.vlab_tool_schema_path, "${path.module}/vlab-tools.json")

  cluster_arn = "arn:aws:eks:${var.region}:${local.account}:cluster/${var.eks_cluster_name}"

  agent_image = var.agent_image_uri != null ? var.agent_image_uri : (
    var.agent_image_tag != null ? "${aws_ecr_repository.agent.repository_url}:${var.agent_image_tag}" : null
  )

  # Bedrock resources for the planner, composer and fallback ids. A cross-Region inference profile
  # (us., eu., apac., global. ...) needs the profile and the foundation model in every Region it routes
  # to (us. routes to us-east-1, us-east-2 and us-west-2; global. also needs the Region-less model ARN,
  # which the * Region matches). The foundation model is granted only through these profiles
  # (bedrock:InferenceProfileArn), so it can never be called directly in any Region. A base id (the
  # mantle client) is granted as the foundation model in var.region.
  profile_pattern = "^(us|eu|apac|global|us-gov|jp|au|ca)\\."
  model_ids       = distinct([var.planner_model_id, var.composer_model_id, var.fallback_model_id])
  profile_ids     = [for id in local.model_ids : id if can(regex(local.profile_pattern, id))]
  inference_profile_arns = [
    for id in local.profile_ids : "arn:aws:bedrock:${var.region}:${local.account}:inference-profile/${id}"
  ]
  profile_model_arns = distinct([
    for id in local.profile_ids : "arn:aws:bedrock:*::foundation-model/${replace(id, "/^[a-z-]+\\./", "")}"
  ])
  direct_model_arns = [
    for id in local.model_ids : "arn:aws:bedrock:${var.region}::foundation-model/${id}" if !can(regex(local.profile_pattern, id))
  ]

  # Runtime environment: contract "Where records live", the agent's settings.py, tools/config.py and
  # model/config.py. Each reader ignores the names it does not use.
  runtime_environment = merge(
    {
      ORION_MODE           = "A"
      ORION_RUN_BUCKET     = local.run_bucket
      ORION_RUN_PREFIX     = var.run_prefix
      ORION_PLANNER        = var.planner
      ORION_COMPOSER       = var.composer
      ORION_EXECUTOR       = "engine"
      ORION_TOOLS          = local.in_process ? "inprocess" : "gateway" # settings.py
      ORION_TOOL_TRANSPORT = var.tool_transport                         # tools/config.py
      ORION_BEDROCK_CLIENT = var.bedrock_client
      ORION_BEDROCK_REGION = var.region
      ORION_PLANNER_MODEL  = var.planner_model_id
      ORION_COMPOSER_MODEL = var.composer_model_id
      ORION_FALLBACK_MODEL = var.fallback_model_id
    },
    local.use_gateway ? {
      ORION_GATEWAY_URL    = aws_bedrockagentcore_gateway.vlab[0].gateway_url
      ORION_GATEWAY_TARGET = var.gateway_target_name
    } : {},
    local.in_process ? {
      ORION_VLAB_ORIGIN           = var.vlab_origin
      ORION_VLAB_BASIC_SECRET_ARN = aws_secretsmanager_secret.vlab_basic.arn
    } : {},
    var.test_runtime ? { ORION_TEST_RUNTIME = "1" } : {},
    var.test_runtime && var.runtime_faults != "" ? { ORION_FAULTS = var.runtime_faults } : {},
    var.runtime_environment,
  )

  # The tool Lambda (orion/agent/lambda/handler.py): the origin and the secret's ARN, never its value.
  lambda_environment = merge(
    {
      ORION_MODE                  = "A"
      ORION_VLAB_ORIGIN           = var.vlab_origin
      ORION_VLAB_BASIC_SECRET_ARN = aws_secretsmanager_secret.vlab_basic.arn
    },
    var.lambda_golden_dir != null ? { ORION_GOLDEN_DIR = var.lambda_golden_dir } : {},
    var.test_runtime ? { ORION_TEST_RUNTIME = "1" } : {},
    var.test_runtime && var.runtime_faults != "" ? { ORION_FAULTS = var.runtime_faults } : {},
    var.lambda_environment,
  )
}
