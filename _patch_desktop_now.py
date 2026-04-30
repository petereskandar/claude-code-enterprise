"""
Immediately patch %LOCALAPPDATA%\\Claude-3p\\claude_desktop_config.json
to use application inference profile ARNs from ~/.claude/settings.json.
Run with: python _patch_desktop_now.py
"""
import json, os, sys, tempfile
from pathlib import Path

# Read ARNs from ~/.claude/settings.json
settings_path = Path.home() / ".claude" / "settings.json"
if not settings_path.exists():
    print("ERROR: ~/.claude/settings.json not found")
    sys.exit(1)

with open(settings_path) as f:
    settings = json.load(f)

env = settings.get("env", {})
opus_arn   = env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "")
sonnet_arn = env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
haiku_arn  = env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "")

if not all(arn and "application-inference-profile" in arn for arn in [opus_arn, sonnet_arn, haiku_arn]):
    print("ERROR: settings.json does not contain application inference profile ARNs.")
    print(f"  ANTHROPIC_DEFAULT_OPUS_MODEL   = {opus_arn!r}")
    print(f"  ANTHROPIC_DEFAULT_SONNET_MODEL = {sonnet_arn!r}")
    print(f"  ANTHROPIC_DEFAULT_HAIKU_MODEL  = {haiku_arn!r}")
    print("\nRun --setup-profiles first to create the per-user inference profiles.")
    sys.exit(1)

print(f"Found ARNs:")
print(f"  opus   = {opus_arn}")
print(f"  sonnet = {sonnet_arn}")
print(f"  haiku  = {haiku_arn}")

# Patch claude_desktop_config.json
local_app_data = os.environ.get("LOCALAPPDATA", "")
config_path = Path(local_app_data) / "Claude-3p" / "claude_desktop_config.json"

if not config_path.exists():
    print(f"ERROR: {config_path} not found")
    sys.exit(1)

with open(config_path, encoding="ascii") as f:
    data = json.load(f)

# inferenceModels: [opus, sonnet, haiku, opusplan(=opus for extended thinking)]
new_models = [opus_arn, sonnet_arn, haiku_arn, opus_arn]

current = data.get("enterpriseConfig", {}).get("inferenceModels", [])
if current == new_models:
    print("\nAlready correct — no update needed.")
    sys.exit(0)

print(f"\nCurrent  inferenceModels: {current}")
print(f"Updating inferenceModels: {new_models}")

data.setdefault("enterpriseConfig", {})["inferenceModels"] = new_models

# Atomic write, ASCII, no BOM
tmp_fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, prefix=".cdc.tmp")
try:
    with os.fdopen(tmp_fd, "w", encoding="ascii") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, config_path)
except Exception:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise

print(f"\nOK: {config_path} updated with application inference profile ARNs.")
print("\nRestart Claude Desktop to apply the changes.")
