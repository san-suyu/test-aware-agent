from strands import tool
from pathlib import Path
import json
import re
import time


DEFAULT_FAILURE_QUEUE_PATH = ".test-agent/test-failures.json"
DEFAULT_FAILURE_HISTORY_PATH = ".test-agent/test-failure-history.jsonl"


def now_epoch_seconds() -> float:
  return round(time.time(), 3)


def load_json_file(path: Path, fallback):
  if not path.exists():
    return fallback

  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except json.JSONDecodeError:
    return fallback


def write_json_file(path: Path, data) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(
    json.dumps(data, indent=2) + "\n",
    encoding="utf-8",
  )


def resolve_repo_file(repo_root: Path, file_path: str) -> tuple[Path, str]:
  candidate = Path(file_path)

  if candidate.is_absolute():
    resolved = candidate.resolve()
  else:
    resolved = (repo_root / candidate).resolve()

  relative_path = resolved.relative_to(repo_root)
  return resolved, relative_path.as_posix()


def resolve_state_path(repo_root: Path, state_path: str) -> Path:
  path = Path(state_path)

  if path.is_absolute():
    resolved = path.resolve()
  else:
    resolved = (repo_root / path).resolve()

  resolved.relative_to(repo_root)
  return resolved


def truncate_text(value, max_chars: int = 4000) -> str:
  text = "" if value is None else str(value)

  if len(text) <= max_chars:
    return text

  return text[:max_chars].rstrip() + "\n...[truncated]"


def infer_test_files_from_command(command: str | None) -> list[str]:
  if not command:
    return []

  matches = re.findall(
    r"(?P<path>[A-Za-z0-9_./\\:-]+\.(?:test|spec)\.(?:ts|tsx|js|jsx))",
    command,
  )

  return list(dict.fromkeys(Path(match).as_posix() for match in matches))

def infer_failing_test_files_from_failure_text(failure_text: str | None) -> list[str]:
  if not failure_text:
    return []

  normalized = []
  test_path_pattern = (
    r"(?P<path>(?:[A-Za-z]:[/\\])?[^:\n\r`'\"]*?"
    r"\.(?:test|spec)\.(?:tsx|ts|jsx|js))(?::\d+:\d+)?"
  )

  def add_matches(text: str) -> None:
    for match in re.findall(test_path_pattern, text):
      path = Path(match.strip()).as_posix()
      src_index = path.lower().find("src/")
      if src_index > 0:
        path = path[src_index:]
      if path and path not in normalized:
        normalized.append(path)

  for line in failure_text.splitlines():
    stripped = line.strip()
    lower = stripped.lower()

    if lower.startswith(("stderr |", "stdout |")):
      continue

    is_failure_location = (
      stripped.startswith("FAIL ")
      or re.search(r"\bat\s+(?:[A-Za-z]:[/\\]|.*?src[/\\]).*?\.(?:test|spec)\.", stripped)
      or re.search(r"^[^\w\s]?\s*(?:[A-Za-z]:[/\\]|.*?src[/\\]).*?\.(?:test|spec)\.", stripped)
    )

    if is_failure_location:
      add_matches(stripped)

  if normalized:
    return normalized

  primary_failure_text = re.split(
    r"\n\s*(?:stderr|stdout)\s+\|",
    failure_text,
    maxsplit=1,
  )[0]
  add_matches(primary_failure_text)

  return normalized


def classify_failure_reason(failure_text: str) -> dict:
  text = failure_text or ""
  normalized = text.lower()

  missing_packages = re.findall(
    r"(?:cannot find package|failed to resolve import)\s+['\"](?P<package>[@A-Za-z0-9_./-]+)['\"]",
    text,
    flags=re.IGNORECASE,
  )
  missing_packages = [
    package
    for package in missing_packages
    if not package.startswith(("@/", "./", "../", "/"))
  ]
  if missing_packages:
    packages = list(dict.fromkeys(missing_packages))
    return {
      "classification": "missing_dependency",
      "confidence": "high",
      "repairSafe": False,
      "matched": packages,
      "guidance": (
        "A required package could not be resolved during the test run. "
        f"Refresh or install dependencies for: {', '.join(packages)}. "
        "Do not rewrite tests unless the import is intentionally mocked."
      ),
    }

  if any(token in normalized for token in [
    "window.matchmedia is not a function",
    "matchmedia is not a function",
    "resizeobserver is not defined",
    "intersectionobserver is not defined",
    "window.scrollto is not a function",
  ]):
    return {
      "classification": "missing_jsdom_browser_api",
      "confidence": "high",
      "repairSafe": True,
      "matched": [],
      "guidance": (
        "Add project-standard jsdom browser API shims to the test setup file. "
        "Do not change product expectations."
      ),
    }

  if "test/utils/render" in normalized and (
    "failed to resolve import" in normalized
    or "cannot find module" in normalized
  ):
    return {
      "classification": "missing_test_support_import",
      "confidence": "high",
      "repairSafe": True,
      "matched": [
        "test/utils/render",
      ],
      "guidance": (
        "Create or repair the generated test render helper with the project-standard "
        "providers. Do not change product expectations."
      ),
    }

  if re.search(r"screen\.get(?:all)?by[a-z0-9_]* is not a function", normalized):
    return {
      "classification": "testing_library_screen_import_bug",
      "confidence": "high",
      "repairSafe": True,
      "matched": [],
      "guidance": (
        "Repair the test's Testing Library imports so screen comes from "
        "@testing-library/react. Do not change product expectations."
      ),
    }

  classifications = [
    {
      "classification": "environment_limit",
      "confidence": "high",
      "repairSafe": True,
      "matched": [
        "not implemented: navigation",
        "jsdom",
      ],
      "guidance": (
        "Mock or isolate the browser API that the test environment cannot implement. "
        "Do not change product expectations."
      ),
    },
    {
      "classification": "test_query_bug",
      "confidence": "high",
      "repairSafe": True,
      "matched": [
        "found multiple elements",
        "testinglibraryelementerror",
      ],
      "guidance": (
        "Replace vague DOM queries with stable user-facing queries or scoped queries. "
        "Keep the same behavior expectation."
      ),
    },
    {
      "classification": "missing_test_setup",
      "confidence": "medium",
      "repairSafe": True,
      "matched": [
        "could not find react-redux context value",
        "usequeryclient",
        "usecontext",
        "must be used within",
      ],
      "guidance": (
        "Add the same provider or test wrapper used by nearby tests. "
        "Keep assertions focused on user-visible behavior."
      ),
    },
    {
      "classification": "missing_test_setup_file",
      "confidence": "high",
      "repairSafe": True,
      "matched": [
        "cannot find module",
        "src/test/setup",
      ],
      "guidance": (
        "Create the configured test setup module with the project-standard matcher imports. "
        "Do not change product expectations."
      ),
    },
    {
      "classification": "async_test_bug",
      "confidence": "medium",
      "repairSafe": True,
      "matched": [
        "act(",
        "timed out",
        "waitfor",
      ],
      "guidance": (
        "Use the project-standard async helpers and wait for the observable outcome. "
        "Avoid fixed sleeps."
      ),
    },
    {
      "classification": "null_smoke_render",
      "confidence": "medium",
      "repairSafe": True,
      "matched": [
        "received value must be an htmlelement",
        "received has value",
        "null",
      ],
      "guidance": (
        "A generated smoke assertion rendered nothing. Inspect whether the component "
        "requires auth, routing, providers, or async waits, then assert a stable "
        "user-visible outcome instead of container.firstChild."
      ),
    },
    {
      "classification": "assertion_or_product_mismatch",
      "confidence": "low",
      "repairSafe": False,
      "matched": [
        "assertionerror",
        "expected",
        "received",
      ],
      "guidance": (
        "Do not automatically rewrite expectations. Decide whether the product code is wrong "
        "or the intended behavior changed."
      ),
    },
  ]

  for item in classifications:
    if all(token in normalized for token in item["matched"]):
      return item

  return {
    "classification": "unknown",
    "confidence": "low",
    "repairSafe": False,
    "matched": [],
    "guidance": (
      "Inspect the failure and related test before patching. Automatic repair is not safe yet."
    ),
  }

def extract_absolute_missing_test_setup_path(failure_text: str) -> Path | None:
  if not failure_text:
    return None

  match = re.search(
    r"(?P<path>[A-Za-z]:[^\n'\"`<>]*src[/\\]test[/\\]setup\.(?:ts|tsx|js|jsx))",
    failure_text,
    re.IGNORECASE,
  )
  if not match:
    return None

  return Path(match.group("path")).resolve()

def stale_missing_test_setup_path(repo_root: Path, failure_text: str) -> Path | None:
  absolute_path = extract_absolute_missing_test_setup_path(failure_text)
  if not absolute_path:
    return None

  try:
    absolute_path.relative_to(repo_root.resolve())
    return None
  except ValueError:
    return absolute_path


def build_failure_record(
  run_result: dict,
  source: str = "manual",
  related_test_files: list[str] | None = None,
) -> dict:
  failure_reason = run_result.get("failureReason")

  if not failure_reason:
    output_parts = [
      run_result.get("stdout") or "",
      run_result.get("stderr") or "",
      run_result.get("error") or "",
    ]
    failure_reason = "\n".join(part.strip() for part in output_parts if part.strip())

  classification = classify_failure_reason(failure_reason or "")
  suite_id = run_result.get("suiteId") or "unknown-suite"
  timestamp = now_epoch_seconds()
  failure_id = f"{suite_id}-{int(timestamp * 1000)}"
  inferred_failure_test_files = infer_failing_test_files_from_failure_text(failure_reason)
  test_files = (
    related_test_files
    or run_result.get("testFiles")
    or inferred_failure_test_files
    or infer_test_files_from_command(run_result.get("command"))
  )

  return {
    "id": failure_id,
    "source": source,
    "createdAt": timestamp,
    "status": "open",
    "suiteId": suite_id,
    "suiteName": run_result.get("suiteName"),
    "command": run_result.get("command"),
    "exitCode": run_result.get("exitCode"),
    "testFiles": test_files,
    "summary": run_result.get("summary"),
    "failureReason": truncate_text(failure_reason, 6000),
    "classification": classification,
    "repairAttempts": [],
  }


def load_failure_queue(queue_path: Path) -> dict:
  return load_json_file(queue_path, {
    "version": 1,
    "updatedAt": None,
    "failures": [],
  })


def append_failure_record(repo_root: Path, failure: dict, queue_path: Path, history_path: Path) -> dict:
  queue = load_failure_queue(queue_path)
  queue.setdefault("version", 1)
  queue.setdefault("failures", [])
  queue["updatedAt"] = now_epoch_seconds()
  queue["failures"].append(failure)
  write_json_file(queue_path, queue)

  history_path.parent.mkdir(parents=True, exist_ok=True)
  with history_path.open("a", encoding="utf-8") as history:
    history.write(json.dumps({
      "repoPath": str(repo_root),
      "event": "test_failure_recorded",
      "failure": failure,
    }) + "\n")

  return queue


def record_failed_run_result(
  repo_root: Path,
  run_result: dict,
  source: str = "manual",
  failure_queue_path: str = DEFAULT_FAILURE_QUEUE_PATH,
  failure_history_path: str = DEFAULT_FAILURE_HISTORY_PATH,
) -> dict | None:
  if run_result.get("ok"):
    return None

  queue_path = resolve_state_path(repo_root, failure_queue_path)
  history_path = resolve_state_path(repo_root, failure_history_path)
  failure = build_failure_record(run_result, source=source)
  append_failure_record(repo_root, failure, queue_path, history_path)
  return failure


def find_failure(queue: dict, failure_id: str | None) -> dict | None:
  failures = queue.get("failures", [])

  if failure_id:
    return next((failure for failure in failures if failure.get("id") == failure_id), None)

  open_failures = [
    failure
    for failure in failures
    if failure.get("status") == "open"
  ]

  return open_failures[-1] if open_failures else None


def alternate_test_file_candidates(relative_posix: str) -> list[str]:
  path = Path(relative_posix)
  name = path.name
  candidates = []

  replacements = [
    (".test.ts", ".test.tsx"),
    (".test.tsx", ".test.ts"),
    (".spec.ts", ".spec.tsx"),
    (".spec.tsx", ".spec.ts"),
  ]

  for old, new in replacements:
    if name.endswith(old):
      candidates.append(path.with_name(name.removesuffix(old) + new).as_posix())

  return candidates


@tool
def list_test_failures(
  repo_path: str,
  failure_queue_path: str = DEFAULT_FAILURE_QUEUE_PATH,
) -> str:
  """List open test failures captured for repair."""

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  try:
    queue_path = resolve_state_path(repo_root, failure_queue_path)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": "failure_queue_path must stay inside repo_path.",
    }, indent=2)

  queue = load_failure_queue(queue_path)
  failures = queue.get("failures", [])

  return json.dumps({
    "ok": True,
    "failureQueuePath": str(queue_path),
    "openFailureCount": sum(1 for failure in failures if failure.get("status") == "open"),
    "failures": [
      {
        "id": failure.get("id"),
        "status": failure.get("status"),
        "suiteId": failure.get("suiteId"),
        "command": failure.get("command"),
        "testFiles": failure.get("testFiles", []),
        "classification": failure.get("classification"),
      }
      for failure in failures
    ],
  }, indent=2)


@tool
def inspect_test_failure(
  repo_path: str,
  failure_id: str | None = None,
  failure_queue_path: str = DEFAULT_FAILURE_QUEUE_PATH,
  max_chars_per_test_file: int = 12000,
) -> str:
  """Return one captured failure plus related test file previews for repair planning."""

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  try:
    queue_path = resolve_state_path(repo_root, failure_queue_path)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": "failure_queue_path must stay inside repo_path.",
    }, indent=2)

  queue = load_failure_queue(queue_path)
  failure = find_failure(queue, failure_id)

  if not failure:
    return json.dumps({
      "ok": False,
      "error": "No captured test failure was found.",
      "failureQueuePath": str(queue_path),
    }, indent=2)

  test_files = (
    failure.get("testFiles")
    or infer_test_files_from_command(failure.get("command"))
  )
  if test_files and not failure.get("testFiles"):
    failure["testFiles"] = test_files
    queue["updatedAt"] = now_epoch_seconds()
    write_json_file(queue_path, queue)

  previews = []
  resolved_test_files = []
  for raw_file in test_files:
    try:
      test_file, relative_posix = resolve_repo_file(repo_root, raw_file)
    except ValueError:
      previews.append({
        "path": raw_file,
        "ok": False,
        "error": "test file path is outside repo_path.",
      })
      continue

    if not test_file.exists():
      replacement_path = None
      for alternate in alternate_test_file_candidates(relative_posix):
        alternate_file = (repo_root / alternate).resolve()
        try:
          alternate_relative = alternate_file.relative_to(repo_root).as_posix()
        except ValueError:
          continue
        if alternate_file.exists():
          test_file = alternate_file
          relative_posix = alternate_relative
          replacement_path = alternate_relative
          break

      if not replacement_path:
        previews.append({
          "path": relative_posix,
          "ok": False,
          "error": "test file does not exist.",
        })
        continue

    if relative_posix not in resolved_test_files:
      resolved_test_files.append(relative_posix)

    if not test_file.exists():
      previews.append({
        "path": relative_posix,
        "ok": False,
        "error": "test file does not exist.",
      })
      continue

    content = test_file.read_text(encoding="utf-8", errors="replace")
    previews.append({
      "path": relative_posix,
      "ok": True,
      "contentPreview": truncate_text(content, max_chars_per_test_file),
      "truncated": len(content) > max_chars_per_test_file,
    })

  if resolved_test_files and resolved_test_files != failure.get("testFiles"):
    failure["testFiles"] = resolved_test_files
    queue["updatedAt"] = now_epoch_seconds()
    write_json_file(queue_path, queue)

  return json.dumps({
    "ok": True,
    "failureQueuePath": str(queue_path),
    "failure": failure,
    "testFilePreviews": previews,
    "repairRules": [
      "Only repair test mechanics when classification.repairSafe is true.",
      "Do not delete meaningful assertions to make the run pass.",
      "Do not rewrite product expectations unless the user requested behavior-change test updates.",
      "After patching, rerun the same command or direct test files.",
    ],
  }, indent=2)


@tool
def record_test_repair_attempt(
  repo_path: str,
  failure_id: str,
  status: str,
  repaired_files: list[str] | None = None,
  notes: list[str] | None = None,
  failure_queue_path: str = DEFAULT_FAILURE_QUEUE_PATH,
  failure_history_path: str = DEFAULT_FAILURE_HISTORY_PATH,
) -> str:
  """Record the outcome of a broken-test repair attempt."""

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  if status not in {"repaired", "blocked", "not_test_bug", "attempted"}:
    return json.dumps({
      "ok": False,
      "error": "status must be one of: repaired, blocked, not_test_bug, attempted.",
    }, indent=2)

  try:
    queue_path = resolve_state_path(repo_root, failure_queue_path)
    history_path = resolve_state_path(repo_root, failure_history_path)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": "failure paths must stay inside repo_path.",
    }, indent=2)

  queue = load_failure_queue(queue_path)
  failure = find_failure(queue, failure_id)

  if not failure:
    return json.dumps({
      "ok": False,
      "error": f"No captured test failure found with id: {failure_id}",
    }, indent=2)

  attempt = {
    "createdAt": now_epoch_seconds(),
    "status": status,
    "repairedFiles": repaired_files or [],
    "notes": notes or [],
  }
  failure.setdefault("repairAttempts", []).append(attempt)

  if status in {"repaired", "not_test_bug"}:
    failure["status"] = status
  elif status == "blocked":
    failure["status"] = "blocked"

  queue["updatedAt"] = now_epoch_seconds()
  write_json_file(queue_path, queue)

  history_path.parent.mkdir(parents=True, exist_ok=True)
  with history_path.open("a", encoding="utf-8") as history:
    history.write(json.dumps({
      "repoPath": str(repo_root),
      "event": "test_repair_attempt_recorded",
      "failureId": failure_id,
      "attempt": attempt,
    }) + "\n")

  return json.dumps({
    "ok": True,
    "failureId": failure_id,
    "status": failure["status"],
    "repairAttempt": attempt,
    "failureQueuePath": str(queue_path),
    "failureHistoryPath": str(history_path),
  }, indent=2)


@tool
def clear_test_failures(
  repo_path: str,
  failure_queue_path: str = DEFAULT_FAILURE_QUEUE_PATH,
) -> str:
  """Clear the current failure repair queue without deleting history."""

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  try:
    queue_path = resolve_state_path(repo_root, failure_queue_path)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": "failure_queue_path must stay inside repo_path.",
    }, indent=2)

  write_json_file(queue_path, {
    "version": 1,
    "updatedAt": now_epoch_seconds(),
    "failures": [],
  })

  return json.dumps({
    "ok": True,
    "failureQueuePath": str(queue_path),
    "cleared": True,
  }, indent=2)
