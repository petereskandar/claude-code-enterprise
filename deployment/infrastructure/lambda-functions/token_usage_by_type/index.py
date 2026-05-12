import json
import boto3
import os
import sys
from datetime import datetime
sys.path.append('/opt')
from widget_utils import parse_widget_context, get_time_range, check_describe_mode
from html_utils import generate_error_html, generate_no_data_html
from format_utils import format_number, format_percentage


def _discover_models(cloudwatch_client):
    models = set()
    paginator = cloudwatch_client.get_paginator('list_metrics')
    for page in paginator.paginate(
        Namespace='ClaudeCode',
        MetricName='claude_code.token.usage',
        Dimensions=[{'Name': 'model'}],
    ):
        for m in page.get('Metrics', []):
            for d in m.get('Dimensions', []):
                if d['Name'] == 'model':
                    models.add(d['Value'])
    return sorted(models)


def lambda_handler(event, context):
    if check_describe_mode(event):
        return {"markdown": "# Token Usage by Type\nDistribution of tokens by operation type"}

    region = os.environ["METRICS_REGION"]

    widget_ctx = parse_widget_context(event)
    time_range = widget_ctx['time_range']

    cloudwatch_client = boto3.client("cloudwatch", region_name=region)

    try:
        start_time, end_time = get_time_range(time_range, default_hours=7*24)

        start_dt = datetime.fromtimestamp(start_time / 1000)
        end_dt = datetime.fromtimestamp(end_time / 1000)

        range_seconds = (end_time - start_time) / 1000
        period = max(300, ((int(range_seconds / 1440) + 59) // 60) * 60)

        token_type_map = [
            ('input', 'Input Tokens'),
            ('output', 'Output Tokens'),
            ('cacheCreation', 'Cache Creation'),
            ('cacheRead', 'Cache Read'),
        ]

        models = _discover_models(cloudwatch_client)

        queries = []
        query_index = {}
        idx = 0
        for type_value, _ in token_type_map:
            for model in models:
                metric_id = f"m{idx}"
                queries.append({
                    'Id': metric_id,
                    'MetricStat': {
                        'Metric': {
                            'Namespace': 'ClaudeCode',
                            'MetricName': 'claude_code.token.usage',
                            'Dimensions': [
                                {'Name': 'model', 'Value': model},
                                {'Name': 'type', 'Value': type_value},
                            ],
                        },
                        'Period': period,
                        'Stat': 'Sum',
                    },
                    'ReturnData': True,
                })
                query_index[metric_id] = type_value
                idx += 1

        type_totals = {tv: 0.0 for tv, _ in token_type_map}

        for batch_start in range(0, len(queries), 500):
            batch = queries[batch_start:batch_start + 500]
            response = cloudwatch_client.get_metric_data(
                MetricDataQueries=batch,
                StartTime=start_dt,
                EndTime=end_dt,
            )
            for result in response.get('MetricDataResults', []):
                mid = result['Id']
                values = result.get('Values', [])
                if values:
                    type_totals[query_index[mid]] += sum(values)

        token_types = []
        for type_value, display_name in token_type_map:
            total = type_totals[type_value]
            if total > 0:
                token_types.append({
                    "type": display_name,
                    "tokens": total,
                })

        if not token_types:
            return generate_no_data_html(
                "No Token Data",
                "No token usage data available for this period"
            )

        total_tokens = sum(t["tokens"] for t in token_types)

        colors = {
            "Input Tokens": "#3b82f6",
            "Output Tokens": "#ef4444",
            "Cache Creation": "#10b981",
            "Cache Read": "#8b5cf6",
        }

        token_types.sort(key=lambda x: x["tokens"], reverse=True)

        legend_html = ""
        max_tokens = max(t["tokens"] for t in token_types) if token_types else 1

        for item in token_types:
            bar_width = (item["tokens"] / max_tokens * 100) if max_tokens > 0 else 0
            color = colors.get(item["type"], "#667eea")

            legend_html += f"""
            <div style="
                display: flex;
                align-items: center;
                width: 100%;
                height: 24px;
                margin-bottom: 6px;
                font-family: 'Amazon Ember', -apple-system, sans-serif;
            ">
                <div style="
                    width: 100px;
                    padding-right: 8px;
                    font-size: 11px;
                    font-weight: 600;
                    color: #374151;
                    text-align: right;
                    flex-shrink: 0;
                ">{item['type']}</div>
                <div style="
                    flex: 1;
                    position: relative;
                    height: 20px;
                    background: #f3f4f6;
                    border-radius: 3px;
                    overflow: hidden;
                ">
                    <div style="
                        width: {bar_width}%;
                        height: 100%;
                        background: {color};
                        transition: width 0.3s ease;
                    "></div>
                </div>
                <div style="
                    width: 100px;
                    padding-left: 8px;
                    font-size: 10px;
                    font-weight: 600;
                    color: #374151;
                    text-align: left;
                    flex-shrink: 0;
                ">{format_percentage(item['tokens'], total_tokens)} | {format_number(item['tokens'])}</div>
            </div>
            """

        return f"""
        <div style="
            padding: 12px;
            height: 100%;
            font-family: 'Amazon Ember', -apple-system, sans-serif;
            background: white;
            border-radius: 8px;
            box-sizing: border-box;
            overflow-y: auto;
        ">
            {legend_html}
        </div>
        """

    except Exception as e:
        return generate_error_html(str(e))
