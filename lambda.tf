data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.module}/lambda/app.py"
  output_path = "${path.module}/lambda/app.zip"
}

resource "aws_iam_role" "lambda_controller" {
  name = "${var.name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_controller.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_ec2" {
  name = "${var.name}-lambda-ec2"
  role = aws_iam_role.lambda_controller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # ec2:DescribeInstances has no resource-level permissions in AWS IAM - "*" is
        # required here regardless of scoping intent.
        Sid      = "DescribeAny"
        Effect   = "Allow"
        Action   = "ec2:DescribeInstances"
        Resource = "*"
      },
      {
        # Covers both the ai and proxy instances with one statement via the shared
        # Project tag, rather than hardcoding two instance ARNs.
        Sid      = "StartStopOnlyProjectInstances"
        Effect   = "Allow"
        Action   = ["ec2:StartInstances", "ec2:StopInstances"]
        Resource = "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/Project" = var.name
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name}-controller"
  retention_in_days = 14
}

resource "aws_lambda_function" "controller" {
  function_name = "${var.name}-controller"
  role          = aws_iam_role.lambda_controller.arn
  handler       = "app.handler"
  runtime       = "python3.12"
  timeout       = 15
  memory_size   = 128

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      INSTANCE_ID_AI     = aws_instance.ai.id
      INSTANCE_ID_PROXY  = aws_instance.proxy.id
      INSTANCE_ID_DEPLOY = aws_instance.deploy.id
      STOP_SECRET        = var.stop_secret
      # Empty when var.web_domain is unset - _handle() treats that as "feature off"
      # and falls back to its original behavior for every request. See acm.tf.
      WEB_DOMAIN      = var.web_domain
      APP_DOMAIN      = var.app_domain
      WEB_OPEN_SECRET = var.web_open_secret
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}
