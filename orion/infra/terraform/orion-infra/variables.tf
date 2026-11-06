# Every name, ARN and model id is a variable. Defaults follow RFA rev 25 A4 and the contract
# (orion/contracts/README.md v1.2, "BFF routes", "Where records live").

# --- Account, Region, stages -------------------------------------------------------------------

variable "sandbox_account_id" {
  description = "Dedicated sandbox account ID; enforced by the AWS provider."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.sandbox_account_id))
    error_message = "Supply a twelve-digit dedicated sandbox account ID."
  }
}

variable "region" {
  description = "Region of every ORION resource and of the sandbox's EKS cluster."
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = "Extra tags on every resource."
  type        = map(string)
  default     = {}
}

variable "create_runtime" {
  description = "Create the AgentCore Runtime and its endpoint. Stage 1 sets false: the image must be in ECR first."
  type        = bool
  default     = true
}

variable "create_tools" {
  description = "Create the tool Lambda and, with tool_transport gateway, the Gateway and its targets. Stage 1 sets false: the Lambda package must exist first."
  type        = bool
  default     = true
}

variable "tool_transport" {
  description = "gateway: Runtime -> Gateway -> Lambda (mode A). in_process: cut 1, the tools run inside the Runtime, which then reads the Basic secret."
  type        = string
  default     = "gateway"
  validation {
    condition     = contains(["gateway", "in_process"], var.tool_transport)
    error_message = "tool_transport is gateway or in_process."
  }
}

# --- The sandbox it reaches --------------------------------------------------------------------

variable "vlab_origin" {
  description = "The sandbox's CloudFront HTTPS origin (sandbox terraform output https_url). The tools reach the Virtual Lab only through it, with Basic auth."
  type        = string
  validation {
    condition     = can(regex("^https://[a-z0-9]+\\.cloudfront\\.net$", var.vlab_origin))
    error_message = "Supply the deployment's CloudFront origin, such as https://d1234abcd.cloudfront.net, with no path."
  }
}

variable "eks_cluster_name" {
  description = "The sandbox's EKS cluster (sandbox terraform output cluster_name)."
  type        = string
  default     = "mmrf-synthetic"
}

variable "kubernetes_namespace" {
  description = "Namespace of the sandbox's Helm release."
  type        = string
  default     = "mmrf-synthetic"
}

variable "frontend_service_account" {
  description = "ServiceAccount the sandbox patch gives the frontend pod; the Pod Identity association binds it to the BFF role."
  type        = string
  default     = "orion-frontend"
}

variable "create_pod_identity_addon" {
  description = "Install the EKS Pod Identity Agent add-on on the sandbox cluster. Set false if the cluster already has it."
  type        = bool
  default     = true
}

variable "pod_identity_addon_version" {
  description = "eks-pod-identity-agent add-on version; null takes the cluster's default."
  type        = string
  default     = null
}

# --- Names -------------------------------------------------------------------------------------

variable "ecr_repository_name" {
  description = "ECR repository of the arm64 agent image."
  type        = string
  default     = "orion/agent"
}

variable "ecr_force_delete" {
  description = "Let destroy delete the repository with images in it."
  type        = bool
  default     = false
}

variable "agent_image_tag" {
  description = "Tag of the arm64 agent image in the module's ECR repository (tags are immutable, so use a new one per push, such as the git commit). Required with create_runtime unless agent_image_uri is set."
  type        = string
  default     = null
}

variable "agent_image_uri" {
  description = "Full arm64 image URI (repo@sha256:digest or repo:tag); overrides agent_image_tag."
  type        = string
  default     = null
}

variable "runtime_name" {
  description = "AgentCore Runtime name (letters, digits and underscores)."
  type        = string
  default     = "orion_agent"
  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}$", var.runtime_name))
    error_message = "A Runtime name starts with a letter and has at most 48 letters, digits and underscores."
  }
}

variable "runtime_endpoint_name" {
  description = "Runtime endpoint the BFF invokes; it is ORION_RUNTIME_QUALIFIER."
  type        = string
  default     = "orion_live"
  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}$", var.runtime_endpoint_name))
    error_message = "An endpoint name starts with a letter and has at most 48 letters, digits and underscores."
  }
}

variable "runtime_role_name" {
  description = "Runtime execution role (orion-runtime in A4)."
  type        = string
  default     = "orion-runtime"
}

variable "bff_role_name" {
  description = "BFF role, assumed through EKS Pod Identity (orion-bff in A4)."
  type        = string
  default     = "orion-bff"
}

variable "bff_extra_trusted_principal_arns" {
  description = "IAM role ARNs that may also assume the BFF role: mode H, where the BFF runs locally on a developer profile."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.bff_extra_trusted_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:role/.+", arn))])
    error_message = "List IAM role ARNs only."
  }
}

variable "gateway_name" {
  description = "AgentCore Gateway name."
  type        = string
  default     = "orion-vlab"
}

variable "gateway_role_name" {
  description = "Gateway service role: lambda:InvokeFunction on the tool Lambda only."
  type        = string
  default     = "orion-gateway"
}

variable "gateway_target_name" {
  description = "Lambda target name. The agent names tools <target>___<tool> with target vlab (tools/base.py GATEWAY_TARGET); change both together."
  type        = string
  default     = "vlab"
}

variable "vlab_tool_schema_path" {
  description = "The vlab target's tool schema (JSON array of tool definitions), generated by orion/infra/gateway_tools.py."
  type        = string
  default     = null
}

variable "enable_kg_target" {
  description = "Stretch: register the Workstream 1 knowledge-graph Lambda as the Gateway target kg (FR-21). Off by default; kg_query answers KG_NOT_CONNECTED until then."
  type        = bool
  default     = false
}

variable "kg_target_name" {
  description = "Name of the stretch knowledge-graph target (tools/base.py KG_GATEWAY_TARGET)."
  type        = string
  default     = "kg"
}

variable "kg_lambda_arn" {
  description = "Workstream 1's Lambda behind kg___kg_query; required with enable_kg_target."
  type        = string
  default     = null
}

variable "lambda_function_name" {
  description = "The tool Lambda."
  type        = string
  default     = "orion-vlab-tools"
}

variable "lambda_role_name" {
  description = "The tool Lambda's execution role."
  type        = string
  default     = "orion-vlab-tools"
}

variable "lambda_package_type" {
  description = "Zip (lambda_zip_path, python3.12 on arm64) or Image (lambda_image_uri, an arm64 Lambda image)."
  type        = string
  default     = "Zip"
  validation {
    condition     = contains(["Zip", "Image"], var.lambda_package_type)
    error_message = "lambda_package_type is Zip or Image."
  }
}

variable "lambda_zip_path" {
  description = "Path of the tool Lambda's zip (orion_agent, its dependencies for linux arm64 and the golden bodies)."
  type        = string
  default     = null
}

variable "lambda_handler" {
  description = "Handler inside the zip (orion/agent/lambda/build.py packages handler.py at the zip root)."
  type        = string
  default     = "handler.handler"
}

variable "lambda_image_uri" {
  description = "arm64 Lambda container image, with lambda_package_type Image."
  type        = string
  default     = null
}

variable "lambda_timeout_s" {
  description = "A4 timeout budget: above the 55 s tool HTTP timeout and below the executor's 60 s step timeout."
  type        = number
  default     = 58
  validation {
    condition     = var.lambda_timeout_s > 55 && var.lambda_timeout_s < 60 && floor(var.lambda_timeout_s) == var.lambda_timeout_s
    error_message = "The Lambda timeout must sit between the 55 s HTTP timeout and the 60 s step timeout (A4): 56 to 59 s."
  }
}

variable "lambda_memory_mb" {
  description = "Tool Lambda memory (orion/agent/lambda/build.py manifest memoryMb)."
  type        = number
  default     = 512
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrency for the tool Lambda; -1 leaves it unreserved. The executor is serial, so a few suffice."
  type        = number
  default     = -1
}

variable "lambda_golden_dir" {
  description = "ORION_GOLDEN_DIR for the Lambda; null keeps the golden snapshot packaged with the tools (tools/golden_snapshot.json)."
  type        = string
  default     = null
}

variable "lambda_environment" {
  description = "Extra tool Lambda environment variables, merged last."
  type        = map(string)
  default     = {}
}

variable "vlab_basic_secret_name" {
  description = "Secrets Manager secret holding the sandbox's Basic credential as JSON {username, password}. Created empty; filled by one operator command."
  type        = string
  default     = "orion/vlab-basic"
}

variable "secret_recovery_window_days" {
  description = "Days a deleted secret stays recoverable; 0 deletes at once so a redeploy can reuse the name."
  type        = number
  default     = 0
}

variable "run_bucket_name" {
  description = "Run-record bucket; null makes orion-runs-<account>-<region>."
  type        = string
  default     = null
}

variable "run_prefix" {
  description = "ORION_RUN_PREFIX: records at <prefix>runs/<runId>.json, thread indexes and the session ledger at <prefix>threads/<threadId>.json."
  type        = string
  default     = "orion/"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9/_-]*/$", var.run_prefix)) && !can(regex("//", var.run_prefix))
    error_message = "run_prefix is a non-empty key prefix ending in /, such as orion/."
  }
}

variable "run_retention_days" {
  description = "Run records expire this many days after their last write (Open question 23 proposes 30)."
  type        = number
  default     = 30
}

variable "run_noncurrent_retention_days" {
  description = "Overwritten record versions (one per transition and lease renewal) expire after this many days."
  type        = number
  default     = 7
}

variable "bucket_force_destroy" {
  description = "Let destroy empty the run bucket first, including every record version."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "Retention of the log groups this module creates."
  type        = number
  default     = 30
}

# --- Model configuration (A4 "Model configuration on Bedrock") ---------------------------------

variable "planner_model_id" {
  description = "ORION_PLANNER_MODEL: Claude Opus 5.5. bedrock-runtime needs the inference profile (us.); the mantle client takes the base id anthropic.claude-opus-5-5."
  type        = string
  default     = "us.anthropic.claude-opus-5-5"
}

variable "composer_model_id" {
  description = "ORION_COMPOSER_MODEL: Claude Opus 5.5."
  type        = string
  default     = "us.anthropic.claude-opus-5-5"
}

variable "fallback_model_id" {
  description = "ORION_FALLBACK_MODEL: the client-side refusal fallback for both roles, Claude Opus 5."
  type        = string
  default     = "us.anthropic.claude-opus-5"
}

variable "bedrock_client" {
  description = "ORION_BEDROCK_CLIENT: runtime (bedrock-runtime, inference-profile ids) or mantle (the Messages-API endpoint, base ids)."
  type        = string
  default     = "runtime"
  validation {
    condition     = contains(["runtime", "mantle"], var.bedrock_client)
    error_message = "bedrock_client is runtime or mantle."
  }
}

variable "planner" {
  description = "ORION_PLANNER on the Runtime: bedrock, or stub for a model-free smoke test."
  type        = string
  default     = "bedrock"
  validation {
    condition     = contains(["bedrock", "stub"], var.planner)
    error_message = "planner is bedrock or stub."
  }
}

variable "composer" {
  description = "ORION_COMPOSER on the Runtime: bedrock, or stub."
  type        = string
  default     = "bedrock"
  validation {
    condition     = contains(["bedrock", "stub"], var.composer)
    error_message = "composer is bedrock or stub."
  }
}

variable "bedrock_invoke_actions" {
  description = "IAM actions the Runtime may use on the three models. Which action covers Bedrock's Messages-API endpoint is unverified (Open question 18); add it here once the day-1 access check names it."
  type        = list(string)
  default     = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
}

# --- Runtime settings --------------------------------------------------------------------------

variable "runtime_environment" {
  description = "Extra Runtime environment variables, merged last, such as ORION_DE_LAUNCHER, ORION_DEFAULT_SCOPE, ORION_PLANNER_EFFORT or ORION_MODEL_CACHE_TTL."
  type        = map(string)
  default     = {}
}

variable "test_runtime" {
  description = "A test Runtime (ORION_TEST_RUNTIME=1): honours runtime_faults. Never for the demo Runtime."
  type        = bool
  default     = false
}

variable "runtime_faults" {
  description = "ORION_FAULTS for a test Runtime, such as de_run:timeout,planner:refuse (E11). Needs test_runtime."
  type        = string
  default     = ""
}

variable "runtime_idle_session_timeout_s" {
  description = "Idle seconds before AgentCore recycles a session's microVM. Records live in S3, so a paused run survives it."
  type        = number
  default     = 900
}

variable "runtime_max_lifetime_s" {
  description = "Maximum seconds a Runtime session lives."
  type        = number
  default     = 28800
}

# --- Observability (A4 "Observability and evaluation") -----------------------------------------

variable "enable_log_delivery" {
  description = "Deliver the Runtime's and the Gateway's application logs to log groups this module owns (CloudWatch vended logs)."
  type        = bool
  default     = true
}

variable "enable_trace_delivery" {
  description = "Deliver Runtime and Gateway spans to X-Ray. Needs CloudWatch Transaction Search, an account-level setting this module does not change."
  type        = bool
  default     = false
}
