# Run records and the session ledger (S3), the Basic credential (Secrets Manager) and the agent
# image repository (ECR). A4 "Data": SSE-S3, versioning, public access blocked, 30-day lifecycle;
# a secret created empty and filled by one operator command.

resource "aws_s3_bucket" "runs" {
  bucket        = local.run_bucket
  force_destroy = var.bucket_force_destroy
}

resource "aws_s3_bucket_ownership_controls" "runs" {
  bucket = aws_s3_bucket.runs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "runs" {
  bucket                  = aws_s3_bucket.runs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "runs" {
  bucket = aws_s3_bucket.runs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "runs" {
  bucket = aws_s3_bucket.runs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "runs" {
  bucket = aws_s3_bucket.runs.id
  rule {
    id     = "expire-run-records"
    status = "Enabled"
    filter {
      prefix = var.run_prefix
    }
    expiration {
      days = var.run_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.run_noncurrent_retention_days
    }
  }
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
  depends_on = [aws_s3_bucket_versioning.runs]
}

resource "aws_s3_bucket_policy" "runs" {
  bucket     = aws_s3_bucket.runs.id
  policy     = data.aws_iam_policy_document.runs_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.runs]
}

data "aws_iam_policy_document" "runs_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.runs.arn, "${aws_s3_bucket.runs.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# The vlab target's MCP tool schema, generated from the catalog (orion/infra/gateway_tools.py).
resource "aws_s3_object" "vlab_tool_schema" {
  count        = local.use_gateway ? 1 : 0
  bucket       = aws_s3_bucket.runs.id
  key          = local.tool_schema_key
  source       = local.tool_schema_path
  etag         = filemd5(local.tool_schema_path)
  content_type = "application/json"
  depends_on   = [aws_s3_bucket_server_side_encryption_configuration.runs]
}

resource "aws_secretsmanager_secret" "vlab_basic" {
  name                    = var.vlab_basic_secret_name
  description             = "Sandbox CloudFront Basic credential as JSON {username, password}, read only by the ORION tool Lambda. Synthetic sandbox only."
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_ecr_repository" "agent" {
  name                 = var.ecr_repository_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.ecr_force_delete
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent agent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
