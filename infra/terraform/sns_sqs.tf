data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# SNS topic: receives S3 ObjectCreated events, fans out to SQS (and, later,
# to any other subscriber -- e.g. an anomaly-detection consumer -- without
# touching the S3 bucket configuration again).
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "sensor_data" {
  name = var.sns_topic_name
  tags = var.tags
}

resource "aws_sns_topic_policy" "sensor_data" {
  arn = aws_sns_topic.sensor_data.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowS3Publish"
        Effect    = "Allow"
        Principal = { Service = "s3.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.sensor_data.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = aws_s3_bucket.raw_sensor_data.arn
          }
        }
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# SQS: the EC2 poller's work queue, plus a dead-letter queue so a message
# that fails processing sqs_max_receive_count times stops looping forever
# and becomes visible for alerting instead.
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "sensor_data_dlq" {
  name                      = var.sqs_dlq_name
  message_retention_seconds = 1209600 # 14 days
  tags                      = var.tags
}

resource "aws_sqs_queue" "sensor_data" {
  name                       = var.sqs_queue_name
  visibility_timeout_seconds = var.sqs_visibility_timeout_seconds
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.sensor_data_dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })

  tags = var.tags
}

resource "aws_sqs_queue_policy" "sensor_data" {
  queue_url = aws_sqs_queue.sensor_data.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowSNSSendMessage"
        Effect    = "Allow"
        Principal = { Service = "sns.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.sensor_data.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_sns_topic.sensor_data.arn
          }
        }
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "sensor_data_to_sqs" {
  topic_arn = aws_sns_topic.sensor_data.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.sensor_data.arn
}

# ---------------------------------------------------------------------------
# CloudWatch alarm: alert when messages start landing in the DLQ.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.project_name}-dlq-messages-visible"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "One or more sensor data messages failed processing and landed in the DLQ."
  dimensions = {
    QueueName = aws_sqs_queue.sensor_data_dlq.name
  }
  tags = var.tags
}
