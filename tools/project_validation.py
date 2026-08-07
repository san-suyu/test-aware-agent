from strands import tool
from pathlib import Path
import json

@tool
def validate_project_access(
  repo_path: str,
  registry_path: str = "test-registry.json",
  write_probe: bool = False
) -> str:
  """Check whether the target repo and test registry are visible to the agent."""

  repo_root = Path(repo_path).resolve()
  registry_file = (repo_root / registry_path).resolve()

  result = {
    "ok": True,
    "repoPath": str(repo_root),
    "repoExists": repo_root.exists() and repo_root.is_dir(),
    "registryPath": str(registry_file),
    "registryExists": registry_file.exists() and registry_file.is_file(),
    "writeProbeRequested": write_probe,
    "writeProbeOk": None,
    "errors": [],
    "notes": [],
  }

  if not result["repoExists"]:
    result["ok"] = False
    result["errors"].append(f"repo_path does not exist or is not a directory: {repo_root}")
    return json.dumps(result, indent=2)

  try:
    registry_file.relative_to(repo_root)
  except ValueError:
    result["ok"] = False
    result["errors"].append(f"registry_path is outside repo: {registry_file}")
    return json.dumps(result, indent=2)

  if not result["registryExists"]:
    result["ok"] = False
    result["errors"].append(f"test registry not found: {registry_file}")

  if write_probe:
    probe_file = repo_root / ".test-aware-agent-write-check.tmp"

    try:
      probe_file.write_text("write check\n", encoding="utf-8")
      probe_file.unlink()
      result["writeProbeOk"] = True
    except OSError as error:
      result["ok"] = False
      result["writeProbeOk"] = False
      result["errors"].append(f"repo write probe failed: {error}")
  else:
    result["notes"].append("write_probe was false, so repo write access was not tested.")

  if result["ok"]:
    result["notes"].append("Target repo and registry are visible to the agent process.")

  return json.dumps(result, indent=2)
