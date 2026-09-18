output "raw_bucket_name" {
  value = aws_s3_bucket.raw_sensor_data.bucket
}

output "sns_topic_arn" {
  value = aws_sns_topic.sensor_data.arn
}

output "sqs_queue_url" {
  value = aws_sqs_queue.sensor_data.id
}

output "sqs_queue_arn" {
  value = aws_sqs_queue.sensor_data.arn
}

output "sqs_dlq_url" {
  value = aws_sqs_queue.sensor_data_dlq.id
}

output "ec2_poller_role_arn" {
  value = aws_iam_role.ec2_poller.arn
}

output "ec2_instance_id" {
  value = var.create_ec2_instance ? aws_instance.ec2_poller[0].id : null
}

output "ec2_ssh_key_name" {
  value = var.ec2_key_name != "" ? var.ec2_key_name : (var.create_ec2_instance && var.create_ec2_ssh_key ? aws_key_pair.ec2_poller[0].key_name : null)
}

output "ec2_private_key_path" {
  value = var.create_ec2_instance && var.create_ec2_ssh_key && var.ec2_key_name == "" ? pathexpand(var.ec2_private_key_path) : null
}
