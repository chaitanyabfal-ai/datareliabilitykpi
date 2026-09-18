resource "aws_s3_bucket" "raw_sensor_data" {
  bucket = var.raw_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "raw_sensor_data" {
  bucket = aws_s3_bucket.raw_sensor_data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "raw_sensor_data" {
  bucket                  = aws_s3_bucket.raw_sensor_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "raw_sensor_data" {
  bucket = aws_s3_bucket.raw_sensor_data.id

  rule {
    id     = "expire-old-raw-objects"
    status = "Enabled"
    filter {
      prefix = var.raw_prefix
    }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    expiration {
      days = 365
    }
  }
}

# S3 -> SNS event notification. The SNS topic policy (in sns_sqs.tf) grants
# s3.amazonaws.com permission to publish, scoped to this specific bucket.
resource "aws_s3_bucket_notification" "raw_sensor_data" {
  bucket = aws_s3_bucket.raw_sensor_data.id

  topic {
    topic_arn     = aws_sns_topic.sensor_data.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = var.raw_prefix
  }

  depends_on = [aws_sns_topic_policy.sensor_data]
}
