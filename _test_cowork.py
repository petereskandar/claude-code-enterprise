"""Quick smoke test for generate_claude_desktop_config()."""
import sys
import types
import json
import tempfile
from pathlib import Path

# Stub out heavy dependencies so we can import cowork_3p without the full package
for mod_name in [
    "claude_code_with_bedrock",
    "claude_code_with_bedrock.cli",
    "claude_code_with_bedrock.cli.utils",
    "claude_code_with_bedrock.cli.utils.aws",
]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)
# Stub get_stack_outputs
sys.modules["claude_code_with_bedrock.cli.utils.aws"].get_stack_outputs = lambda *a, **kw: {}  # type: ignore

# Stub botocore so the credential_helper_bat section doesn't break
for mod_name in ["botocore", "botocore.auth", "botocore.awsrequest", "botocore.credentials"]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)

import importlib.util
spec = importlib.util.spec_from_file_location(
    "cowork_3p",
    Path(__file__).parent / "source" / "claude_code_with_bedrock" / "cli" / "utils" / "cowork_3p.py",
)
cowork_3p = importlib.util.module_from_spec(spec)  # type: ignore
spec.loader.exec_module(cowork_3p)  # type: ignore

generate_claude_desktop_config = cowork_3p.generate_claude_desktop_config
build_mdm_config = cowork_3p.build_mdm_config

cfg = build_mdm_config("eu-central-1", ["opus", "sonnet", "haiku", "opusplan"])

with tempfile.TemporaryDirectory() as d:
    p = generate_claude_desktop_config(Path(d), cfg, "ClaudeCode")

    # No BOM
    raw = p.read_bytes()
    assert raw[0:1] == b"{", f"Expected '{{' (no BOM), got: {raw[0:4].hex()}"

    content = p.read_text("ascii")
    print("--- Generated template ---")
    print(content)

    # Sentinel present
    assert "__USERPROFILE__" in content, "sentinel missing!"

    # Valid JSON
    parsed = json.loads(content)
    assert "enterpriseConfig" in parsed, "enterpriseConfig key missing!"
    ec = parsed["enterpriseConfig"]
    assert ec.get("inferenceProvider") == "bedrock"
    assert "__USERPROFILE__" in ec["inferenceCredentialHelper"]
    assert "claude-code-with-bedrock" in ec["inferenceCredentialHelper"]
    assert "credential-helper-ClaudeCode.bat" in ec["inferenceCredentialHelper"]

    # Simulate PowerShell substitution in install.bat
    fake_userprofile = r"C:\Users\testuser"
    fake_up_json = fake_userprofile.replace("\\", "\\\\")  # C:\\Users\\testuser
    expanded = content.replace("__USERPROFILE__", fake_up_json)
    expanded_parsed = json.loads(expanded)
    helper = expanded_parsed["enterpriseConfig"]["inferenceCredentialHelper"]
    expected = r"C:\Users\testuser\claude-code-with-bedrock\credential-helper-ClaudeCode.bat"
    assert helper == expected, f"After substitution: {helper!r}"

    print(f"\n--- After __USERPROFILE__ substitution ---")
    print(f"  inferenceCredentialHelper = {helper}")
    print("\nALL ASSERTIONS PASSED OK")
