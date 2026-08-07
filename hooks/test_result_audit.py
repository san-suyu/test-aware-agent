from pathlib import Path
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
import json

class TestResultAuditHook(HookProvider):
  """
  Useful for checking what actually happened after a tool call,
  such as whether tests passed, whether stderr had warnings, or
  whether the result has the fields your model is allowed to summarize.
  """

  def register_hooks(self, registry: HookRegistry) -> None:
    registry.add_callback(AfterToolCallEvent, self.audit_test_result)

  def audit_test_result(self, event: AfterToolCallEvent) -> None:
    tool_name = event.tool_use["name"]

    if tool_name != "run_test_suite":
      return

    raw_result = event.result

    try:
      if isinstance(raw_result, str):
          result = json.loads(raw_result)
      else:
          result = raw_result
    except json.JSONDecodeError:
      raise ValueError(
          "run_test_suite returned non-JSON output. "
          "The model should not summarize it as verified test data."
      )

    required_fields = [
      "suiteId",
      "suiteName",
      "command",
      "exitCode",
      "durationSeconds",
      "stdout",
      "stderr",
    ]

    missing_fields = [
      field for field in required_fields
      if field not in result
    ]

    if missing_fields:
      raise ValueError(
        f"run_test_suite result is missing required fields: {missing_fields}"
      )

    passed = result["exitCode"] == 0
    stderr = result.get("stderr", "").strip()

    audit = {
      "tool": tool_name,
      "suiteId": result["suiteId"],
      "passed": passed,
      "exitCode": result["exitCode"],
      "durationSeconds": result["durationSeconds"],
      "hasStderr": bool(stderr),
      "stderrLooksLikeWarningOnly": (
        bool(stderr)
        and "Warning" in stderr
        and "Error" not in stderr
          and result["exitCode"] == 0
      ),
    }

    Path("last-test-audit.json").write_text(
      json.dumps(audit, indent=2),
      encoding="utf-8",
    )
