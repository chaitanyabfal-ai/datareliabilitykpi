data "aws_ami" "al2023" {
  count       = var.create_ec2_instance && var.ec2_ami_id == "" ? 1 : 0
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

resource "tls_private_key" "ec2_poller" {
  count     = var.create_ec2_instance && var.create_ec2_ssh_key && var.ec2_key_name == "" ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "ec2_poller" {
  count      = var.create_ec2_instance && var.create_ec2_ssh_key && var.ec2_key_name == "" ? 1 : 0
  key_name   = "${var.project_name}-ec2-key"
  public_key = tls_private_key.ec2_poller[0].public_key_openssh
  tags       = var.tags
}

resource "local_sensitive_file" "ec2_private_key" {
  count           = var.create_ec2_instance && var.create_ec2_ssh_key && var.ec2_key_name == "" ? 1 : 0
  filename        = pathexpand(var.ec2_private_key_path)
  content         = tls_private_key.ec2_poller[0].private_key_openssh
  file_permission = "0600"
}

resource "aws_security_group" "ec2_poller" {
  count       = var.create_ec2_instance ? 1 : 0
  name        = "${var.project_name}-ec2-poller-sg"
  description = "No inbound rules -- access via SSM Session Manager only."
  vpc_id      = null # uses default VPC unless overridden by subnet's VPC

  egress {
    description = "Allow all outbound (S3, SQS, SNS, SSM endpoints)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags
}

resource "aws_security_group_rule" "ec2_ssh" {
  count             = var.create_ec2_instance && var.ec2_ssh_allowed_cidr != "" ? 1 : 0
  type              = "ingress"
  security_group_id = aws_security_group.ec2_poller[0].id
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = [var.ec2_ssh_allowed_cidr]
  description       = "SSH access from the configured administrator IP"
}

resource "aws_instance" "ec2_poller" {
  count                  = var.create_ec2_instance ? 1 : 0
  ami                    = var.ec2_ami_id != "" ? var.ec2_ami_id : data.aws_ami.al2023[0].id
  instance_type          = var.ec2_instance_type
  subnet_id              = var.ec2_subnet_id != "" ? var.ec2_subnet_id : null
  key_name               = var.ec2_key_name != "" ? var.ec2_key_name : (var.create_ec2_ssh_key ? aws_key_pair.ec2_poller[0].key_name : null)
  iam_instance_profile   = aws_iam_instance_profile.ec2_poller.name
  vpc_security_group_ids = [aws_security_group.ec2_poller[0].id]

  root_block_device {
    volume_size           = var.ec2_root_volume_size
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  lifecycle {
    # Existing instances may have been resized outside Terraform to avoid replacement.
    ignore_changes = [root_block_device]
  }

  user_data = <<-EOF
    #!/bin/bash
    dnf install -y python3-pip git
    pip3 install boto3 streamlit pandas python-dotenv
  EOF

  tags = merge(var.tags, { Name = "${var.project_name}-ec2-poller" })
}
