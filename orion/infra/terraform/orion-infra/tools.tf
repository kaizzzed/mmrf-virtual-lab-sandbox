# Tools: AgentCore Gateway (MCP, AWS_IAM inbound) with one Lambda target `vlab` exposing the nine
# read-only server tools as vlab___<tool>; the stretch target `kg` stays off by default.
# The Lambda reaches the sandbox through CloudFront with Basic auth, so every call must finish
# inside CloudFront's 60 s origin read timeout: A4 budget 55 s HTTP < 58 s Lambda < 60 s step.
# The Gateway-to-Lambda timeout is not configurable and not documented (Open question 19).

resource "aws_cloudwatch_log_group" "lambda" {
  count             = local.create_lambda ? 1 : 0
  name              = "/aws/lambda/${var.lambda_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "tools" {
  count                          = local.create_lambda ? 1 : 0
  function_name                  = var.lambda_function_name
  description                    = "ORION read-only Virtual Lab tools behind AgentCore Gateway (synthetic sandbox)"
  role                           = aws_iam_role.lambda[0].arn
  architectures                  = ["arm64"]
  package_type                   = var.lambda_package_type
  filename                       = var.lambda_package_type == "Zip" ? var.lambda_zip_path : null
  source_code_hash               = var.lambda_package_type == "Zip" && var.lambda_zip_path != null ? filebase64sha256(var.lambda_zip_path) : null
  handler                        = var.lambda_package_type == "Zip" ? var.lambda_handler : null
  runtime                        = var.lambda_package_type == "Zip" ? "python3.12" : null
  image_uri                      = var.lambda_package_type == "Image" ? var.lambda_image_uri : null
  timeout                        = var.lambda_timeout_s
  memory_size                    = var.lambda_memory_mb
  reserved_concurrent_executions = var.lambda_reserved_concurrency
  environment {
    variables = local.lambda_environment
  }
  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.lambda[0].name
  }
  tracing_config {
    mode = "Active"
  }
  lifecycle {
    precondition {
      condition     = var.lambda_package_type == "Zip" ? var.lambda_zip_path != null : var.lambda_image_uri != null
      error_message = "Set lambda_zip_path (package type Zip) or lambda_image_uri (package type Image)."
    }
  }
  depends_on = [aws_iam_role_policy.lambda]
}

resource "aws_bedrockagentcore_gateway" "vlab" {
  count           = local.use_gateway ? 1 : 0
  name            = var.gateway_name
  description     = "ORION: typed read-only Virtual Lab tools for the run controller"
  role_arn        = aws_iam_role.gateway[0].arn
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"
  protocol_configuration {
    mcp {
      instructions = "Read-only tools over the synthetic MMRF Virtual Lab sandbox. Called only by the ORION run controller with compiled, validated inputs."
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "vlab" {
  count              = local.use_gateway ? 1 : 0
  name               = var.gateway_target_name
  gateway_identifier = aws_bedrockagentcore_gateway.vlab[0].gateway_id
  description        = "The nine ORION server tools (orion/contracts/TOOLS.md section 2)"
  credential_provider_configuration {
    gateway_iam_role {}
  }
  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tools[0].arn
        tool_schema {
          s3 {
            uri                     = "s3://${aws_s3_bucket.runs.bucket}/${local.tool_schema_key}"
            bucket_owner_account_id = local.account
          }
        }
      }
    }
  }
  lifecycle {
    # The schema is read when the target is created: a regenerated file needs a new target.
    replace_triggered_by = [aws_s3_object.vlab_tool_schema[count.index]]
  }
  depends_on = [aws_iam_role_policy.gateway, aws_s3_object.vlab_tool_schema]
}

# Stretch 10 (FR-21): the Workstream 1 knowledge-graph agent as target `kg`, tool kg___kg_query.
resource "aws_bedrockagentcore_gateway_target" "kg" {
  count              = local.use_gateway && var.enable_kg_target ? 1 : 0
  name               = var.kg_target_name
  gateway_identifier = aws_bedrockagentcore_gateway.vlab[0].gateway_id
  description        = "Workstream 1 knowledge-graph agent (stretch)"
  credential_provider_configuration {
    gateway_iam_role {}
  }
  target_configuration {
    mcp {
      lambda {
        lambda_arn = var.kg_lambda_arn
        tool_schema {
          inline_payload {
            name        = "kg_query"
            description = "Workstream 1 knowledge-graph agent (stretch; KG_NOT_CONNECTED). kg_query@1, read-only."
            input_schema {
              type = "object"
              property {
                name        = "question"
                type        = "string"
                description = "The question for the Workstream 1 knowledge-graph agent."
                required    = true
              }
              property {
                name        = "genes"
                type        = "array"
                description = "Genes the question is about."
                items {
                  type = "string"
                }
              }
            }
          }
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = var.kg_lambda_arn != null
      error_message = "enable_kg_target needs kg_lambda_arn."
    }
  }
  depends_on = [aws_iam_role_policy.gateway]
}
