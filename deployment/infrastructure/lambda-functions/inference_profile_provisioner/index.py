import json, logging, os, re, hashlib, boto3
from datetime import datetime, timezone
from botocore.exceptions import ClientError
logger = logging.getLogger()
logger.setLevel(logging.INFO)
_EU_REGIONS = {"eu-west-1","eu-west-2","eu-west-3","eu-central-1","eu-central-2","eu-north-1","eu-south-1","eu-south-2"}
def _load_models():
  raw = os.environ.get("INFERENCE_PROFILE_MODELS_JSON", "{}")
  try:
    return json.loads(raw)
  except json.JSONDecodeError as e:
    logger.error("Failed to parse INFERENCE_PROFILE_MODELS_JSON: %s", e)
    return {}
INFERENCE_PROFILE_MODELS = _load_models()
logger.info("Loaded %d model(s) from config: %s", len(INFERENCE_PROFILE_MODELS), list(INFERENCE_PROFILE_MODELS.keys()))
_MAX_TAG_VALUE = 256
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAPPING_TABLE = None
def _get_mapping_table():
  global _MAPPING_TABLE
  if _MAPPING_TABLE is None:
    name = os.environ.get("INFERENCE_PROFILE_MAPPING_TABLE")
    if name:
      _MAPPING_TABLE = boto3.resource("dynamodb").Table(name)
  return _MAPPING_TABLE
def _model_name(mk):
  e = INFERENCE_PROFILE_MODELS.get(mk, {})
  crid = e.get("cross_region_profile_id", "")
  n = crid.split(".")[-1] if "." in crid else mk
  for sfx in ["-v1:0","-v2:0","-v1","-v2"]:
    if sfx in n: n = n[:n.rfind(sfx)]; break
  return re.sub(r"-\d{8}$", "", n)
def _write_mapping(arns, email):
  tbl = _get_mapping_table()
  if not tbl: return
  now = datetime.now(timezone.utc).isoformat()
  for mk, arn in arns.items():
    try:
      tbl.put_item(Item={"profileArn":arn,"email":email.lower(),"model":_model_name(mk),"modelKey":mk,"profileId":arn.split("/")[-1],"createdAt":now})
    except Exception as e: logger.warning("Could not write mapping for '%s': %s", mk, e)
def _geo(region): return "eu" if region in _EU_REGIONS else "us"
def _source_arn(model_key, region):
  e = INFERENCE_PROFILE_MODELS[model_key]
  pid = e["cross_region_profile_id"].format(geo=_geo(region))
  return f"arn:aws:bedrock:{region}::inference-profile/{pid}"
def _profile_name(email, model_key):
  h = hashlib.sha256(email.encode()).hexdigest()[:8]
  s = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "-", email.lower())).strip("-")
  suffix = f"-{h}-{model_key}"
  return f"claude-code-{s[:64-12-len(suffix)]}{suffix}"
def _tags(email, claims):
  tags = [{"key":"user.email","value":email.lower()[:_MAX_TAG_VALUE]},{"key":"status","value":"enabled"}]
  for ck, tk in [("custom:cost_center","cost_center"),("custom:department","department"),("custom:organization","organization"),("custom:team","team")]:
    v = claims.get(ck)
    if v: tags.append({"key":tk,"value":str(v)[:_MAX_TAG_VALUE]})
  return tags
def handler(event, context):
  if not INFERENCE_PROFILE_MODELS:
    logger.error("No models configured â€” check INFERENCE_PROFILE_MODELS_JSON env var")
    return {"statusCode":500,"body":json.dumps({"error":"No models configured"})}
  region = boto3.session.Session().region_name
  bedrock = boto3.client("bedrock", region_name=region)
  email = (event.get("email") or "").strip()
  if not email or not _EMAIL_RE.match(email):
    logger.error("Invalid or missing email: %r", email)
    return {"statusCode":400,"body":json.dumps({"error":"Invalid or missing email in payload"})}
  claims = event.get("claims") or {}
  tags = _tags(email, claims)
  arns = {}
  enabled = {k:v for k,v in INFERENCE_PROFILE_MODELS.items() if v.get("enabled")}
  expected = {_profile_name(email, mk): mk for mk in enabled}
  try:
    pg = bedrock.get_paginator("list_inference_profiles")
    for page in pg.paginate(typeEquals="APPLICATION"):
      for p in page.get("inferenceProfileSummaries",[]):
        pn = p.get("inferenceProfileName","")
        if pn in expected:
          arns[expected[pn]] = p["inferenceProfileArn"]
          logger.info("Found existing '%s': %s", expected[pn], p["inferenceProfileArn"])
  except Exception as e: logger.warning("Could not list profiles: %s", e)
  if all(mk in arns for mk in enabled):
    logger.info("All %d profiles exist for %s", len(arns), email)
    _write_mapping(arns, email)
    return {"statusCode":200,"body":json.dumps({"profile_arns":arns})}
  for mk in enabled:
    if mk in arns: continue
    pname = _profile_name(email, mk)
    logger.info("Creating profile '%s' for '%s'", pname, mk)
    try:
      r = bedrock.create_inference_profile(inferenceProfileName=pname, description="Claude Code inference profile", modelSource={"copyFrom":_source_arn(mk,region)}, tags=tags)
      arns[mk] = r["inferenceProfileArn"]
      logger.info("Created '%s': %s", mk, arns[mk])
    except ClientError as e: logger.warning("Could not create profile for '%s': %s", mk, e)
    except Exception as e: logger.warning("Unexpected error for '%s': %s", mk, e)
  _write_mapping(arns, email)
  logger.info("Returning %d ARN(s) for %s", len(arns), email)
  return {"statusCode":200,"body":json.dumps({"profile_arns":arns})}
