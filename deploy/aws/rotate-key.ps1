# Rotate the StipGate API key: new SSM SecureString value, then recreate the stipgate-api container
# on the instance (it reads the key from SSM itself, so the key never appears in SSM command history).
# Also scrubs any earlier key from the bootstrap / cloud-init logs.
param([string]$Region = "us-east-1")
$ErrorActionPreference = "Stop"

$InstanceId = "$(aws ec2 describe-instances --region $Region `
    --filters "Name=tag:project,Values=stipgate" "Name=instance-state-name,Values=running" `
    --query "Reservations[0].Instances[0].InstanceId" --output text)".Trim()
if (-not $InstanceId -or $InstanceId -eq "None") { throw "no running stipgate instance" }

$old = "$(aws ssm get-parameter --name /stipgate/api-key --with-decryption --query Parameter.Value --output text --region $Region)".Trim()
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$new = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
aws ssm put-parameter --name /stipgate/api-key --type SecureString --value $new --overwrite --region $Region | Out-Null
Remove-Variable new

# The old key is only used as a sed pattern to scrub logs; it is already invalid after this run.
$script = @(
    'set -euo pipefail'
    "OLD='$old'"
    'for f in /var/log/stipgate-bootstrap.log /var/log/cloud-init-output.log; do [ -f "$f" ] && sed -i "s/$OLD/[REDACTED]/g" "$f"; done'
    "KEY=`$(aws ssm get-parameter --name /stipgate/api-key --with-decryption --query Parameter.Value --output text --region $Region)"
    'ENVS=$(docker inspect stipgate-api --format "{{range .Config.Env}}{{println .}}{{end}}" | grep -E "^(STIPGATE_DECIDER_|AWS_REGION|AWS_DEFAULT_REGION)" | sed "s/^/-e /" | tr "\n" " ")'
    'docker rm -f stipgate-api >/dev/null'
    'docker run -d --name stipgate-api --restart unless-stopped --network stipgate -p 8000:8000 $ENVS -e STIPGATE_API_KEY="$KEY" stipgate-api:local >/dev/null'
    'unset KEY OLD'
    'echo ROTATED'
)
Remove-Variable old
$tmp = Join-Path ([IO.Path]::GetTempPath()) "stipgate-rotate.json"
[IO.File]::WriteAllText($tmp, (@{ commands = $script } | ConvertTo-Json -Compress))
$cmd = "$(aws ssm send-command --instance-ids $InstanceId --document-name AWS-RunShellScript `
    --parameters "file://$tmp" --query Command.CommandId --output text --region $Region)".Trim()
Remove-Item $tmp
aws ssm wait command-executed --command-id $cmd --instance-id $InstanceId --region $Region 2>$null
aws ssm get-command-invocation --command-id $cmd --instance-id $InstanceId --region $Region `
    --query "[Status,StandardOutputContent,StandardErrorContent]" --output text
