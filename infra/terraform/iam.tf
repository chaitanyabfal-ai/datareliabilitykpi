# Least-privilege role for the EC2 poller: it may only read the raw bucket,
# inspect the configured S3 notification and SNS topic, and receive/delete/
# inspect the one SQS queue it consumes. It has no write access to AWS data.
resource "aws_iam_role" "ec2_poller" {
  name = "${var.project_name}-ec2-poller-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "ec2_poller" {
  name = "${var.project_name}-ec2-poller-policy"
  role = aws_iam_role.ec2_poller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadRawBucket"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket", "s3:GetBucketNotification"]
        Resource = [
          aws_s3_bucket.raw_sensor_data.arn,
          "${aws_s3_bucket.raw_sensor_data.arn}/*",
        ]
      },
      {
        Sid      = "InspectNotificationTopic"
        Effect   = "Allow"
        Action   = ["sns:GetTopicAttributes"]
        Resource = aws_sns_topic.sensor_data.arn
      },
      {
        Sid    = "ConsumeQueue"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
        ]
        Resource = aws_sqs_queue.sensor_data.arn
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/sensor-kpi/*"
      }
    ]
  })
}

# Allows SSM Session Manager access (for port-forwarding the dashboard)
# without opening any inbound security group rules.
resource "aws_iam_role_policy_attachment" "ec2_poller_ssm" {
  role       = aws_iam_role.ec2_poller.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2_poller" {
  name = "${var.project_name}-ec2-poller-profile"
  role = aws_iam_role.ec2_poller.name
}
