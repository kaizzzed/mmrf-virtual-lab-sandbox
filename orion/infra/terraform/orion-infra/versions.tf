# orion-infra: ORION Workstream 2 on AWS (RFA rev 25, A4 "Deployment"). A root module with its own
# local state, deployed beside the sandbox's terraform/ and never sharing its state.
# hashicorp/aws 6.72.0 is the version `terraform init` resolved on 2026-11-05; it has the
# bedrockagentcore resources (agent runtime with server_protocol AGUI, gateway, gateway target).
terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.69" }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.sandbox_account_id]
  default_tags {
    tags = merge({
      Project     = "mmrf-virtual-lab-sandbox"
      Component   = "orion"
      Data        = "synthetic-only"
      Environment = "sandbox"
    }, var.tags)
  }
}
