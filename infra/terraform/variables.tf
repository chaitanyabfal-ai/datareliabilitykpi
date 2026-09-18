variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Short name used as a prefix for all resources."
  type        = string
  default     = "sensor-kpi"
}

variable "raw_bucket_name" {
  description = "Name of the S3 bucket that receives raw sensor data from garage_sync.py."
  type        = string
  default     = "raw-sensor-data-bucket"
}

variable "raw_prefix" {
  description = "Key prefix within the raw bucket that triggers the S3 event notification."
  type        = string
  default     = "raw-sensor-data/"
}

variable "sns_topic_name" {
  type    = string
  default = "sensor-data-topic"
}

variable "sqs_queue_name" {
  type    = string
  default = "sensor-data-queue"
}

variable "sqs_dlq_name" {
  type    = string
  default = "sensor-data-queue-dlq"
}

variable "sqs_max_receive_count" {
  description = "Number of failed deliveries before a message is moved to the DLQ."
  type        = number
  default     = 5
}

variable "sqs_visibility_timeout_seconds" {
  type    = number
  default = 60
}

variable "create_ec2_instance" {
  description = "Whether to create the EC2 poller instance itself (set false if you already have one and just want the IAM role/policies)."
  type        = bool
  default     = false
}

variable "ec2_instance_type" {
  type    = string
  default = "t3.small"
}

variable "ec2_root_volume_size" {
  description = "Root EBS volume size in GiB for newly created EC2 poller instances."
  type        = number
  default     = 20
}

variable "ec2_ami_id" {
  description = "AMI for the EC2 poller instance. Leave blank to look up the latest Amazon Linux 2023 AMI."
  type        = string
  default     = ""
}

variable "ec2_key_name" {
  description = "Existing EC2 key pair name. Leave blank to generate one with Terraform."
  type        = string
  default     = ""
}

variable "create_ec2_ssh_key" {
  description = "Generate and register an EC2 SSH key pair when ec2_key_name is empty."
  type        = bool
  default     = true
}

variable "ec2_private_key_path" {
  description = "Local path where Terraform writes the generated EC2 private key."
  type        = string
  default     = "~/.ssh/sensor-kpi-ec2.pem"
}

variable "ec2_ssh_allowed_cidr" {
  description = "IPv4 CIDR allowed to SSH to the EC2 poller. Leave blank to disable inbound SSH."
  type        = string
  default     = ""
}

variable "ec2_subnet_id" {
  type    = string
  default = ""
}

variable "tags" {
  type = map(string)
  default = {
    Project = "sensor-kpi-pipeline"
  }
}
