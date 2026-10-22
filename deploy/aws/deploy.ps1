# Deploy StipGate (API + Strands Decider 2B CPU + Amazon Nova Lite on Bedrock) to one EC2 instance.
#
#   pwsh deploy/aws/deploy.ps1                 # create everything (idempotent where possible)
#   pwsh deploy/aws/teardown.ps1               # delete everything this script created
#
# Resources (all tagged project=stipgate, region us-east-1):
#   S3 bucket stipgate-deploy-<account>        source tarball
#   SSM SecureString /stipgate/api-key         X-API-Key for /v1/*
#   IAM role + instance profile stipgate-ec2   SSM core, read the tarball/key, InvokeModel on Nova Lite only
#   Security group stipgate-api                TCP 8000 from the caller's public IP /32 only
#   EC2 m7i-flex.large (AL2023, 30 GB gp3)     IMDSv2 required, hop limit 2 for containers
#   Decider 2B runs int8 on CPU (community AgentCore runtime) to fit 8 GB; see user-data.sh
param(
    [string]$Region = "us-east-1",
    [string]$InstanceType = "m7i-flex.large",   # free-plan eligible: 2 vCPU / 8 GB
    [string]$AllowCidr = ""
)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Account = (aws sts get-caller-identity --query Account --output text).Trim()
$Bucket = "stipgate-deploy-$Account"
$Name = "stipgate"
$Tags = "Key=project,Value=stipgate"
if (-not $AllowCidr) { $AllowCidr = "$((Invoke-RestMethod https://checkip.amazonaws.com).Trim())/32" }
$Tmp = Join-Path ([IO.Path]::GetTempPath()) "stipgate-deploy"
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

function Write-Json([string]$Path, $Obj) {
    [IO.File]::WriteAllText($Path, ($Obj | ConvertTo-Json -Depth 10 -Compress))
}

Write-Host "== account $Account, region $Region, ingress $AllowCidr"

# --- S3 bucket + source tarball ---------------------------------------------------------------
$null = aws s3api head-bucket --bucket $Bucket 2>$null
if ($LASTEXITCODE -ne 0) {
    if ($Region -eq "us-east-1") { aws s3api create-bucket --bucket $Bucket --region $Region | Out-Null }
    else { aws s3api create-bucket --bucket $Bucket --region $Region --create-bucket-configuration LocationConstraint=$Region | Out-Null }
    aws s3api put-public-access-block --bucket $Bucket --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
    aws s3api put-bucket-tagging --bucket $Bucket --tagging "TagSet=[{Key=project,Value=stipgate}]"
}
$Tarball = Join-Path $Tmp "stipgate-src.tgz"
Remove-Item $Tarball -ErrorAction SilentlyContinue
tar -czf $Tarball -C $Root `
    --exclude=.venv --exclude=.mypy_cache --exclude=.pytest_cache --exclude=__pycache__ `
    --exclude=.agents --exclude=*.egg-info --exclude=deploy --exclude=.git `
    Dockerfile .dockerignore requirements.txt pyproject.toml README.md stipgate docker
aws s3 cp $Tarball "s3://$Bucket/stipgate-src.tgz" --region $Region | Out-Null
Write-Host "== uploaded source to s3://$Bucket/stipgate-src.tgz"

# --- API key ------------------------------------------------------------------------------------
$null = aws ssm get-parameter --name /stipgate/api-key --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $key = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    aws ssm put-parameter --name /stipgate/api-key --type SecureString --value $key --tags $Tags --region $Region | Out-Null
    Remove-Variable key
    Write-Host "== created SSM SecureString /stipgate/api-key"
}

# --- IAM role + instance profile ------------------------------------------------------------------
$null = aws iam get-role --role-name "$Name-ec2" 2>$null
if ($LASTEXITCODE -ne 0) {
    $trust = Join-Path $Tmp "trust.json"
    Write-Json $trust @{
        Version = "2012-10-17"
        Statement = @(@{ Effect = "Allow"; Principal = @{ Service = "ec2.amazonaws.com" }; Action = "sts:AssumeRole" })
    }
    aws iam create-role --role-name "$Name-ec2" --assume-role-policy-document "file://$trust" --tags $Tags | Out-Null
    aws iam attach-role-policy --role-name "$Name-ec2" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
}
$inline = Join-Path $Tmp "inline.json"
Write-Json $inline @{
    Version = "2012-10-17"
    Statement = @(
        @{ Sid = "InvokeNovaLiteOnly"; Effect = "Allow"
           Action = @("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream")
           Resource = "arn:aws:bedrock:${Region}::foundation-model/amazon.nova-lite-v1:0" },
        @{ Sid = "ReadSource"; Effect = "Allow"; Action = "s3:GetObject"; Resource = "arn:aws:s3:::$Bucket/stipgate-src.tgz" },
        @{ Sid = "ReadApiKey"; Effect = "Allow"; Action = "ssm:GetParameter"
           Resource = "arn:aws:ssm:${Region}:${Account}:parameter/stipgate/api-key" }
    )
}
aws iam put-role-policy --role-name "$Name-ec2" --policy-name "$Name-runtime" --policy-document "file://$inline"
$null = aws iam get-instance-profile --instance-profile-name "$Name-ec2" 2>$null
if ($LASTEXITCODE -ne 0) {
    aws iam create-instance-profile --instance-profile-name "$Name-ec2" --tags $Tags | Out-Null
    aws iam add-role-to-instance-profile --instance-profile-name "$Name-ec2" --role-name "$Name-ec2"
    Write-Host "== waiting for IAM instance profile to propagate"
    Start-Sleep -Seconds 15
}

# --- Security group ------------------------------------------------------------------------------
$Vpc = (aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query "Vpcs[0].VpcId" --output text --region $Region).Trim()
$Sg = (aws ec2 describe-security-groups --filters "Name=group-name,Values=$Name-api" "Name=vpc-id,Values=$Vpc" --query "SecurityGroups[0].GroupId" --output text --region $Region).Trim()
if ($Sg -eq "None" -or -not $Sg) {
    $Sg = (aws ec2 create-security-group --group-name "$Name-api" --description "StipGate API 8000 from one IP" --vpc-id $Vpc `
        --tag-specifications "ResourceType=security-group,Tags=[{$Tags}]" --query GroupId --output text --region $Region).Trim()
}
$null = aws ec2 authorize-security-group-ingress --group-id $Sg --protocol tcp --port 8000 --cidr $AllowCidr --region $Region 2>$null
$global:LASTEXITCODE = 0

# --- EC2 instance --------------------------------------------------------------------------------
$existing = "$(aws ec2 describe-instances --region $Region `
    --filters "Name=tag:project,Values=stipgate" "Name=instance-state-name,Values=pending,running" `
    --query "Reservations[].Instances[].InstanceId" --output text)".Trim()
if ($existing) {
    Write-Host "== instance already running: $existing (run teardown.ps1 first to redeploy from scratch)"
    $InstanceId = $existing
} else {
    $Ami = (aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 `
        --query Parameter.Value --output text --region $Region).Trim()
    $ud = (Get-Content -Raw (Join-Path $PSScriptRoot "user-data.sh")).Replace("`r`n", "`n").Replace("__BUCKET__", $Bucket).Replace("__REGION__", $Region)
    $udPath = Join-Path $Tmp "user-data.sh"
    [IO.File]::WriteAllText($udPath, $ud)
    $bdm = Join-Path $Tmp "bdm.json"
    [IO.File]::WriteAllText($bdm, '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":30,"VolumeType":"gp3","DeleteOnTermination":true}}]')
    $InstanceId = (aws ec2 run-instances --region $Region --image-id $Ami --instance-type $InstanceType `
        --iam-instance-profile "Name=$Name-ec2" --security-group-ids $Sg `
        --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled" `
        --block-device-mappings "file://$bdm" `
        --user-data "file://$udPath" `
        --tag-specifications "ResourceType=instance,Tags=[{$Tags},{Key=Name,Value=stipgate}]" "ResourceType=volume,Tags=[{$Tags}]" `
        --query "Instances[0].InstanceId" --output text).Trim()
    Write-Host "== launched $InstanceId ($InstanceType, $Ami)"
    aws ec2 wait instance-running --instance-ids $InstanceId --region $Region
}
$Ip = (aws ec2 describe-instances --instance-ids $InstanceId --query "Reservations[0].Instances[0].PublicIpAddress" --output text --region $Region).Trim()
Remove-Item -Recurse -Force $Tmp

Write-Host ""
Write-Host "Instance : $InstanceId"
Write-Host "UI       : http://${Ip}:8000/   (open only to $AllowCidr)"
Write-Host "API key  : aws ssm get-parameter --name /stipgate/api-key --with-decryption --query Parameter.Value --output text"
Write-Host "Bootstrap takes ~10-20 min (image builds + 4.5 GB model download). Check: http://${Ip}:8000/healthz"
