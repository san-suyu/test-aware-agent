from pathlib import Path
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
import json

class TestCreationAuditHook(HookProvider):
  """
  Records whether the agent wrote/patched tests and whether it later ran a test suite.

  This hook does not block immediately after a write, because the test run usually happens
  in a later tool call. Instead, it writes an audit file that your final summary can check.
  """

  WRITE_TOOL_NAMES = {
    "write_test_file",
    "patch_test_file",
  }

  RUN_TOOL_NAMES = {
    "run_test_suite",
  }

  AUDIT_PATH = Path("last-test-creation-audit.json")

  def register_hooks(self, registry: HookRegistry) -> None:
    registry.add_callback(AfterToolCallEvent, self.record_test_creation_activity)

  def record_test_creation_activity(self, event: AfterToolCallEvent) -> None:
    tool_name = event.tool_use["name"]

    if tool_name not in self.WRITE_TOOL_NAMES and tool_name not in self.RUN_TOOL_NAMES:
      return

    audit = self.load_audit()

    if tool_name in self.WRITE_TOOL_NAMES:
      tool_input = event.tool_use.get("input", {})
      raw_result = event.result

      try:
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
      except json.JSONDecodeError:
        result = {
          "ok": False,
          "error": "Tool returned non-JSON result.",
        }

      audit["testFilesWritten"].append({
        "tool": tool_name,
        "path": tool_input.get("file_path"),
        "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
        "action": result.get("action") if isinstance(result, dict) else None,
      })

      audit["requiresTestRun"] = True

    if tool_name in self.RUN_TOOL_NAMES:
      raw_result = event.result

      try:
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
      except json.JSONDecodeError:
        result = {
          "ok": False,
          "error": "Tool returned non-JSON result.",
        }

      audit["testRuns"].append({
        "tool": tool_name,
        "suiteId": result.get("suiteId") if isinstance(result, dict) else None,
        "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
        "exitCode": result.get("exitCode") if isinstance(result, dict) else None,
        "command": result.get("command") if isinstance(result, dict) else None,
        "failureReason": result.get("failureReason") if isinstance(result, dict) and not result.get("ok") else None,
      })

      audit["requiresTestRun"] = False

    audit["hasWrittenTestsWithoutRun"] = (
      bool(audit["testFilesWritten"])
      and audit["requiresTestRun"]
      and not audit["testRuns"]
    )

    self.AUDIT_PATH.write_text(
      json.dumps(audit, indent=2),
      encoding="utf-8",
    )

  def load_audit(self) -> dict:
    if not self.AUDIT_PATH.exists():
      return self.empty_audit()

    try:
      return json.loads(self.AUDIT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
      return self.empty_audit()

  def empty_audit(self) -> dict:
    return {
      "testFilesWritten": [],
      "testRuns": [],
      "requiresTestRun": False,
      "hasWrittenTestsWithoutRun": False,
    }
