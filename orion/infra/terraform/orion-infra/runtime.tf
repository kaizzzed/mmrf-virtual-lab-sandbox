# Agent: AgentCore Runtime in PUBLIC network mode serving AG-UI (POST /invocations, GET /ping,
# /ws on 8080) from the arm64 image in ECR, and the endpoint the BFF invokes
# (ORION_RUNTIME_QUALIFIER). Each image change makes a new Runtime version; the endpoint follows it.

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  count              = var.create_runtime ? 1 : 0
  agent_runtime_name = var.runtime_name
  description        = "ORION run controller: plans, runs read-only tools, writes run records (synthetic sandbox)"
  role_arn           = aws_iam_role.runtime.arn
  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_image
    }
  }
  network_configuration {
    network_mode = "PUBLIC"
  }
  protocol_configuration {
    server_protocol = "AGUI"
  }
  environment_variables = local.runtime_environment
  lifecycle_configuration {
    idle_runtime_session_timeout = var.runtime_idle_session_timeout_s
    max_lifetime                 = var.runtime_max_lifetime_s
  }
  lifecycle {
    precondition {
      condition     = local.agent_image != null
      error_message = "Set agent_image_tag (pushed to the module's ECR repository) or agent_image_uri."
    }
    precondition {
      condition     = var.agent_image_tag == null || var.agent_image_tag != "latest"
      error_message = "Use an immutable tag such as the git commit, not latest."
    }
    precondition {
      condition     = var.tool_transport != "gateway" || var.create_tools
      error_message = "tool_transport gateway needs create_tools = true (or take cut 1: tool_transport = in_process)."
    }
    precondition {
      condition     = var.runtime_faults == "" || var.test_runtime
      error_message = "runtime_faults is honoured only by a test Runtime (test_runtime = true)."
    }
  }
  depends_on = [aws_iam_role_policy.runtime]
}

resource "aws_bedrockagentcore_agent_runtime_endpoint" "agent" {
  count                 = var.create_runtime ? 1 : 0
  name                  = var.runtime_endpoint_name
  agent_runtime_id      = aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_id
  agent_runtime_version = aws_bedrockagentcore_agent_runtime.agent[0].agent_runtime_version
  description           = "Endpoint the ORION BFF invokes (ORION_RUNTIME_QUALIFIER)"
}
