import json
import boto3
import os
from datetime import datetime, timedelta
import sys
sys.path.append('/opt')
from query_utils import rate_limited_start_query, wait_for_query_results, validate_time_range


def lambda_handler(event, context):
    if event.get('describe', False):
        return {"markdown": "# Token Usage Over Time\nTime series of total token usage in 5-minute bins"}

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
        fields @timestamp, @message
        | filter @message like /claude_code.token.usage/
        | parse @message /"claude_code.token.usage":(?<tokens>[0-9.]+)/
        | stats sum(tokens) as total_tokens by bin(5m) as time
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

        # Build SVG time series chart
        data_points = []
        for row in results:
            t_val = None
            tokens = 0
            for field in row:
                if field['field'] == 'time':
                    t_val = field['value']
                elif field['field'] == 'total_tokens':
                    tokens = float(field['value'])
            if t_val:
                data_points.append((t_val, tokens))

        if not data_points:
            return '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ca3af;font-size:14px;">No data</div>'

        max_val = max(t[1] for t in data_points) or 1
        n = len(data_points)
        w, h = 560, 160
        margin_left, margin_bottom = 60, 30

        # Build SVG path
        points = []
        for i, (_, val) in enumerate(data_points):
            x = margin_left + (i / max(n - 1, 1)) * (w - margin_left - 10)
            y = 10 + (1 - val / max_val) * (h - margin_bottom - 10)
            points.append(f"{x:.1f},{y:.1f}")

        polyline = ' '.join(points)
        # Area fill
        area = f"M{points[0]} L{' L'.join(points)} L{points[-1].split(',')[0]},{h - margin_bottom} L{margin_left},{h - margin_bottom} Z"

        def fmt(v):
            if v >= 1e6: return f"{v/1e6:.1f}M"
            if v >= 1e3: return f"{v/1e3:.0f}K"
            return f"{v:.0f}"

        svg = f'''<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%;">
          <defs><linearGradient id="tg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#667eea" stop-opacity="0.3"/><stop offset="100%" stop-color="#667eea" stop-opacity="0.02"/></linearGradient></defs>
          <line x1="{margin_left}" y1="{h - margin_bottom}" x2="{w - 10}" y2="{h - margin_bottom}" stroke="#e5e7eb" stroke-width="1"/>
          <text x="{margin_left - 5}" y="15" text-anchor="end" fill="#6b7280" font-size="10">{fmt(max_val)}</text>
          <text x="{margin_left - 5}" y="{h - margin_bottom}" text-anchor="end" fill="#6b7280" font-size="10">0</text>
          <text x="{margin_left}" y="{h - 5}" fill="#9ca3af" font-size="9">{data_points[0][0][:16]}</text>
          <text x="{w - 10}" y="{h - 5}" text-anchor="end" fill="#9ca3af" font-size="9">{data_points[-1][0][:16]}</text>
          <path d="{area}" fill="url(#tg)"/>
          <polyline points="{polyline}" fill="none" stroke="#667eea" stroke-width="2"/>
        </svg>'''

        return f'<div style="height:100%;font-family:\'Amazon Ember\',-apple-system,sans-serif;padding:4px;box-sizing:border-box;">{svg}</div>'

    except Exception as e:
        return f'''<div style="display:flex;align-items:center;justify-content:center;height:100%;background:#fef2f2;border-radius:8px;padding:10px;box-sizing:border-box;font-family:'Amazon Ember',-apple-system,sans-serif;">
            <div style="text-align:center;"><div style="color:#991b1b;font-weight:600;font-size:14px;">Data Unavailable</div><div style="color:#7f1d1d;font-size:10px;">{str(e)[:120]}</div></div></div>'''
