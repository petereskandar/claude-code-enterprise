import json
import boto3
import os
import time
from datetime import datetime, timedelta
import sys
sys.path.append('/opt')
from query_utils import validate_time_range
try:
    from metrics_utils import get_metric_statistics
except ImportError:
    pass

_pricing_cache = None
_pricing_cache_time = 0.0
CACHE_TTL_SECONDS = 3600

# Cache ARN → resolved model ID (application inference profiles)
_profile_cache = {}


def get_pricing(ssm_client, param_name):
    global _pricing_cache, _pricing_cache_time
    now = time.monotonic()
    if _pricing_cache is None or (now - _pricing_cache_time) > CACHE_TTL_SECONDS:
        response = ssm_client.get_parameter(Name=param_name)
        _pricing_cache = json.loads(response['Parameter']['Value'])
        _pricing_cache_time = now
        print(f"Refreshed pricing from SSM — models: {list(_pricing_cache.get('models', {}).keys())}")
    return _pricing_cache


def resolve_model_id(bedrock_client, model_id):
    """
    Resolve an ARN (application or cross-region inference profile) to a base model ID.
    Returns the model_id unchanged if it is already a plain model string.
    """
    if not model_id.startswith('arn:'):
        return model_id

    if model_id in _profile_cache:
        return _profile_cache[model_id]

    try:
        resp = bedrock_client.get_inference_profile(inferenceProfileIdentifier=model_id)
        # models[] list — each entry has modelArn; pick the first
        models = resp.get('models', [])
        if models:
            model_arn = models[0].get('modelArn', '')
            # modelArn is like arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-7
            resolved = model_arn.split('/')[-1] if '/' in model_arn else model_arn
            print(f"Resolved profile {model_id} → {resolved}")
            _profile_cache[model_id] = resolved
            return resolved
    except Exception as e:
        print(f"Could not resolve inference profile {model_id}: {e}")

    _profile_cache[model_id] = model_id  # negative cache to avoid repeated calls
    return model_id


def get_model_pricing(pricing, model_id):
    """Return pricing dict for a specific model, falling back to 'default'."""
    models = pricing.get('models', {})
    if model_id in models:
        return models[model_id]
    # Partial match — handles both variations of the same model family
    for key in models:
        if key in model_id or model_id in key:
            return models[key]
    return pricing.get('default', {
        'input_per_million': 3.00, 'output_per_million': 15.00,
        'cache_read_per_million': 0.30, 'cache_write_per_million': 3.75,
    })


def compute_cw_period(start_time_ms, end_time_ms):
    """Return a CloudWatch period that keeps datapoints under 1440."""
    range_s = (end_time_ms - start_time_ms) / 1000
    if range_s <= 300 * 1440:
        return 300
    elif range_s <= 3600 * 1440:
        return 3600
    return 86400


def format_cost(amount):
    if amount >= 1000:
        return f"${amount:,.0f}"
    elif amount >= 1:
        return f"${amount:,.2f}"
    else:
        return f"${amount:.4f}"


def short_model_name(model_id):
    """Extract a short human-readable label from a model ID or ARN."""
    import re
    if model_id.startswith('arn:'):
        # Try to get the profile/resource part
        parts = model_id.split('/')
        return parts[-1][:12] + '…' if len(parts) > 1 else 'Profile'
    # e.g. "eu.anthropic.claude-opus-4-7" → "Opus 4.7"
    for prefix in ('eu.', 'us.', 'global.', 'au.', 'anthropic.'):
        model_id = model_id.replace(prefix, '')
    model_id = model_id.replace('anthropic.', '').replace('-v1:0', '').replace('-v1', '')
    model_id = model_id.replace('claude-', '')
    # Remove date suffixes like -20250514
    model_id = re.sub(r'-\d{8}', '', model_id)
    return model_id.replace('-', ' ').title()


def lambda_handler(event, context):
    if event.get('describe', False):
        return {"markdown": "# Total Cost USD\nEstimated cost based on per-model token pricing stored in SSM Parameter Store. Shows per-model breakdown when per-model metrics are available."}

    region = os.environ['METRICS_REGION']
    param_name = os.environ['PRICING_PARAM_NAME']
    max_query_days = int(os.environ.get('MAX_QUERY_DAYS', '7'))

    widget_context = event.get('widgetContext', {})
    time_range = widget_context.get('timeRange', {})
    widget_size = widget_context.get('size', {})
    width = widget_size.get('width', 300)
    height = widget_size.get('height', 200)

    # CloudWatch Metrics (ClaudeCode namespace) are published by the aggregator in
    # the Lambda's own region, NOT in METRICS_REGION (which is the Logs region).
    cloudwatch_client = boto3.client('cloudwatch')
    # SSM parameter lives in the same region as the Lambda (stack region)
    ssm_client = boto3.client('ssm')
    # Bedrock inference profiles are in METRICS_REGION
    bedrock_client = boto3.client('bedrock', region_name=region)

    try:
        if 'start' in time_range and 'end' in time_range:
            start_time = time_range['start']
            end_time = time_range['end']
        else:
            end_time = int(datetime.now().timestamp() * 1000)
            start_time = int((datetime.now() - timedelta(hours=1)).timestamp() * 1000)

        is_valid, range_days, error_html = validate_time_range(start_time, end_time, max_query_days)
        if not is_valid:
            return error_html

        pricing = get_pricing(ssm_client, param_name)
        period = compute_cw_period(start_time, end_time)

        def sum_metric(metric_name, dimensions=None):
            dps = get_metric_statistics(cloudwatch_client, metric_name, start_time, end_time, dimensions, 'Sum', period)
            return sum(p.get('Sum', 0) for p in dps) if dps else 0

        # Discover which model IDs have per-model dimensional metrics
        models_in_cw = set()
        paginator = cloudwatch_client.get_paginator('list_metrics')
        for page in paginator.paginate(
            Namespace='ClaudeCode',
            MetricName='InputTokens',
            Dimensions=[{'Name': 'ModelId'}]
        ):
            for m in page.get('Metrics', []):
                for d in m.get('Dimensions', []):
                    if d['Name'] == 'ModelId':
                        models_in_cw.add(d['Value'])

        model_costs = {}
        total_cost = 0.0

        if models_in_cw:
            # Per-model cost: each model × its own pricing
            for model_id in sorted(models_in_cw):
                # Resolve inference profile ARN → underlying base model for pricing lookup
                base_model = resolve_model_id(bedrock_client, model_id)
                dims = [{'Name': 'ModelId', 'Value': model_id}]
                p = get_model_pricing(pricing, base_model)
                inp = sum_metric('InputTokens', dims)
                out = sum_metric('OutputTokens', dims)
                cr  = sum_metric('CacheReadTokens', dims)
                cw  = sum_metric('CacheCreationTokens', dims)
                cost = (inp / 1e6 * p.get('input_per_million', 3.0)
                      + out / 1e6 * p.get('output_per_million', 15.0)
                      + cr  / 1e6 * p.get('cache_read_per_million', 0.30)
                      + cw  / 1e6 * p.get('cache_write_per_million', 3.75))
                model_costs[model_id] = cost
                total_cost += cost
        else:
            # Fallback: dimensionless totals × default pricing
            dp = pricing.get('default', {})
            inp = sum_metric('InputTokens')
            out = sum_metric('OutputTokens')
            cr  = sum_metric('CacheReadTokens')
            cw  = sum_metric('CacheCreationTokens')
            total_cost = (inp / 1e6 * dp.get('input_per_million', 3.0)
                        + out / 1e6 * dp.get('output_per_million', 15.0)
                        + cr  / 1e6 * dp.get('cache_read_per_million', 0.30)
                        + cw  / 1e6 * dp.get('cache_write_per_million', 3.75))

        formatted_total = format_cost(total_cost)
        font_size = min(width // 10, height // 5, 48)

        if total_cost == 0:
            bg_gradient = "linear-gradient(135deg, #6b7280 0%, #4b5563 100%)"
            subtitle = "No Usage"
            breakdown_html = ""
        else:
            bg_gradient = "linear-gradient(135deg, #059669 0%, #047857 100%)"
            subtitle = "Estimated Cost (USD)"
            if model_costs:
                sorted_models = sorted(model_costs.items(), key=lambda x: x[1], reverse=True)
                rows_html = ''.join(
                    f'<div style="display:flex;justify-content:space-between;width:100%;padding:0 4px;">'
                    f'<span style="opacity:0.85;">{short_model_name(mid)}</span>'
                    f'<span style="font-weight:600;">{format_cost(c)}</span></div>'
                    for mid, c in sorted_models[:4]
                )
                breakdown_html = (
                    '<div style="font-size:10px;color:rgba(255,255,255,0.8);'
                    'margin-top:6px;line-height:1.6;width:100%;">'
                    + rows_html + '</div>'
                )
            else:
                breakdown_html = ""

        return f"""
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;
                    height:100%;font-family:'Amazon Ember',-apple-system,sans-serif;
                    background:{bg_gradient};border-radius:8px;padding:10px;
                    box-sizing:border-box;overflow:hidden;">
            <div style="font-size:{font_size}px;font-weight:700;color:white;
                        text-shadow:0 2px 4px rgba(0,0,0,0.2);margin-bottom:4px;line-height:1;">
                {formatted_total}</div>
            <div style="font-size:12px;color:rgba(255,255,255,0.9);text-transform:uppercase;
                        letter-spacing:0.5px;font-weight:500;line-height:1;">
                {subtitle}</div>
            {breakdown_html}
        </div>
        """

    except Exception as e:
        return f"""
        <div style="display:flex;align-items:center;justify-content:center;height:100%;
                    background:#fef2f2;border-radius:8px;padding:10px;box-sizing:border-box;
                    overflow:hidden;font-family:'Amazon Ember',-apple-system,sans-serif;">
            <div style="text-align:center;width:100%;overflow:hidden;">
                <div style="color:#991b1b;font-weight:600;margin-bottom:4px;font-size:14px;">Data Unavailable</div>
                <div style="color:#7f1d1d;font-size:10px;">{str(e)[:100]}</div>
            </div>
        </div>
        """
