output "controller_url" {
  description = "Open this URL to start both servers and watch their status"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "web_domain_validation_records" {
  description = "DNS record(s) to add at your registrar to validate the web_domain ACM cert (phase 1 of the two-phase apply - see README). Empty unless web_domain is set."
  value = var.web_domain != "" ? [
    for o in aws_acm_certificate.web[0].domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ] : []
}

output "web_domain_cname_target" {
  description = "CNAME target for web_domain itself, once the cert is issued (phase 2 - see README). Empty unless web_domain is set."
  value       = var.web_domain != "" ? aws_apigatewayv2_domain_name.web[0].domain_name_configuration[0].target_domain_name : null
}

output "proxy_eip" {
  description = "Stable Elastic IP for the proxy instance - point app_domain's A record here. Empty unless app_domain is set."
  value       = var.app_domain != "" ? aws_eip.proxy[0].public_ip : null
}

output "ai_instance_id" {
  description = "EC2 instance ID of the isolated AI (Claude Code) instance"
  value       = aws_instance.ai.id
}

output "proxy_instance_id" {
  description = "EC2 instance ID of the internet-facing proxy instance"
  value       = aws_instance.proxy.id
}

output "proxy_public_ip" {
  description = "Public IP of the proxy instance as of the last apply/refresh. Stable across stop/start if app_domain is set (see proxy_eip) - otherwise changes on every stop/start, so prefer the controller URL's /status endpoint for the current value"
  value       = var.app_domain != "" ? aws_eip.proxy[0].public_ip : aws_instance.proxy.public_ip
}

output "ai_private_ip" {
  description = "Private IP of the AI instance (stable across stop/start) - the ssh -J target"
  value       = aws_instance.ai.private_ip
}

output "ai_subnet_id" {
  description = "Private subnet ID for the AI instance (no Internet Gateway route)"
  value       = aws_subnet.ai.id
}

output "proxy_subnet_id" {
  description = "Public subnet ID for the proxy instance"
  value       = aws_subnet.proxy.id
}

output "ai_security_group_id" {
  description = "Security group ID for the AI instance"
  value       = aws_security_group.ai.id
}

output "proxy_security_group_id" {
  description = "Security group ID for the proxy instance"
  value       = aws_security_group.proxy.id
}

output "deploy_instance_id" {
  description = "EC2 instance ID of the deploy/web instance"
  value       = aws_instance.deploy.id
}

output "deploy_private_ip" {
  description = "Private IP of the deploy instance (stable across stop/start) - the ssh -J target"
  value       = aws_instance.deploy.private_ip
}

output "deploy_subnet_id" {
  description = "Private subnet ID for the deploy instance (no Internet Gateway route)"
  value       = aws_subnet.deploy.id
}

output "deploy_security_group_id" {
  description = "Security group ID for the deploy instance"
  value       = aws_security_group.deploy.id
}
