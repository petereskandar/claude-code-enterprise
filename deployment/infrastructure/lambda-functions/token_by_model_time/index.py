import json
import boto3
import os
from datetime import datetime, timedelta
import sys
sys.path.append('/opt')
from query_utils import rate_limited_start_query, wait_for_query_results, validate_time_range


MODEL_COLORS = {
    'Opus 4.7': '#14b8a6',
    'Opus 4.1': '#3b82f6',
    'Opus 4': '#f97316',
    'Sonnet 4.5': '#a855f7',
    'Sonnet 4': '#10b981',
    'Sonnet 3.7': '#ef4444',
    'Haiku 3.5': '#8b5cf6',
}

MODEL_PATTERNS = [
    ('opus-4-7', 'Opus 4.7'),
    ('opus-4-1', 'Opus 4.1'),
    ('opus-4', 'Opus 4'),
    ('sonnet-4-5', 'Sonnet 4.5'),
    ('sonnet-4', 'Sonnet 4'),
    ('sonnet-3-7', 'Sonnet 3.7'),
    ('3-7-sonnet', 'Sonnet 3.7'),
    ('haiku-3-5', 'Haiku 3.5'),
    ('3-5-haiku', 'Haiku 3.5'),
]


def classify_model(model_str):
    model_lower = model_str.lower().replace('.', '-')
    for pattern, name in MODEL_PATTERNS:
        if pattern in model_lower:
            return name
    return model_str[:20]


def lambda_handler(event, context):
    if event.get('describe', False):
        return {"markdown": "# Token Usage by Model Over Time\nTime series of token usage broken down by model"}

    log_group = os.environ['METRICS_LOG_GROUP']
    region = os.environ['METRICS_REGION']
    max_query_days = int(os.environ.get('MAX_QUERY_DAYS', '7'))

    widget_context = event.get('widgetContext', {})
    time_range = widget_context.get('timeRange', {})

    logs_client = boto3.client('logs', region_name=region)

    try:
        if 'start' in time_range and 'end' in time_range:
            start_time = time_range['start']
            end_time = time_range['end']
        else:
            end_time = int(datetime.now().timestamp() * 1000)
            start_time = int((datetime.now() - timedelta(hours=6)).timestamp() * 1000)

        is_valid, range_days, error_html = validate_time_range(start_time, end_time, max_query_days)
        if not is_valid:
            return error_html

        query = """
        fields @message
        | filter @message like /claude_code.token.usage/
        | parse @message /"model":"(?<model>[^"]*)"/
        | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
        | stats sum(tokens) as total by bin(5m) as time, model
        | sort time asc
        """

        response = rate_limited_start_query(logs_client, log_group, start_time, end_time, query)
        query_id = response['queryId']
        response = wait_for_query_results(logs_client, query_id)

        if response.get('status') != 'Complete':
            raise Exception(f"Query status: {response.get('status', 'Unknown')}")

        results = response.get('results', [])

        if not results:
            return '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ca3af;font-family:\'Amazon Ember\',-apple-system,sans-serif;font-size:14px;">No token usage data for this time range</div>'

        # Group by time bucket and model
        from collections import defaultdict
        time_model_data = defaultdict(lambda: defaultdict(float))
        all_models = set()
        all_times = set()

        for row in results:
            t_val = None
            model = None
            total = 0
            for field in row:
                if field['field'] == 'time':
                    t_val = field['value']
                elif field['field'] == 'model':
                    model = field['value']
                elif field['field'] == 'total':
                    total = float(field['value'])
            if t_val and model:
                display_name = classify_model(model)
                time_model_data[t_val][display_name] += total
                all_models.add(display_name)
                all_times.add(t_val)

        if not all_times:
            return '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ca3af;font-size:14px;">No data</div>'

        sorted_times = sorted(all_times)
        # Sort models by total tokens descending
        model_totals = {m: sum(time_model_data[t].get(m, 0) for t in sorted_times) for m in all_models}
        sorted_models = sorted(all_models, key=lambda m: model_totals[m], reverse=True)[:7]

        # Find max for y-axis
        max_val = 0
        for t in sorted_times:
            s = sum(time_model_data[t].get(m, 0) for m in sorted_models)
            max_val = max(max_val, s)
        max_val = max_val or 1

        w, h = 560, 160
        ml, mb = 60, 30
        n = len(sorted_times)

        def fmt(v):
            if v >= 1e6: return f"{v/1e6:.1f}M"
            if v >= 1e3: return f"{v/1e3:.0f}K"
            return f"{v:.0f}"

        # Build stacked area chart
        svg_elements = []
        svg_elements.append(f'<line x1="{ml}" y1="{h-mb}" x2="{w-10}" y2="{h-mb}" stroke="#e5e7eb" stroke-width="1"/>')
        svg_elements.append(f'<text x="{ml-5}" y="15" text-anchor="end" fill="#6b7280" font-size="10">{fmt(max_val)}</text>')
        svg_elements.append(f'<text x="{ml-5}" y="{h-mb}" text-anchor="end" fill="#6b7280" font-size="10">0</text>')
        svg_elements.append(f'<text x="{ml}" y="{h-5}" fill="#9ca3af" font-size="9">{sorted_times[0][:16]}</text>')
        svg_elements.append(f'<text x="{w-10}" y="{h-5}" text-anchor="end" fill="#9ca3af" font-size="9">{sorted_times[-1][:16]}</text>')

        # Draw lines for each model
        for model_name in reversed(sorted_models):
            color = MODEL_COLORS.get(model_name, '#6b7280')
            points = []
            for i, t in enumerate(sorted_times):
                x = ml + (i / max(n - 1, 1)) * (w - ml - 10)
                val = time_model_data[t].get(model_name, 0)
                y = 10 + (1 - val / max_val) * (h - mb - 10)
                points.append(f"{x:.1f},{y:.1f}")
            svg_elements.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2" stroke-opacity="0.85"/>')

        # Legend
        legend_y = 12
        for i, model_name in enumerate(sorted_models[:5]):
            color = MODEL_COLORS.get(model_name, '#6b7280')
            lx = ml + 10 + i * 90
            svg_elements.append(f'<rect x="{lx}" y="{legend_y - 6}" width="8" height="8" rx="2" fill="{color}"/>')
            svg_elements.append(f'<text x="{lx + 12}" y="{legend_y + 1}" fill="#374151" font-size="9">{model_name}</text>')

        svg_body = '\n'.join(svg_elements)
        svg = f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;">{svg_body}</svg>'

        return f'<div style="height:100%;font-family:\'Amazon Ember\',-apple-system,sans-serif;padding:4px;box-sizing:border-box;">{svg}</div>'

    except Exception as e:
        return f'''<div style="display:flex;align-items:center;justify-content:center;height:100%;background:#fef2f2;border-radius:8px;padding:10px;box-sizing:border-box;font-family:'Amazon Ember',-apple-system,sans-serif;">
            <div style="text-align:center;"><div style="color:#991b1b;font-weight:600;font-size:14px;">Data Unavailable</div><div style="color:#7f1d1d;font-size:10px;">{str(e)[:120]}</div></div></div>'''
