from pathlib import Path
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
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


def truncate_text(value, max_chars: int = 6000) -> str:
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


def resolve_state_path(repo_root: Path, state_path: str) -> Path:
  path = Path(state_path)

  if path.is_absolute():
    resolved = path.resolve()
  else:
    resolved = (repo_root / path).resolve()

  resolved.relative_to(repo_root)
  return resolved


def classify_failure_reason(failure_text: str) -> dict:
  normalized = (failure_text or "").lower()

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
      "guidance": "Add project-standard jsdom browser API shims to the test setup file.",
    }

  if "not implemented: navigation" in normalized and "jsdom" in normalized:
    return {
      "classification": "environment_limit",
      "confidence": "high",
      "repairSafe": True,
      "guidance": "Mock or isolate the browser API that jsdom cannot implement.",
    }

  if "testinglibraryelementerror" in normalized and "found multiple elements" in normalized:
    return {
      "classification": "test_query_bug",
      "confidence": "high",
      "repairSafe": True,
      "guidance": "Replace vague DOM queries with stable user-facing or scoped queries.",
    }

  if any(token in normalized for token in [
    "could not find react-redux context value",
    "usequeryclient",
    "must be used within",
  ]):
    return {
      "classification": "missing_test_setup",
      "confidence": "medium",
      "repairSafe": True,
      "guidance": "Add the project-standard provider or test wrapper.",
    }

  if "cannot find module" in normalized and "src/test/setup" in normalized:
    return {
      "classification": "missing_test_setup_file",
      "confidence": "high",
      "repairSafe": True,
      "guidance": "Create the configured test setup module with project-standard matcher imports.",
    }

  if "test/utils/render" in normalized and (
    "failed to resolve import" in normalized
    or "cannot find module" in normalized
  ):
    return {
      "classification": "missing_test_support_import",
      "confidence": "high",
      "repairSafe": True,
      "guidance": "Create or repair the generated test render helper with project-standard providers.",
    }

  if re.search(r"screen\.get(?:all)?by[a-z0-9_]* is not a function", normalized):
    return {
      "classification": "testing_library_screen_import_bug",
      "confidence": "high",
      "repairSafe": True,
      "guidance": "Repair the test's Testing Library imports so screen comes from @testing-library/react.",
    }

  if (
    "received value must be an htmlelement" in normalized
    and "received has value" in normalized
    and "null" in normalized
  ):
    return {
      "classification": "null_smoke_render",
      "confidence": "medium",
      "repairSafe": True,
      "guidance": (
        "Inspect missing auth, routing, providers, or async waits, then assert a stable "
        "user-visible outcome instead of container.firstChild."
      ),
    }

  if "assertionerror" in normalized or ("expected" in normalized and "received" in normalized):
    return {
      "classification": "assertion_or_product_mismatch",
      "confidence": "low",
      "repairSafe": False,
      "guidance": "Do not automatically rewrite expectations without behavior-change context.",
    }

  return {
    "classification": "unknown",
    "confidence": "low",
    "repairSafe": False,
    "guidance": "Inspect before patching; automatic repair is not safe yet.",
  }


def build_failure_record(run_result: dict, source: str) -> dict:
  failure_reason = run_result.get("failureReason")

  if not failure_reason:
    output_parts = [
      run_result.get("stdout") or "",
      run_result.get("stderr") or "",
      run_result.get("error") or "",
    ]
    failure_reason = "\n".join(part.strip() for part in output_parts if part.strip())

  suite_id = run_result.get("suiteId") or "unknown-suite"
  timestamp = now_epoch_seconds()
  test_files = (
    run_result.get("testFiles")
    or infer_test_files_from_command(run_result.get("command"))
  )

  return {
    "id": f"{suite_id}-{int(timestamp * 1000)}",
    "source": source,
    "createdAt": timestamp,
    "status": "open",
    "suiteId": suite_id,
    "suiteName": run_result.get("suiteName"),
    "command": run_result.get("command"),
    "exitCode": run_result.get("exitCode"),
    "testFiles": test_files,
    "summary": run_result.get("summary"),
    "failureReason": truncate_text(failure_reason),
    "classification": classify_failure_reason(failure_reason or ""),
    "repairAttempts": [],
  }


class TestFailureLogHook(HookProvider):
  """
  Captures failed test runs into a repair queue and append-only history.
  """

  RUN_TOOL_NAMES = {
    "run_test_suite",
    "run_test_files",
  }

  def __init__(
    self,
    failure_queue_path: str = DEFAULT_FAILURE_QUEUE_PATH,
    failure_history_path: str = DEFAULT_FAILURE_HISTORY_PATH,
  ):
    self.failure_queue_path = failure_queue_path
    self.failure_history_path = failure_history_path

  def register_hooks(self, registry: HookRegistry) -> None:
    registry.add_callback(AfterToolCallEvent, self.record_failed_test_run)

  def record_failed_test_run(self, event: AfterToolCallEvent) -> None:
    tool_name = event.tool_use["name"]

    if tool_name not in self.RUN_TOOL_NAMES:
      return

    raw_result = event.result

    try:
      result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except json.JSONDecodeError:
      result = {
        "ok": False,
        "error": "Tool returned non-JSON result.",
      }

    if not isinstance(result, dict) or result.get("ok"):
      return

    repo_path = event.tool_use.get("input", {}).get("repo_path")
    if not repo_path:
      return

    repo_root = Path(repo_path).resolve()
    if not repo_root.exists():
      return

    try:
      queue_path = resolve_state_path(repo_root, self.failure_queue_path)
      history_path = resolve_state_path(repo_root, self.failure_history_path)
    except ValueError:
      return

    queue = load_json_file(queue_path, {
      "version": 1,
      "updatedAt": None,
      "failures": [],
    })
    failure = build_failure_record(result, tool_name)

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
