from strands import tool
from fnmatch import fnmatch
from pathlib import Path
import json
import os
import subprocess
import time
import re

def parse_count_summary_line(stdout: str, label: str) -> dict:
  line_match = re.search(rf"^\s*{re.escape(label)}\s+(?P<body>.+)$", stdout, re.MULTILINE)

  if not line_match:
    return {
      "total": None,
      "passed": None,
      "failed": None,
    }

  body = line_match.group("body")
  total_match = re.search(r"\((\d+)\)", body)
  passed_match = re.search(r"(\d+)\s+passed", body)
  failed_match = re.search(r"(\d+)\s+failed", body)

  if "no tests" in body.lower():
    total = 0
  else:
    total = int(total_match.group(1)) if total_match else None

  return {
    "total": total,
    "passed": int(passed_match.group(1)) if passed_match else 0 if total is not None else None,
    "failed": int(failed_match.group(1)) if failed_match else 0 if total is not None else None,
  }

def parse_vitest_summary(stdout: str) -> dict:
  duration_match = re.search(r"Duration\s+([0-9.]+)s", stdout)

  return {
    "tests": parse_count_summary_line(stdout, "Tests"),
    "testFiles": parse_count_summary_line(stdout, "Test Files"),
    "reportedDurationSeconds": float(duration_match.group(1)) if duration_match else None,
  }

def extract_test_failure_reason(stdout: str, stderr: str, max_chars: int = 3500) -> str | None:
  combined = "\n".join(
    part.strip()
    for part in [stdout, stderr]
    if part and part.strip()
  ).strip()

  if not combined:
    return None

  failure_markers = [
    "Failed Suites",
    "Failed Tests",
    "FAIL ",
    "Error:",
    "AssertionError:",
    "TypeError:",
    "ReferenceError:",
  ]

  start_positions = [
    combined.find(marker)
    for marker in failure_markers
    if combined.find(marker) != -1
  ]
  start = min(start_positions) if start_positions else 0

  summary_markers = [
    "\n Test Files ",
    "\n Tests ",
  ]
  end_positions = [
    combined.find(marker, start)
    for marker in summary_markers
    if combined.find(marker, start) != -1
  ]
  end = min(end_positions) if end_positions else len(combined)

  excerpt = combined[start:end].strip() or combined

  if len(excerpt) > max_chars:
    excerpt = excerpt[:max_chars].rstrip() + "\n...[truncated]"

  return excerpt

def timeout_output_text(value) -> str:
  if value is None:
    return ""

  if isinstance(value, bytes):
    return value.decode("utf-8", errors="replace")

  return str(value)

def timeout_result(
  tool_name: str,
  suite_id: str,
  suite_name: str,
  command: str,
  test_files: list[str],
  started_at: float,
  error: subprocess.TimeoutExpired,
) -> str:
  stdout = timeout_output_text(error.output)
  stderr = timeout_output_text(error.stderr)
  timeout_seconds = error.timeout
  duration = time.perf_counter() - started_at

  return json.dumps({
    "tool": tool_name,
    "status": "Failed",
    "ok": False,
    "suiteId": suite_id,
    "suiteName": suite_name,
    "testFiles": test_files,
    "command": command,
    "exitCode": None,
    "durationSeconds": round(duration, 2),
    "timeoutSeconds": timeout_seconds,
    "summary": parse_vitest_summary(stdout),
    "failureReason": f"Test command timed out after {timeout_seconds} seconds.",
    "stdout": stdout,
    "stderr": stderr,
  }, indent=2)

@tool
def run_all_tests(
  repo_path: str,
) -> str:
  """Run the target repo's full test suite."""

  repo_root = Path(repo_path).resolve()
  command = "npm run test"

  if not repo_root.exists():
    return json.dumps({
      "tool": "run_all_tests",
      "ok": False,
      "suiteId": "all-tests",
      "suiteName": "All tests",
      "command": command,
      "exitCode": None,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  started_at = time.perf_counter()
  env = {
    **os.environ,
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
  }

  try:
    result = subprocess.run(
      command,
      cwd=repo_root,
      shell=True,
      text=True,
      encoding="utf-8",
      errors="replace",
      capture_output=True,
      timeout=300,
      env=env,
    )
  except subprocess.TimeoutExpired as error:
    return timeout_result(
      "run_all_tests",
      "all-tests",
      "All tests",
      command,
      [],
      started_at,
      error,
    )
  except FileNotFoundError as error:
    return json.dumps({
      "tool": "run_all_tests",
      "ok": False,
      "suiteId": "all-tests",
      "suiteName": "All tests",
      "command": command,
      "exitCode": None,
      "error": str(error),
    }, indent=2)

  status = "Passed" if result.returncode == 0 else "Failed"
  duration = time.perf_counter() - started_at
  summary = parse_vitest_summary(result.stdout)

  return json.dumps({
    "tool": "run_all_tests",
    "status": status,
    "ok": result.returncode == 0,
    "suiteId": "all-tests",
    "suiteName": "All tests",
    "testFiles": [],
    "command": command,
    "exitCode": result.returncode,
    "durationSeconds": round(duration, 2),
    "summary": summary,
    "failureReason": extract_test_failure_reason(result.stdout, result.stderr) if result.returncode != 0 else None,
    "stdout": result.stdout,
    "stderr": result.stderr,
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

  env = {
    **os.environ,
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
  }

  try:
    result = subprocess.run(
      command,
      cwd=repo_root,
      shell=True,
      text=True,
      encoding="utf-8",
      errors="replace",
      capture_output=True,
      timeout=120,
      env=env,
    )
  except subprocess.TimeoutExpired as error:
    return timeout_result(
      "run_test_suite",
      suite_id,
      suite.get("name", suite_id),
      command,
      suite.get("files", []),
      started_at,
      error,
    )
  except FileNotFoundError as error:
    return json.dumps({
      "tool": "run_test_suite",
      "ok": False,
      "suiteId": suite_id,
      "suiteName": suite.get("name", suite_id),
      "testFiles": suite.get("files", []),
      "command": command,
      "exitCode": None,
      "error": str(error),
    }, indent=2)

  status = "Passed" if result.returncode == 0 else "Failed"

  duration = time.perf_counter() - started_at

  summary = parse_vitest_summary(result.stdout)

  return json.dumps({
    "tool": "run_test_suite",
    "status": status,
    "ok": result.returncode == 0,
    "suiteId": suite_id,
    "suiteName": suite.get("name", suite_id),
    "testFiles": suite.get("files", []),
    "command": command,
    "exitCode": result.returncode,
    "durationSeconds": round(duration, 2),
    "summary": summary,
    "failureReason": extract_test_failure_reason(result.stdout, result.stderr) if result.returncode != 0 else None,
    "stdout": result.stdout,
    "stderr": result.stderr
  }, indent=2)

@tool
def run_test_files(
  repo_path: str,
  test_files: list[str],
) -> str:
  """Run specific repo-relative test files directly."""

  if isinstance(test_files, str):
    test_files = json.loads(test_files)

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  allowed_test_patterns = [
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "src/test/**",
    "tests/**",
    "__tests__/**",
    "**/__tests__/**",
  ]

  normalized_files = []

  for raw_file in test_files:
    candidate = Path(raw_file)

    if candidate.is_absolute():
      return json.dumps({
        "ok": False,
        "error": "test_files must be repo-relative.",
        "testFile": raw_file,
      }, indent=2)

    test_file = (repo_root / candidate).resolve()

    try:
      relative_posix = test_file.relative_to(repo_root).as_posix()
    except ValueError:
      return json.dumps({
        "ok": False,
        "error": f"test file is outside repo: {test_file}",
        "testFile": raw_file,
      }, indent=2)

    if not any(fnmatch(relative_posix, pattern) for pattern in allowed_test_patterns):
      return json.dumps({
        "ok": False,
        "error": "Only test files may be run directly.",
        "testFile": relative_posix,
      }, indent=2)

    if not test_file.exists():
      return json.dumps({
        "ok": False,
        "error": "test file does not exist.",
        "testFile": relative_posix,
      }, indent=2)

    if relative_posix not in normalized_files:
      normalized_files.append(relative_posix)

  if not normalized_files:
    return json.dumps({
      "ok": False,
      "error": "No test files were provided.",
    }, indent=2)

  started_at = time.perf_counter()
  command = "npm run test -- " + " ".join(normalized_files)
  env = {
    **os.environ,
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
  }

  try:
    result = subprocess.run(
      command,
      cwd=repo_root,
      shell=True,
      text=True,
      encoding="utf-8",
      errors="replace",
      capture_output=True,
      timeout=120,
      env=env,
    )
  except subprocess.TimeoutExpired as error:
    return timeout_result(
      "run_test_files",
      "direct-test-files",
      "Direct test files",
      command,
      normalized_files,
      started_at,
      error,
    )
  except FileNotFoundError as error:
    return json.dumps({
      "tool": "run_test_files",
      "ok": False,
      "suiteId": "direct-test-files",
      "suiteName": "Direct test files",
      "command": command,
      "exitCode": None,
      "error": str(error),
    }, indent=2)

  status = "Passed" if result.returncode == 0 else "Failed"
  duration = time.perf_counter() - started_at
  summary = parse_vitest_summary(result.stdout)

  return json.dumps({
    "tool": "run_test_files",
    "status": status,
    "ok": result.returncode == 0,
    "suiteId": "direct-test-files",
    "suiteName": "Direct test files",
    "testFiles": normalized_files,
    "command": command,
    "exitCode": result.returncode,
    "durationSeconds": round(duration, 2),
    "summary": summary,
    "failureReason": extract_test_failure_reason(result.stdout, result.stderr) if result.returncode != 0 else None,
    "stdout": result.stdout,
    "stderr": result.stderr,
  }, indent=2)
