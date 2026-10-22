# Put a public HTTPS CloudFront URL in front of the StipGate instance and lock the instance's
# port 8000 to CloudFront's origin-facing IP ranges only (no direct access).
#
#   pwsh deploy/aws/cloudfront.ps1        # idempotent; prints the https://<id>.cloudfront.net URL
#
# No caching (every request reaches the API). All viewer headers except Host are forwarded, so
# X-API-Key reaches the API. The API key is still required on /v1/*; / and /healthz stay open.
param([string]$Region = "us-east-1")
$ErrorActionPreference = "Stop"
$Comment = "stipgate"
$CachingDisabled = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"            # managed: CachingDisabled
$AllViewerExceptHost = "b689b0a8-53d0-40ab-baf2-68738e2966ac"        # managed: AllViewerExceptHostHeader

$inst = aws ec2 describe-instances --region $Region `
    --filters "Name=tag:project,Values=stipgate" "Name=instance-state-name,Values=running" `
    --query "Reservations[0].Instances[0].[PublicDnsName,SecurityGroups[0].GroupId]" --output text
$OriginDns, $Sg = ("$inst".Trim() -split "\s+")
if (-not $OriginDns -or $OriginDns -eq "None") { throw "no running stipgate instance" }

# --- Security group: CloudFront origin-facing prefix list only --------------------------------------
$Pl = "$(aws ec2 describe-managed-prefix-lists --region $Region `
    --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing `
    --query "PrefixLists[0].PrefixListId" --output text)".Trim()
$perm = Join-Path ([IO.Path]::GetTempPath()) "stipgate-perm.json"
[IO.File]::WriteAllText($perm, (ConvertTo-Json -Compress -Depth 5 @(@{
    IpProtocol = "tcp"; FromPort = 8000; ToPort = 8000
    PrefixListIds = @(@{ PrefixListId = $Pl; Description = "CloudFront origin-facing" })
})))
$null = aws ec2 authorize-security-group-ingress --group-id $Sg --ip-permissions "file://$perm" --region $Region 2>$null
Remove-Item $perm
# Drop any direct /32 rules on 8000 so the instance is reachable only through CloudFront.
$cidrs = "$(aws ec2 describe-security-groups --group-ids $Sg --region $Region `
    --query "SecurityGroups[0].IpPermissions[?FromPort==``8000``].IpRanges[].CidrIp" --output text)".Trim()
foreach ($c in ($cidrs -split "\s+" | Where-Object { $_ })) {
    aws ec2 revoke-security-group-ingress --group-id $Sg --protocol tcp --port 8000 --cidr $c --region $Region | Out-Null
    Write-Host "== revoked direct access from $c"
}
$global:LASTEXITCODE = 0

# --- Distribution ----------------------------------------------------------------------------------
$DistId = "$(aws cloudfront list-distributions --query "DistributionList.Items[?Comment=='$Comment'].Id | [0]" --output text)".Trim()
if (-not $DistId -or $DistId -eq "None") {
    $cfg = @{
        CallerReference = "stipgate-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
        Comment = $Comment
        Enabled = $true
        PriceClass = "PriceClass_100"
        HttpVersion = "http2"
        Origins = @{ Quantity = 1; Items = @(@{
            Id = "stipgate-ec2"; DomainName = $OriginDns
            CustomOriginConfig = @{
                HTTPPort = 8000; HTTPSPort = 443; OriginProtocolPolicy = "http-only"
                OriginSslProtocols = @{ Quantity = 1; Items = @("TLSv1.2") }
                OriginReadTimeout = 60; OriginKeepaliveTimeout = 5
            }
        }) }
        DefaultCacheBehavior = @{
            TargetOriginId = "stipgate-ec2"
            ViewerProtocolPolicy = "redirect-to-https"
            Compress = $true
            CachePolicyId = $CachingDisabled
            OriginRequestPolicyId = $AllViewerExceptHost
            AllowedMethods = @{
                Quantity = 7; Items = @("GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE")
                CachedMethods = @{ Quantity = 2; Items = @("GET", "HEAD") }
            }
        }
    }
    $cfgPath = Join-Path ([IO.Path]::GetTempPath()) "stipgate-cf.json"
    [IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 10 -Compress))
    $DistId = "$(aws cloudfront create-distribution --distribution-config "file://$cfgPath" --query Distribution.Id --output text)".Trim()
    Remove-Item $cfgPath
    aws cloudfront tag-resource --resource "arn:aws:cloudfront::$((aws sts get-caller-identity --query Account --output text).Trim()):distribution/$DistId" --tags "Items=[{Key=project,Value=stipgate}]"
    Write-Host "== created distribution $DistId (origin ${OriginDns}:8000); deploying takes ~5 min"
}
$Domain = "$(aws cloudfront get-distribution --id $DistId --query Distribution.DomainName --output text)".Trim()
Write-Host "Public URL: https://$Domain/"
