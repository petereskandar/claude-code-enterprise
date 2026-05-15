#!/bin/bash
# Deploy script for Claude Code Admin Dashboard
# Steps: CloudFormation stack → Lambda zip → SPA upload (with placeholder substitution) → CloudFront invalidation

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

STACK_NAME="claude-code-quota-admin"
REGION="eu-central-1"
PROFILE="poste-claudeaws"
ARTIFACTS_BUCKET="claude-code-s3-cfnartifactsbucket-0enb7bvsxlut"
LAMBDA_S3_KEY="claude-code/admin/dashboard/api.zip"
AZURE_CLIENT_ID="97c4d1b5-7c46-4bff-b6cf-d93898635821"
AZURE_TENANT_ID="761de76f-3d5c-4174-917c-5ad4d06360cb"

TEMPLATE="$REPO_ROOT/deployment/infrastructure/quota-admin-dashboard.yaml"
LAMBDA_SRC="$REPO_ROOT/deployment/infrastructure/lambda-functions/quota_admin/index.py"
SPA_DIR="$REPO_ROOT/deployment/infrastructure/lambda-functions/quota_admin/spa"

echo "╭─────────────────────────────────────────────────╮"
echo "│  Claude Code Admin Dashboard — Deploy            │"
echo "╰─────────────────────────────────────────────────╯"
echo ""

# ============================================================
# Step 1: Deploy CloudFormation stack
# ============================================================
echo "→ [1/4] Deploy CloudFormation stack..."
aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_IAM \
  --region "$REGION" \
  --profile "$PROFILE" \
  --no-fail-on-empty-changeset

echo "✓ Stack deployed"
echo ""

# ============================================================
# Step 2: Package and upload Lambda
# ============================================================
echo "→ [2/4] Package e upload Lambda..."

TMP_DIR=$(mktemp -d)
cp "$LAMBDA_SRC" "$TMP_DIR/index.py"

pushd "$TMP_DIR" > /dev/null
zip -q api.zip index.py
popd > /dev/null

aws s3 cp "$TMP_DIR/api.zip" "s3://$ARTIFACTS_BUCKET/$LAMBDA_S3_KEY" \
  --sse AES256 \
  --region "$REGION" \
  --profile "$PROFILE"

rm -rf "$TMP_DIR"

# Update Lambda function code directly
aws lambda update-function-code \
  --function-name ClaudeCode-QuotaAdmin \
  --s3-bucket "$ARTIFACTS_BUCKET" \
  --s3-key "$LAMBDA_S3_KEY" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --output text --query 'FunctionArn' > /dev/null

echo "✓ Lambda aggiornata"
echo ""

# ============================================================
# Step 3: Fetch stack outputs and upload SPA
# ============================================================
echo "→ [3/4] Upload SPA..."

API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" \
  --output text)

SPA_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query "Stacks[0].Outputs[?OutputKey=='SPABucketName'].OutputValue" \
  --output text)

CF_DISTRIBUTION_ID=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue" \
  --output text)

DASHBOARD_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue" \
  --output text)

if [ -z "$API_ENDPOINT" ] || [ -z "$SPA_BUCKET" ]; then
  echo "✗ Impossibile leggere gli output dello stack. Verificare il deploy."
  exit 1
fi

echo "  API Endpoint : $API_ENDPOINT"
echo "  SPA Bucket   : $SPA_BUCKET"
echo "  Dashboard URL: $DASHBOARD_URL"
echo ""

# Substitute placeholders in app.js before upload
TMP_APP=$(mktemp /tmp/app_XXXXXX.js)
sed \
  -e "s|__API_ENDPOINT__|$API_ENDPOINT|g" \
  -e "s|__AZURE_CLIENT_ID__|$AZURE_CLIENT_ID|g" \
  -e "s|__AZURE_TENANT_ID__|$AZURE_TENANT_ID|g" \
  "$SPA_DIR/app.js" > "$TMP_APP"

aws s3 cp "$TMP_APP" "s3://$SPA_BUCKET/app.js" \
  --sse AES256 --content-type "application/javascript" \
  --region "$REGION" --profile "$PROFILE"
rm -f "$TMP_APP"

aws s3 cp "$SPA_DIR/index.html" "s3://$SPA_BUCKET/index.html" \
  --sse AES256 --content-type "text/html" \
  --region "$REGION" --profile "$PROFILE"

aws s3 cp "$SPA_DIR/style.css" "s3://$SPA_BUCKET/style.css" \
  --sse AES256 --content-type "text/css" \
  --region "$REGION" --profile "$PROFILE"

echo "✓ SPA caricata"
echo ""

# ============================================================
# Step 4: CloudFront invalidation
# ============================================================
echo "→ [4/4] Invalidazione CloudFront..."

aws cloudfront create-invalidation \
  --distribution-id "$CF_DISTRIBUTION_ID" \
  --paths "/*" \
  --region "$REGION" \
  --profile "$PROFILE" \
  --output text --query 'Invalidation.Id' > /dev/null

echo "✓ Invalidazione avviata"
echo ""

# ============================================================
# Summary
# ============================================================
echo "╭─────────────────────────────────────────────────╮"
echo "│  ✓ Deploy completato                             │"
echo "╰─────────────────────────────────────────────────╯"
echo ""
echo "  Dashboard URL : $DASHBOARD_URL"
echo "  API Endpoint  : $API_ENDPOINT"
echo ""
