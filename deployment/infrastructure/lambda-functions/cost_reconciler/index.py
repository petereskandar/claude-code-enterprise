import json
import boto3
import os
from decimal import Decimal
from datetime import datetime, timezone

dynamodb = boto3.resource("dynamodb")
ssm_client = boto3.client("ssm")

QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
PRICING_PARAM_NAME = os.environ.get("PRICING_PARAM_NAME")


def _load_pricing():
    if not PRICING_PARAM_NAME:
        raise ValueError("PRICING_PARAM_NAME not set")
    resp = ssm_client.get_parameter(Name=PRICING_PARAM_NAME)
    return json.loads(resp["Parameter"]["Value"])


def _get_rates(pricing, model):
    models = pricing.get("models", {})
    default = pricing.get("default", {})
    rates = models.get(model)
    if not rates:
        for key in models:
            if key in model or model in key:
                rates = models[key]
                break
    return rates or default


def _cost_for_model(rates, tokens):
    return (
        float(tokens.get("input", 0)) * rates.get("input_per_million", 0) / 1_000_000
        + float(tokens.get("output", 0)) * rates.get("output_per_million", 0) / 1_000_000
        + float(tokens.get("cache_read", 0)) * rates.get("cache_read_per_million", 0) / 1_000_000
        + float(tokens.get("cache_write", 0)) * rates.get("cache_write_per_million", 0) / 1_000_000
    )


def lambda_handler(event, context):
    month = event.get("month") or datetime.now(timezone.utc).strftime("%Y-%m")
    dry_run = event.get("dry_run", False)

    print(f"Reconciling costs for month={month}, dry_run={dry_run}")

    pricing = _load_pricing()
    print(f"Loaded pricing with {len(pricing.get('models', {}))} models")

    table = dynamodb.Table(QUOTA_TABLE)

    scan_kwargs = {
        "FilterExpression": boto3.dynamodb.conditions.Attr("sk").eq(f"MONTH#{month}"),
    }

    updated = 0
    skipped = 0
    total_old_cost = 0.0
    total_new_cost = 0.0
    items_processed = 0

    while True:
        response = table.scan(**scan_kwargs)
        items = response.get("Items", [])

        for item in items:
            items_processed += 1
            pk = item["pk"]
            sk = item["sk"]
            models_map = item.get("models", {})
            old_cost = float(item.get("total_cost", 0))
            total_old_cost += old_cost

            if not models_map:
                skipped += 1
                continue

            new_total_cost = 0.0
            updated_models = {}

            for model_id, tokens in models_map.items():
                rates = _get_rates(pricing, model_id)
                new_model_cost = _cost_for_model(rates, tokens)
                new_total_cost += new_model_cost

                updated_models[model_id] = {
                    "input": tokens.get("input", Decimal("0")),
                    "output": tokens.get("output", Decimal("0")),
                    "cache_read": tokens.get("cache_read", Decimal("0")),
                    "cache_write": tokens.get("cache_write", Decimal("0")),
                    "cost": Decimal(str(round(new_model_cost, 6))),
                }

            total_new_cost += new_total_cost
            diff = new_total_cost - old_cost

            if abs(diff) < 0.0001:
                skipped += 1
                continue

            email = item.get("email", pk)
            print(f"  {email}: ${old_cost:.4f} -> ${new_total_cost:.4f} (diff: ${diff:+.4f})")

            if not dry_run:
                table.update_item(
                    Key={"pk": pk, "sk": sk},
                    UpdateExpression="SET total_cost = :cost, models = :models",
                    ExpressionAttributeValues={
                        ":cost": Decimal(str(round(new_total_cost, 6))),
                        ":models": updated_models,
                    },
                )
                updated += 1
            else:
                updated += 1

        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    summary = {
        "month": month,
        "dry_run": dry_run,
        "items_processed": items_processed,
        "updated": updated,
        "skipped": skipped,
        "total_old_cost": round(total_old_cost, 2),
        "total_new_cost": round(total_new_cost, 2),
        "difference": round(total_new_cost - total_old_cost, 2),
    }

    print(f"Done: {json.dumps(summary)}")
    return summary
