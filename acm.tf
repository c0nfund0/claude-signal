# Optional "single-link start" custom domain (var.web_domain) - see README's
# "Custom domain" section. Skipped entirely when web_domain is "" (the default).
#
# DNS is managed outside this account (see README), so unlike a typical Route53
# setup this can't self-validate in one `terraform apply`: the ACM certificate is
# created, then you add the DNS validation record(s) from `terraform output
# web_domain_validation_records` at your registrar, then re-run `terraform apply`
# so aws_acm_certificate_validation can confirm issuance and the domain mapping can
# be created. See the README for the exact two-phase sequence.

resource "aws_acm_certificate" "web" {
  count             = var.web_domain != "" ? 1 : 0
  domain_name       = var.web_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "web" {
  count           = var.web_domain != "" ? 1 : 0
  certificate_arn = aws_acm_certificate.web[0].arn

  # No route53_record_fqdns - validation records live in an external DNS provider
  # (see file header). Terraform just polls ACM until it reports ISSUED, which only
  # happens once you've added the record(s) yourself.
  timeouts {
    create = "45m"
  }
}

resource "aws_apigatewayv2_domain_name" "web" {
  count       = var.web_domain != "" ? 1 : 0
  domain_name = var.web_domain

  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.web[0].certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "web" {
  count       = var.web_domain != "" ? 1 : 0
  api_id      = aws_apigatewayv2_api.controller.id
  domain_name = aws_apigatewayv2_domain_name.web[0].id
  stage       = aws_apigatewayv2_stage.default.id
}
