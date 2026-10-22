# Ship local code changes to the running StipGate instance without touching the Decider.
#
#   pwsh deploy/aws/update-api.ps1
#
# Uploads a fresh source tarball to S3, then (via SSM Run Command) rebuilds only the stipgate-api image
# and recreates that container with its current Decider/AWS settings. The API key is read from SSM on the
# instance, so it never appears in SSM command history. Expect ~1-3 min of API downtime.
param([string]$Region = "us-east-1")
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Account = (aws sts get-caller-identity --query Account --output text).Trim()
$Bucket = "stipgate-deploy-$Account"

$InstanceId = "$(aws ec2 describe-instances --region $Region `
    --filters "Name=tag:project,Values=stipgate" "Name=instance-state-name,Values=running" `
    --query "Reservations[0].Instances[0].InstanceId" --output text)".Trim()
if (-not $InstanceId -or $InstanceId -eq "None") { throw "no running stipgate instance (use deploy.ps1)" }
Write-Host "== instance $InstanceId"

$Tmp = Join-Path ([IO.Path]::GetTempPath()) "stipgate-update"
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
$Tarball = Join-Path $Tmp "stipgate-src.tgz"
Remove-Item $Tarball -ErrorAction SilentlyContinue
tar -czf $Tarball -C $Root `
    --exclude=.venv --exclude=.mypy_cache --exclude=.pytest_cache --exclude=__pycache__ `
    --exclude=.agents --exclude=*.egg-info --exclude=deploy --exclude=.git `
    Dockerfile .dockerignore requirements.txt pyproject.toml README.md stipgate docker
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
aws s3 cp $Tarball "s3://$Bucket/stipgate-src.tgz" --region $Region | Out-Null
if ($LASTEXITCODE -ne 0) { throw "S3 upload failed" }
Write-Host "== uploaded source to s3://$Bucket/stipgate-src.tgz"

$script = @(
    'set -euo pipefail'
    'cd /opt/stipgate'
    "aws s3 cp s3://$Bucket/stipgate-src.tgz src.tgz --region $Region --only-show-errors"
    'rm -rf stipgate'   # drop the old package so deleted modules do not linger
    'tar -xzf src.tgz && rm -f src.tgz'
    'docker build -q -t stipgate-api:local . >/dev/null'
    'echo BUILT'
    "KEY=`$(aws ssm get-parameter --name /stipgate/api-key --with-decryption --query Parameter.Value --output text --region $Region)"
    'ENVS=$(docker inspect stipgate-api --format "{{range .Config.Env}}{{println .}}{{end}}" | grep -E "^(STIPGATE_DECIDER_|AWS_REGION|AWS_DEFAULT_REGION)" | sed "s/^/-e /" | tr "\n" " ")'
    'docker rm -f stipgate-api >/dev/null'
    'docker run -d --name stipgate-api --restart unless-stopped --network stipgate -p 8000:8000 $ENVS -e STIPGATE_API_KEY="$KEY" stipgate-api:local >/dev/null'
    'unset KEY'
    'for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 2; done'
    'curl -fsS http://127.0.0.1:8000/healthz; echo'
    'docker image prune -f >/dev/null'
    'echo UPDATED'
)
$paramFile = Join-Path $Tmp "params.json"
[IO.File]::WriteAllText($paramFile, (@{ commands = $script; executionTimeout = @("1800") } | ConvertTo-Json -Compress))
$cmd = "$(aws ssm send-command --instance-ids $InstanceId --document-name AWS-RunShellScript `
    --comment "stipgate update-api" --parameters "file://$paramFile" --query Command.CommandId --output text --region $Region)".Trim()
Remove-Item -Recurse -Force $Tmp
Write-Host "== SSM command $cmd (rebuilding stipgate-api)"

do {
    Start-Sleep -Seconds 10
    $status = "$(aws ssm get-command-invocation --command-id $cmd --instance-id $InstanceId --region $Region --query Status --output text 2>$null)".Trim()
    Write-Host "   $status"
} while ($status -in @("Pending", "InProgress", "Delayed", ""))

aws ssm get-command-invocation --command-id $cmd --instance-id $InstanceId --region $Region `
    --query "[StandardOutputContent,StandardErrorContent]" --output text
if ($status -ne "Success") { throw "update failed: $status" }
