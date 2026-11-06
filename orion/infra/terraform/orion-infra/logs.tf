# CloudWatch: the tool Lambda's log group is in tools.tf. The Runtime writes its container stdout
# (plain-text Python logging and uvicorn access lines) to the service-created groups
# /aws/bedrock-agentcore/runtimes/<id>-<endpoint>, one per endpoint, created on first use and never
# expiring until the operator sets their retention (README, deploy step 6). The vended deliveries
# below add application logs, and optionally spans, under groups this module owns, so their
# retention is set here and destroy removes them.

resource "aws_cloudwatch_log_group" "runtime_app" {
  count             = var.create_runtime && var.enable_log_delivery ? 1 : 0
  name              = "/aws/vendedlogs/bedrock-agentcore/runtime/${var.runtime_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_delivery_source" "runtime_app" {
  count        = var.create_runtime && var.enable_log_delivery ? 1 : 0
  name         = "${var.runtime_name}-app-logs"
  log_type     = "APPLICATION_LOGS"
  resource_arn = aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_arn
}

resource "aws_cloudwatch_log_delivery_destination" "runtime_app" {
  count = var.create_runtime && var.enable_log_delivery ? 1 : 0
  name  = "${var.runtime_name}-app-logs"
  delivery_destination_configuration {
    destination_resource_arn = aws_cloudwatch_log_group.runtime_app[0].arn
  }
}

resource "aws_cloudwatch_log_delivery" "runtime_app" {
  count                    = var.create_runtime && var.enable_log_delivery ? 1 : 0
  delivery_source_name     = aws_cloudwatch_log_delivery_source.runtime_app[0].name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.runtime_app[0].arn
}

resource "aws_cloudwatch_log_group" "gateway_app" {
  count             = local.use_gateway && var.enable_log_delivery ? 1 : 0
  name              = "/aws/vendedlogs/bedrock-agentcore/gateway/${var.gateway_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_delivery_source" "gateway_app" {
  count        = local.use_gateway && var.enable_log_delivery ? 1 : 0
  name         = "${var.gateway_name}-app-logs"
  log_type     = "APPLICATION_LOGS"
  resource_arn = aws_bedrockagentcore_gateway.vlab[0].gateway_arn
}

resource "aws_cloudwatch_log_delivery_destination" "gateway_app" {
  count = local.use_gateway && var.enable_log_delivery ? 1 : 0
  name  = "${var.gateway_name}-app-logs"
  delivery_destination_configuration {
    destination_resource_arn = aws_cloudwatch_log_group.gateway_app[0].arn
  }
}

resource "aws_cloudwatch_log_delivery" "gateway_app" {
  count                    = local.use_gateway && var.enable_log_delivery ? 1 : 0
  delivery_source_name     = aws_cloudwatch_log_delivery_source.gateway_app[0].name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.gateway_app[0].arn
}

# Spans (turn -> plan -> compile -> step -> compose -> verify) to X-Ray, off by default.
locals {
  trace_sources = merge(
    var.create_runtime && var.enable_trace_delivery ? { runtime = aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_arn } : {},
    local.use_gateway && var.enable_trace_delivery ? { gateway = aws_bedrockagentcore_gateway.vlab[0].gateway_arn } : {},
  )
}

resource "aws_cloudwatch_log_delivery_source" "traces" {
  for_each     = local.trace_sources
  name         = "orion-${each.key}-traces"
  log_type     = "TRACES"
  resource_arn = each.value
}

resource "aws_cloudwatch_log_delivery_destination" "xray" {
  count                     = var.enable_trace_delivery ? 1 : 0
  name                      = "orion-xray"
  delivery_destination_type = "XRAY"
}

resource "aws_cloudwatch_log_delivery" "traces" {
  for_each                 = local.trace_sources
  delivery_source_name     = aws_cloudwatch_log_delivery_source.traces[each.key].name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.xray[0].arn
}
