from strands import Agent, tool
from strands.models.ollama import OllamaModel
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from fnmatch import fnmatch
import json
from pathlib import Path
import subprocess
import time
import os

@tool
def find_relevant_test_suites(
  repo_path: str,
  changed_files: list[str],
  registry_path: str = "src/test/test-registry.json"
) -> str:
  """Find test suites that should run for the given changed files in a target repo."""

  # print("DEBUG: find_relevant_test_suites was actually called")
  # print("DEBUG repo_path:", repo_path)
  # print("DEBUG changed_files:", changed_files)
  # print("DEBUG registry_path:", registry_path)

  if not registry_path:
    registry_path = "src/test/test-registry.json"

  if isinstance(changed_files, str):
    changed_files = json.loads(changed_files)

  repo_root = Path(repo_path).resolve()
  registry_file = repo_root / registry_path

  if registry_file.is_dir():
    return json.dumps({
      "ok": False,
      "error": f"Registry path points to a directory, not a file: {registry_file}",
      "suggestion": "Use src/test/test-registry.json as the registry_path."
    }, indent=2)

  if not registry_file.exists():
    return json.dumps({
      "ok": False,
      "error": f"Test registry not found at {registry_file}",
      "matchedSuites": []
    }, indent=2)

  registry = json.loads(registry_file.read_text(encoding="utf-8"))

  matched_suites = []

  for suite in registry.get("suites", []):
    patterns = suite.get("runWhenChanged", [])

    matching_files = [
      file
      for file in changed_files
      if any(fnmatch(file, pattern) for pattern in patterns)
    ]

    if matching_files:
      matched_suites.append({
        "id": suite["id"],
        "name": suite.get("name", suite["id"]),
        "kind": suite.get("kind"),
        "command": suite["command"],
        "matchedFiles": matching_files
      })

  return json.dumps({
  "ok": True,
  "matchedSuiteCount": len(matched_suites),
  "repoPath": str(repo_root),
  "registryPath": str(registry_file),
  "changedFiles": changed_files,
  "matchedSuites": matched_suites
}, indent=2)

@tool
def run_test_suite(
  repo_path: str,
  suite_id: str,
  registry_path: str = "src/test/test-registry.json"
) -> str:
  """Run one registered test suite by suite ID."""

  if not registry_path:
    registry_path = "src/test/test-registry.json"

  repo_root = Path(repo_path).resolve()
  registry_file = repo_root / registry_path

  if not registry_file.exists():
    return json.dumps({
      "ok": False,
      "error": f"Test registry not found at {registry_file}"
    }, indent=2)

  registry = json.loads(registry_file.read_text(encoding="utf-8"))

  suite = next(
    (
      suite
      for suite in registry.get("suites", [])
      if suite.get("id") == suite_id
    ),
    None
  )

  if suite is None:
    return json.dumps({
      "ok": False,
      "error": f"No test suite found with id: {suite_id}"
    }, indent=2)

  command = suite.get("command")

  if not command:
    return json.dumps({
      "ok": False,
      "error": f"Suite {suite_id} does not define a command."
    }, indent=2)
  
  started_at = time.perf_counter()

  # remove vitest pretty print
  env = {
  **os.environ,
  "NO_COLOR": "1",
  "FORCE_COLOR": "0"
  }

  #
  result = subprocess.run(
  command,
  cwd=repo_root,
  shell=True,
  text=True,
  encoding="utf-8",
  errors="replace",
  capture_output=True,
  timeout=120,
  env=env
  )

  duration = time.perf_counter() - started_at

  return json.dumps({
  "ok": result.returncode == 0,
  "suiteId": suite_id,
  "suiteName": suite.get("name", suite_id),
  "command": command,
  "exitCode": result.returncode,
  "durationSeconds": round(duration, 2),
  "stdout": result.stdout,
  "stderr": result.stderr
}, indent=2)