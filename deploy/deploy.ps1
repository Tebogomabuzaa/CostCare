<#
.SYNOPSIS
  Build and deploy CostCare to AWS (API Gateway + Lambda + Secrets Manager + S3 + EventBridge + CloudWatch).

.EXAMPLE
  .\deploy\deploy.ps1
.EXAMPLE
  .\deploy\deploy.ps1 -Region af-south-1 -Stage prod -AlarmEmail you@example.com
#>
param(
    [string]$Region = "af-south-1",
    [string]$Stage = "prod",
    [string]$AdminEmail = "admin@costcare.local",
    [string]$AlarmEmail = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Continue"

function Invoke-Native([scriptblock]$Block, [string]$What) {
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build"
$pkg = Join-Path $build "lambda"
$stack = "costcare-$Stage"

if (-not $SkipBuild) {
    Write-Host "==> Building Lambda package (Linux x86_64, Python 3.12)" -ForegroundColor Cyan
    if (Test-Path $pkg) { Remove-Item -Recurse -Force $pkg }
    New-Item -ItemType Directory -Force $pkg | Out-Null
    Invoke-Native {
        python -m pip install -r (Join-Path $root "requirements-lambda.txt") --target $pkg `
            --implementation cp --python-version 3.12 `
            --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 `
            --only-binary=:all: --upgrade --quiet --disable-pip-version-check
    } "pip install"
    Copy-Item -Recurse (Join-Path $root "app") (Join-Path $pkg "app")
    Copy-Item (Join-Path $root "lambda_handler.py") $pkg
    Get-ChildItem $pkg -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
}

Write-Host "==> Preparing deployment artifacts bucket" -ForegroundColor Cyan
$account = aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0) { throw "AWS credentials are not configured. Run 'aws configure' first." }
$artifacts = "costcare-artifacts-$account-$Region"
aws s3api head-bucket --bucket $artifacts --region $Region *> $null
if ($LASTEXITCODE -ne 0) {
    Invoke-Native { aws s3api create-bucket --bucket $artifacts --region $Region --create-bucket-configuration LocationConstraint=$Region | Out-Null } "create artifacts bucket"
    Invoke-Native { aws s3api put-public-access-block --bucket $artifacts --region $Region --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" } "block public access"
}

Write-Host "==> Packaging CloudFormation template" -ForegroundColor Cyan
$template = Join-Path $root "deploy\template.yaml"
$packaged = Join-Path $build "packaged.yaml"
Invoke-Native {
    aws cloudformation package --template-file $template --s3-bucket $artifacts --s3-prefix $stack `
        --output-template-file $packaged --region $Region
} "cloudformation package"

Write-Host "==> Deploying stack $stack to $Region" -ForegroundColor Cyan
$params = @("StageName=$Stage", "AdminEmail=$AdminEmail")
if ($AlarmEmail) { $params += "AlarmEmail=$AlarmEmail" }
Invoke-Native {
    aws cloudformation deploy --template-file $packaged --stack-name $stack --region $Region `
        --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND --parameter-overrides $params `
        --tags app=costcare --no-fail-on-empty-changeset
} "cloudformation deploy"

aws cloudformation describe-stacks --stack-name $stack --region $Region --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table

Write-Host ""
Write-Host "Next: add DATABASE_URL (Supabase), OPENAI_API_KEY and GOOGLE_MAPS_API_KEY to the secret costcare/$Stage/app" -ForegroundColor Yellow
Write-Host "      in the AWS Secrets Manager console (Retrieve secret value -> Edit), keeping SECRET_KEY unchanged." -ForegroundColor Yellow
Write-Host "      Admin login: Secrets Manager -> costcare/$Stage/admin -> Retrieve secret value." -ForegroundColor Yellow
