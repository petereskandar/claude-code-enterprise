#!/bin/bash
# Deploy script for Claude Code Admin Dashboard
# Steps: CloudFormation stack → Lambda zip → SPA upload → CloudFront invalidation
# Usage: bash deploy-admin-dashboard.sh [--profile <aws-profile>] [--region <region>]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Defaults (override via argomenti o env var) ────────────────────────────────
STACK_NAME="claude-code-quota-admin"
REGION="${AWS_REGION:-eu-central-1}"
PROFILE="${AWS_PROFILE:-poste-claudeaws}"
ARTIFACTS_BUCKET="claude-code-s3-cfnartifactsbucket-0enb7bvsxlut"
LAMBDA_S3_KEY="claude-code/admin/dashboard/api.zip"
AZURE_CLIENT_ID="97c4d1b5-7c46-4bff-b6cf-d93898635821"
AZURE_TENANT_ID="761de76f-3d5c-4174-917c-5ad4d06360cb"

# Parsing argomenti opzionali
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --region)  REGION="$2";  shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

TEMPLATE="$REPO_ROOT/deployment/infrastructure/quota-admin-dashboard.yaml"
LAMBDA_SRC="$REPO_ROOT/deployment/infrastructure/lambda-functions/quota_admin/index.py"
LAMBDA_EXPORT_SRC="$REPO_ROOT/deployment/infrastructure/lambda-functions/quota_admin_daily_export/index.py"
LAMBDA_EXPORT_S3_KEY="claude-code/admin/daily-export/api.zip"
SPA_DIR="$REPO_ROOT/deployment/infrastructure/lambda-functions/quota_admin/spa"

echo "╭─────────────────────────────────────────────────╮"
echo "│  Claude Code Admin Dashboard — Deploy            │"
echo "╰─────────────────────────────────────────────────╯"
echo "  Profile : $PROFILE"
echo "  Region  : $REGION"
echo ""

# ============================================================
# Step 1: Deploy CloudFormation stack
# ============================================================
echo "→ [1/5] Deploy CloudFormation stack..."

aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --profile "$PROFILE" \
  --no-fail-on-empty-changeset

echo "✓ Stack aggiornato"
echo ""

# ============================================================
# Step 2: Package e upload Lambda
# ============================================================
echo "→ [2/5] Package e upload Lambda (admin API)..."

TMP_ZIP="/tmp/admin_api_$$.zip"

# zip nativo o fallback python (Windows/ambienti senza zip)
if command -v zip &> /dev/null; then
  TMP_DIR=$(mktemp -d)
  cp "$LAMBDA_SRC" "$TMP_DIR/index.py"
  (cd "$TMP_DIR" && zip -q "$TMP_ZIP" index.py)
  rm -rf "$TMP_DIR"
else
  python3 -c "
import zipfile
with zipfile.ZipFile('$TMP_ZIP', 'w', zipfile.ZIP_DEFLATED) as z:
    z.write('$LAMBDA_SRC', 'index.py')
"
fi

aws s3 cp "$TMP_ZIP" "s3://$ARTIFACTS_BUCKET/$LAMBDA_S3_KEY" \
  --sse AES256 --region "$REGION" --profile "$PROFILE"
rm -f "$TMP_ZIP"

aws lambda update-function-code \
  --function-name ClaudeCode-QuotaAdmin \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --s3-key "$LAMBDA_S3_KEY" \
  --region "$REGION" --profile "$PROFILE" \
  --output text --query 'FunctionArn' > /dev/null

echo "✓ Lambda admin aggiornata"
echo ""

# ============================================================
# Step 3: Package e upload Lambda daily export
# ============================================================
echo "→ [3/5] Package e upload Lambda (daily export)..."

TMP_ZIP_EXPORT="/tmp/admin_export_$$.zip"

if command -v zip &> /dev/null; then
  TMP_DIR_EXPORT=$(mktemp -d)
  cp "$LAMBDA_EXPORT_SRC" "$TMP_DIR_EXPORT/index.py"
  (cd "$TMP_DIR_EXPORT" && zip -q "$TMP_ZIP_EXPORT" index.py)
  rm -rf "$TMP_DIR_EXPORT"
else
  python3 -c "
import zipfile
with zipfile.ZipFile('$TMP_ZIP_EXPORT', 'w', zipfile.ZIP_DEFLATED) as z:
    z.write('$LAMBDA_EXPORT_SRC', 'index.py')
"
fi

aws s3 cp "$TMP_ZIP_EXPORT" "s3://$ARTIFACTS_BUCKET/$LAMBDA_EXPORT_S3_KEY" \
  --sse AES256 --region "$REGION" --profile "$PROFILE"
rm -f "$TMP_ZIP_EXPORT"

aws lambda update-function-code \
  --function-name ClaudeCode-DailyExport \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --s3-key "$LAMBDA_EXPORT_S3_KEY" \
  --region "$REGION" --profile "$PROFILE" \
  --output text --query 'FunctionArn' > /dev/null

echo "✓ Lambda daily export aggiornata"
echo ""

# ============================================================
# Step 4: Upload SPA
# ============================================================
echo "→ [4/5] Upload SPA..."

API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" --profile "$PROFILE" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text)

SPA_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" --profile "$PROFILE" \
  --query "Stacks[0].Outputs[?OutputKey=='SPABucketName'].OutputValue" --output text)

CF_DISTRIBUTION_ID=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" --profile "$PROFILE" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" --output text)

DASHBOARD_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" --profile "$PROFILE" \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue" --output text)

if [ -z "$API_ENDPOINT" ] || [ -z "$SPA_BUCKET" ]; then
  echo "✗ Impossibile leggere gli output dello stack. Verificare di avere accesso all'account corretto."
  exit 1
fi

echo "  API Endpoint : $API_ENDPOINT"
echo "  SPA Bucket   : $SPA_BUCKET"
echo "  Dashboard URL: $DASHBOARD_URL"
echo ""

# Sostituzione placeholder in app.js
TMP_APP="/tmp/app_admin_$$.js"
python3 -c "
content = open('$SPA_DIR/app.js', encoding='utf-8').read()
content = content.replace('__API_ENDPOINT__', '$API_ENDPOINT')
content = content.replace('__AZURE_CLIENT_ID__', '$AZURE_CLIENT_ID')
content = content.replace('__AZURE_TENANT_ID__', '$AZURE_TENANT_ID')
open('$TMP_APP', 'w', encoding='utf-8').write(content)
"

aws s3 cp "$TMP_APP" "s3://$SPA_BUCKET/app.js" \
  --sse AES256 --content-type "application/javascript" --region "$REGION" --profile "$PROFILE"
rm -f "$TMP_APP"

aws s3 cp "$SPA_DIR/index.html" "s3://$SPA_BUCKET/index.html" \
  --sse AES256 --content-type "text/html" --region "$REGION" --profile "$PROFILE"

aws s3 cp "$SPA_DIR/style.css" "s3://$SPA_BUCKET/style.css" \
  --sse AES256 --content-type "text/css" --region "$REGION" --profile "$PROFILE"

echo "✓ SPA caricata"
echo ""

# ============================================================
# Step 4: CloudFront invalidation
# ============================================================
echo "→ [5/5] Invalidazione CloudFront..."

aws cloudfront create-invalidation \
  --distribution-id "$CF_DISTRIBUTION_ID" \
  --paths "/*" \
  --region "$REGION" --profile "$PROFILE" \
  --output text --query 'Invalidation.Id' > /dev/null

echo "✓ Invalidazione avviata"
echo ""

echo "╭─────────────────────────────────────────────────╮"
echo "│  ✓ Deploy completato                             │"
echo "╰─────────────────────────────────────────────────╯"
echo ""
echo "  Dashboard URL : $DASHBOARD_URL"
echo "  API Endpoint  : $API_ENDPOINT"
echo ""
