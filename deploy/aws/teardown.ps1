# Delete every resource created by deploy.ps1 (EC2 instance, security group, IAM role/profile,
# SSM API key, S3 bucket). Irreversible; the Decider model cache on the instance volume is lost.
param([string]$Region = "us-east-1")
$ErrorActionPreference = "Continue"
$Account = (aws sts get-caller-identity --query Account --output text).Trim()
$Bucket = "stipgate-deploy-$Account"
$Name = "stipgate"

$ids = "$(aws ec2 describe-instances --region $Region `
    --filters "Name=tag:project,Values=stipgate" "Name=instance-state-name,Values=pending,running,stopping,stopped" `
    --query "Reservations[].Instances[].InstanceId" --output text)".Trim()
if ($ids) {
    $list = $ids -split "\s+"
    aws ec2 terminate-instances --instance-ids @list --region $Region | Out-Null
    Write-Host "== terminating $ids"
    aws ec2 wait instance-terminated --instance-ids @list --region $Region
}

$Sg = "$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$Name-api" --query "SecurityGroups[0].GroupId" --output text --region $Region)".Trim()
if ($Sg -and $Sg -ne "None") { aws ec2 delete-security-group --group-id $Sg --region $Region; Write-Host "== deleted $Sg" }

aws iam remove-role-from-instance-profile --instance-profile-name "$Name-ec2" --role-name "$Name-ec2" 2>$null
aws iam delete-instance-profile --instance-profile-name "$Name-ec2" 2>$null
aws iam detach-role-policy --role-name "$Name-ec2" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore 2>$null
aws iam delete-role-policy --role-name "$Name-ec2" --policy-name "$Name-runtime" 2>$null
aws iam delete-role --role-name "$Name-ec2" 2>$null
Write-Host "== deleted IAM role/profile $Name-ec2"

aws ssm delete-parameter --name /stipgate/api-key --region $Region 2>$null
aws s3 rb "s3://$Bucket" --force --region $Region 2>$null
Write-Host "== deleted /stipgate/api-key and s3://$Bucket"
