from strands import Agent, tool
from strands.models.ollama import OllamaModel
import contextlib
import io
import json
import re
from fnmatch import fnmatch
from pathlib import Path
import tools, hooks

model = OllamaModel(
  host="http://localhost:11434",  
  model_id="llama3.1",
  temperature=0.7,
  keep_alive="10m",
  stop_sequences=["###", "END"],
  options={"top_k": 40}
)

config = json.loads(Path("config.json").read_text(encoding="utf-8"))

project_name = config["defaultProject"]
project = config["projects"][project_name]

instructions = Path(config["instructionsPath"]).read_text(encoding="utf-8")

#### agents ################

# tool agent - discover & run tests
agent = Agent(
  model=model,
  tools=[
    tools.validate_project_access,
    tools.find_relevant_test_suites, 
    tools.run_all_tests,
    tools.run_test_suite,
    tools.run_test_files,
    tools.inspect_test_context,
    tools.write_test_file,
    tools.write_test_support_file,
    tools.patch_test_file,
    tools.cleanup_tests_for_deleted_sources,
    tools.get_changed_files_from_git_diff,
    tools.get_changed_file_diff,
    tools.update_test_registry,
    tools.list_test_failures,
    tools.inspect_test_failure,
    tools.record_test_repair_attempt,
    tools.clear_test_failures,
    ],
  system_prompt=instructions,
    hooks=[
      hooks.TestWriteGuardHook(),
      hooks.PathValidationHook(),
      hooks.TestResultAuditHook(),
      hooks.TestCreationAuditHook(),
      hooks.TestFailureLogHook(),
    ],
)

# summary agent - format known results
summary_agent = Agent(
  model=model,
  system_prompt="""
You summarize verified test results.
You do not call tools.
You do not suggest tool calls.
You only use data provided in the prompt.
"""
)

test_plan_agent = Agent(
  model=model,
  system_prompt="""
You create JSON plans for writing tests.
You do not call tools.
You do not explain.
You do not summarize context.
You return only valid JSON.

A valid response has this exact shape:
{
  "writes": [
    {
      "action": "create",
      "filePath": "src/example.test.ts",
      "content": "full test file content",
      "overwrite": false
    }
  ],
  "registryUpdates": [
    {
      "suiteId": "profile-settings",
      "testFile": "src/test/components/profile/PrivateNoteCard.test.tsx",
      "relatedSource": ["src/components/profile/PrivateNoteCard.tsx"],
      "runWhenChanged": ["src/components/profile/PrivateNoteCard.tsx"]
    }
  ],
  "suiteIdsToRun": [],
  "skipped": [],
  "notes": []
}

If there is not enough information to create or patch a real test, return:
{
  "writes": [],
  "registryUpdates": [],
  "suiteIdsToRun": [],
  "skipped": ["specific reason"],
  "notes": []
}
"""
)

#################################


config = json.loads(Path("config.json").read_text(encoding="utf-8"))

format_template = Path("templates/results-summary.md").read_text(encoding="utf-8")

project_name = config["defaultProject"]
project = config["projects"][project_name]

repo_path = project["path"]
registry_path = project["registry"]

changed_files = [
  "src/components/community/ChannelMessage.tsx",
  "src/pages/community/ChannelDetailPage.tsx",
]

def markdown_escape(value):
  text = "" if value is None else str(value)
  return text.replace("|", "\\|").replace("\n", " ")

def markdown_list(items):
  if not items:
    return "None"

  return "\n".join(f"- {item}" for item in items)

def extract_failure_reason(result, max_chars=3500):
  failure_reason = result.get("failureReason")
  if failure_reason:
    return truncate_text(failure_reason, max_chars)

  output_parts = []

  stdout = (result.get("stdout") or "").strip()
  stderr = (result.get("stderr") or "").strip()

  if stdout:
    output_parts.append(stdout)

  if stderr:
    output_parts.append(stderr)

  combined = "\n".join(output_parts).strip()
  if not combined:
    error = result.get("error")
    return str(error) if error else "No stdout or stderr was captured for this failed run."

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

  excerpt = combined[start:end].strip()

  if not excerpt:
    excerpt = combined

  if len(excerpt) > max_chars:
    excerpt = excerpt[:max_chars].rstrip() + "\n...[truncated]"

  return excerpt

def format_failure_details(run_results):
  failed_results = [
    result
    for result in run_results
    if not result.get("ok")
  ]

  if not failed_results:
    return "None"

  sections = []

  for result in failed_results:
    suite_label = result.get("suiteName") or result.get("suiteId") or "unknown suite"
    exit_code = result.get("exitCode")
    command = result.get("command") or "unknown command"
    reason = extract_failure_reason(result)
    repair_lines = failure_repair_hint_lines(result)

    sections.append(
      f"### {suite_label}\n"
      f"- Exit code: {exit_code}\n"
      f"- Command: `{command}`\n"
      f"{chr(10).join(repair_lines)}\n\n"
      f"```text\n{reason}\n```"
    )

  return "\n\n".join(sections)

def failure_text_for_result(result):
  parts = [
    result.get("failureReason"),
    result.get("stdout"),
    result.get("stderr"),
    result.get("error"),
  ]

  return "\n".join(str(part).strip() for part in parts if part and str(part).strip())

def failed_test_files_for_result(result):
  inferred_files = tools.infer_failing_test_files_from_failure_text(failure_text_for_result(result))
  if inferred_files:
    return inferred_files

  files = []

  for path in result.get("failureTestFiles") or []:
    if path and path not in files:
      files.append(path)

  for path in result.get("testFiles") or []:
    if path and path not in files:
      files.append(path)

  return files

def failed_test_names_for_result(result):
  text = failure_text_for_result(result)
  names = []

  for line in text.splitlines():
    stripped = line.strip()
    if stripped.lower().startswith(("stderr |", "stdout |")):
      continue

    match = re.search(
      r"(?:^|\s)FAIL\s+[^\n\r]*?\.(?:test|spec)\.(?:tsx|ts|jsx|js)\s+>\s+(?P<name>[^\n\r]+)",
      stripped,
    )
    if not match:
      continue

    name = match.group("name").strip()
    if name and name not in names:
      names.append(name)

  return names

def failure_repair_hint_lines(result):
  lines = []
  failure_id = result.get("failureId")
  test_files = failed_test_files_for_result(result)
  test_names = failed_test_names_for_result(result)

  if failure_id:
    lines.append(f"- Repair ID: `{failure_id}`")
    lines.append(f"- Fix command: `fix-tests {failure_id}`")

  if test_files:
    lines.append(f"- Failed test files: {', '.join(f'`{path}`' for path in test_files)}")
    if not failure_id:
      lines.append(f"- Fix command: `fix-tests {', '.join(test_files)}`")

  if test_names:
    lines.append(f"- Failed tests: {'; '.join(f'`{name}`' for name in test_names[:5])}")

  if not failure_id and not test_files:
    lines.append("- Fix command: run `failures` to see captured repair targets.")

  return lines

def render_test_run_summary(changed_files, matched_suites, run_results, summary_data, extra_notes=None):
  result_by_suite_id = {
    result.get("suiteId"): result
    for result in run_results
  }

  matched_rows = []
  for suite in matched_suites:
    result = result_by_suite_id.get(suite.get("id"), {})
    matched_files = suite.get("matchedFiles", [])
    reason = f"matchedFiles: {json.dumps(matched_files)}" if matched_files else ""
    status = result.get("status") or ("Passed" if result.get("ok") else "Failed")
    duration = result.get("durationSeconds", "")

    matched_rows.append(
      "| "
      + " | ".join([
        markdown_escape(suite.get("id")),
        markdown_escape(suite.get("name", suite.get("id"))),
        markdown_escape(reason),
        markdown_escape(status),
        markdown_escape(duration),
      ])
      + " |"
    )

  suite_results = []
  for result in run_results:
    status = "Passed" if result.get("ok") else "Failed"
    duration = result.get("durationSeconds", "")
    suite_results.append(f"- {result.get('suiteId')}: {status} ({duration}s)")

  test_file_results = []
  for result in run_results:
    summary = result.get("summary", {})
    test_file_summary = summary.get("testFiles", {})
    total = test_file_summary.get("total")
    passed = test_file_summary.get("passed")
    failed = test_file_summary.get("failed")
    if total is None:
      test_file_results.append(f"- {result.get('suiteId')}: file-level result counts unavailable")
    else:
      test_file_results.append(
        f"- {result.get('suiteId')}: {failed or 0} failed, {passed or 0} passed ({total})"
      )

  warnings = []
  for result in run_results:
    stderr = (result.get("stderr") or "").strip()
    if result.get("ok") and stderr:
      warnings.append(f"{result.get('suiteId')}: {truncate_text(stderr, 1000)}")

  commands_run = [
    result.get("command")
    for result in run_results
    if result.get("command")
  ]

  notes = []
  if not matched_suites:
    notes.append("No suites matched the changed files.")
  if any(not result.get("ok") for result in run_results):
    notes.append("Failure details are captured from test runner stdout/stderr.")
  for note in extra_notes or []:
    if note not in notes:
      notes.append(note)

  return f"""# Test Run Summary

## Overall Result
**Status**: {summary_data["overallStatus"]}  
**Registry suites run**: {summary_data["totalSuites"]}  
**Passed registry suites**: {summary_data["passedSuites"]}  
**Failed registry suites**: {summary_data["failedSuites"]}  
**Total test files**: {summary_data["totalTestFiles"]}  
**Passed test files**: {summary_data["passedTestFiles"]}  
**Failed test files**: {summary_data["failedTestFiles"]}  
**Total tests**: {summary_data["totalTests"]}  
**Duration**: {summary_data["durationSeconds"]}  

## Changed Files
{json.dumps(changed_files, indent=2)}

## Matched Registry Suites
| Suite ID | Name | Reason | Status | Duration |
|---|---|---|---|---|
{chr(10).join(matched_rows) if matched_rows else "| None | None | None | None | None |"}

## Registry Suite Results
{markdown_list(suite_results)}

## Test File Results
{markdown_list(test_file_results)}

## Failures
{format_failure_details(run_results)}

## Warnings
{markdown_list(warnings)}

## Commands Run
{markdown_list(commands_run)}

## Notes
{markdown_list(notes)}
"""

def test_file_count_for_result(result):
  summary_total = result.get("summary", {}).get("testFiles", {}).get("total")
  if summary_total is not None:
    return summary_total

  return len(result.get("testFiles", []))

def run_for_changed_files(changed_files):
  matched_json = tools.find_relevant_test_suites(
    repo_path=repo_path,
    changed_files=changed_files,
    registry_path=registry_path,
  )

  matched_data = json.loads(matched_json)
  matched_suites = matched_data.get("matchedSuites", [])
  notes = []

  if not matched_data.get("ok") and matched_data.get("error"):
    notes.append(matched_data.get("error"))

  run_results = []

  for suite in matched_suites:
    run_json = tools.run_test_suite(
      repo_path=repo_path,
      suite_id=suite["id"],
      registry_path=registry_path,
    )

    result = json.loads(run_json)
    run_results.append(result)
    record_failure_for_repair(result, source="simulate")

  if not run_results:
    context_json = tools.inspect_test_context(
      repo_path=repo_path,
      changed_files=changed_files,
      registry_path=registry_path,
    )
    context_data = json.loads(context_json)
    direct_test_files = existing_valid_candidate_test_files(context_data) if context_data.get("ok") else []

    if direct_test_files:
      result_json = tools.run_test_files(
        repo_path=repo_path,
        test_files=direct_test_files,
      )
      result = json.loads(result_json)
      run_results.append(result)
      record_test_run_activity(result)
      notes.append(
        "No registered suite was available, so simulate ran existing candidate test files directly."
      )

  summary_data = {
    "overallStatus": (
      "Passed"
      if run_results and all(r["ok"] for r in run_results)
      else "Failed"
      if run_results and any(not r["ok"] for r in run_results)
      else "No matched suites"
    ),
    "totalSuites": len(run_results),
    "passedSuites": sum(1 for r in run_results if r["ok"]),
    "failedSuites": sum(1 for r in run_results if not r["ok"]),
    "totalTestFiles": sum(test_file_count_for_result(r) for r in run_results),
    "passedTestFiles": sum((r.get("summary", {}).get("testFiles", {}).get("passed") or 0) for r in run_results),
    "failedTestFiles": sum((r.get("summary", {}).get("testFiles", {}).get("failed") or 0) for r in run_results),
    "totalTests": sum((r.get("summary", {}).get("tests", {}).get("total") or 0) for r in run_results),
    "durationSeconds": round(sum(r.get("durationSeconds", 0) for r in run_results), 2),
  }

  return render_test_run_summary(changed_files, matched_suites, run_results, summary_data, notes)

def run_all_tests_command():
  result = json.loads(tools.run_all_tests(repo_path=repo_path))
  record_test_run_activity(result)

  summary = result.get("summary", {})
  test_file_summary = summary.get("testFiles", {})
  test_summary = summary.get("tests", {})
  status = "Passed" if result.get("ok") else "Failed"

  return f"""# Test Run Summary

## Overall Result
**Status**: {status}  
**Total test files**: {test_file_summary.get("total", "Unknown")}  
**Passed test files**: {test_file_summary.get("passed", "Unknown")}  
**Failed test files**: {test_file_summary.get("failed", "Unknown")}  
**Total tests**: {test_summary.get("total", "Unknown")}  
**Passed tests**: {test_summary.get("passed", "Unknown")}  
**Failed tests**: {test_summary.get("failed", "Unknown")}  
**Duration**: {result.get("durationSeconds", "Unknown")}s

## Failures
{format_failure_details([result])}

## Warnings
{markdown_list([result.get("stderr", "").strip()] if result.get("ok") and result.get("stderr", "").strip() else [])}

## Commands Run
{markdown_list([result.get("command")] if result.get("command") else [])}
"""

def existing_valid_candidate_test_files(planner_context):
  invalid_paths = {
    item.get("path")
    for item in planner_context.get("invalidCandidateTests", [])
    if item.get("path")
  }

  existing_paths = []

  for preview in planner_context.get("candidateTestPreviews", []):
    path = preview.get("path")

    if not preview.get("exists") or not path or path in invalid_paths:
      continue

    if path not in existing_paths:
      existing_paths.append(path)

  return existing_paths

def existing_test_previews_for_edit(planner_context):
  previews = []
  seen_paths = set()

  for group_name in [
    "candidateTestPreviews",
    "matchedSuiteTestPreviews",
    "nearbyTestPreviews",
  ]:
    for preview in planner_context.get(group_name, []):
      path = preview.get("path")

      if not preview.get("exists") or not path or path in seen_paths:
        continue

      previews.append(preview)
      seen_paths.add(path)

  return previews

def editable_test_file_paths(planner_context):
  return [
    preview.get("path")
    for preview in existing_test_previews_for_edit(planner_context)
    if preview.get("path")
  ]

def infer_registry_suite_id(test_file, planner_context):
  normalized_test_file = Path(test_file).as_posix()
  changed_files = [
    Path(path).as_posix()
    for path in planner_context.get("changedFiles", [])
  ]
  source_dirs = {
    str(Path(path).parent).replace("\\", "/")
    for path in changed_files
  }

  best_suite = None
  best_score = 0

  for suite in planner_context.get("allSuites", []):
    score = 0
    suite_files = [
      Path(path).as_posix()
      for path in suite.get("files", [])
    ]
    test_locations = [
      Path(path).as_posix().rstrip("/")
      for path in suite.get("testLocations", [])
    ]
    run_patterns = [
      Path(pattern).as_posix()
      for pattern in suite.get("runWhenChanged", [])
    ]
    command = suite.get("command") or ""

    if normalized_test_file in suite_files:
      score += 100

    if any(normalized_test_file.startswith(location + "/") or normalized_test_file == location for location in test_locations):
      score += 40

    test_parent = str(Path(normalized_test_file).parent).replace("\\", "/")
    if test_parent and test_parent in command:
      score += 25

    if normalized_test_file in command:
      score += 20

    if any(any(fnmatch(path, pattern) for pattern in run_patterns) for path in changed_files):
      score += 15

    if any(source_dir and source_dir.replace("src/", "src/test/") in normalized_test_file for source_dir in source_dirs):
      score += 10

    if score > best_score:
      best_suite = suite
      best_score = score

  if best_suite and best_score >= 10:
    return best_suite.get("id")

  return None

def bootstrap_registry_suite_id(planner_context):
  if planner_context.get("allSuites"):
    return None

  return "generated-tests"

def run_existing_tests_for_create_request(changed_files, planner_context):
  matched_suites = planner_context.get("matchedSuites", [])
  existing_tests = existing_valid_candidate_test_files(planner_context)
  registry_results = []
  run_results = []

  for suite in matched_suites:
    suite_id = suite.get("id")
    if not suite_id:
      continue

    result_json = tools.run_test_suite(
      repo_path=repo_path,
      suite_id=suite_id,
      registry_path=registry_path,
    )
    result = json.loads(result_json)
    run_results.append(result)
    record_test_run_activity(result)

  if not run_results and existing_tests:
    suite_id = infer_registry_suite_id(existing_tests[0], planner_context)

    if suite_id:
      for test_file in existing_tests:
        result_json = tools.update_test_registry(
          repo_path=repo_path,
          registry_path=registry_path,
          suite_id=suite_id,
          test_file=test_file,
          related_source=planner_context.get("changedFiles", []),
          run_when_changed=planner_context.get("changedFiles", []),
        )
        registry_results.append(json.loads(result_json))

      if registry_results and all(result.get("ok") for result in registry_results):
        result_json = tools.run_test_suite(
          repo_path=repo_path,
          suite_id=suite_id,
          registry_path=registry_path,
        )
        result = json.loads(result_json)
        run_results.append(result)
        record_test_run_activity(result)

  if not run_results and existing_tests:
    result_json = tools.run_test_files(
      repo_path=repo_path,
      test_files=existing_tests,
    )
    result = json.loads(result_json)
    run_results.append(result)
    record_test_run_activity(result)

  commands_run = [
    result.get("command")
    for result in run_results
    if result.get("command")
  ]
  registry_updates = [
    f"{result.get('suiteId')}: {result.get('testFile')}"
    for result in registry_results
    if result.get("ok")
  ]
  failed_registry_updates = [
    result
    for result in registry_results
    if not result.get("ok")
  ]

  notes = [
    "Existing valid candidate tests were found, so create-tests ran the matching registered suites without creating or patching tests."
  ]
  skipped = []

  if existing_tests:
    notes.append("Existing tests found: " + ", ".join(existing_tests))

  if registry_updates:
    notes.append("The existing tests were added to the registry so future simulate runs can match the changed files.")
  elif existing_tests and not matched_suites:
    notes.append("No registered suites matched the changed files, so the existing tests were run directly.")

  return f"""
Tests created:
- None

Tests changed:
- None

Registry updates:
{chr(10).join(f"- {item}" for item in registry_updates) if registry_updates else "- None"}

Commands run:
{chr(10).join(f"- {command}" for command in commands_run) if commands_run else "- None"}

Skipped:
{chr(10).join(f"- {item}" for item in skipped) if skipped else "- None"}

Failed writes:
- None

Failed registry updates:
{chr(10).join(f"- {json.dumps(item)}" for item in failed_registry_updates) if failed_registry_updates else "- None"}

Test run failures:
{format_failure_details(run_results)}

Notes:
{chr(10).join(f"- {item}" for item in notes)}

Audit:
{json.dumps(load_test_creation_audit(), indent=2)}
"""

TEST_CREATION_AUDIT_PATH = Path("last-test-creation-audit.json")

def empty_test_creation_audit():
  return {
    "testFilesWritten": [],
    "testRuns": [],
    "requiresTestRun": False,
    "hasWrittenTestsWithoutRun": False,
  }

def reset_test_creation_audit():
  TEST_CREATION_AUDIT_PATH.write_text(
    json.dumps(empty_test_creation_audit(), indent=2),
    encoding="utf-8",
  )

def load_test_creation_audit():
  if not TEST_CREATION_AUDIT_PATH.exists():
    return empty_test_creation_audit()

  try:
    return json.loads(TEST_CREATION_AUDIT_PATH.read_text(encoding="utf-8"))
  except json.JSONDecodeError:
    return empty_test_creation_audit()

def save_test_creation_audit(audit):
  TEST_CREATION_AUDIT_PATH.write_text(
    json.dumps(audit, indent=2),
    encoding="utf-8",
  )

def record_failure_for_repair(result, source="manual"):
  if result.get("ok"):
    return None

  try:
    failure = tools.record_failed_run_result(
      repo_root=Path(repo_path).resolve(),
      run_result=result,
      source=source,
    )
    if failure:
      result["failureId"] = failure.get("id")
      result["failureTestFiles"] = failure.get("testFiles", [])
      result["failureClassification"] = failure.get("classification")
    return failure
  except Exception:
    return None

def record_test_write_activity(tool_name, file_path, result):
  audit = load_test_creation_audit()
  write_ok = bool(result.get("ok"))
  audit["testFilesWritten"].append({
    "tool": tool_name,
    "path": file_path,
    "ok": write_ok,
    "action": result.get("action"),
  })
  if write_ok:
    audit["requiresTestRun"] = True
  audit["hasWrittenTestsWithoutRun"] = (
    any(item.get("ok") for item in audit["testFilesWritten"])
    and audit["requiresTestRun"]
    and not audit["testRuns"]
  )
  save_test_creation_audit(audit)

def record_test_run_activity(result):
  audit = load_test_creation_audit()
  run_record = {
    "tool": result.get("tool", "run_test_suite"),
    "suiteId": result.get("suiteId"),
    "ok": bool(result.get("ok")),
    "exitCode": result.get("exitCode"),
    "command": result.get("command"),
  }
  if not result.get("ok"):
    run_record["failureReason"] = extract_failure_reason(result, max_chars=1200)

  audit["testRuns"].append(run_record)
  audit["requiresTestRun"] = False
  audit["hasWrittenTestsWithoutRun"] = False
  save_test_creation_audit(audit)
  record_failure_for_repair(result, source=run_record["tool"])

def truncate_text(value, max_chars=4000):
  if value is None:
    return None

  text = str(value)
  if len(text) <= max_chars:
    return text

  return text[:max_chars] + "\n...[truncated]"

def build_compact_planner_context(diff_json, context_json):
  diff_data = json.loads(diff_json)
  context_data = json.loads(context_json)

  compact_diffs = []

  for item in diff_data.get("diffs", []):
    diff_text = item.get("diff", "") or ""
    added_lines = []
    removed_lines = []

    for line in diff_text.splitlines():
      if line.startswith("+++") or line.startswith("---"):
        continue

      if line.startswith("+"):
        added_lines.append(line)
      elif line.startswith("-"):
        removed_lines.append(line)

    compact_diffs.append({
      "path": item.get("path"),
      "exists": item.get("exists", True),
      "ok": item.get("ok", item.get("exists", True)),
      "isUntracked": item.get("isUntracked", False),
      "addedLines": added_lines[:120],
      "removedLines": removed_lines[:80],
      "contentPreview": truncate_text(item.get("contentPreview"), 4000),
      "notes": item.get("notes", []),
    })

  nearby_previews = []

  for preview in context_data.get("nearbyTestPreviews", [])[:3]:
    nearby_previews.append({
      "path": preview.get("path"),
      "exists": preview.get("exists"),
      "contentPreview": truncate_text(preview.get("contentPreview"), 5000),
    })

  matched_suite_test_previews = []

  for preview in context_data.get("matchedSuiteTestPreviews", [])[:6]:
    matched_suite_test_previews.append({
      "path": preview.get("path"),
      "exists": preview.get("exists"),
      "contentPreview": truncate_text(preview.get("contentPreview"), 5000),
    })

  changed_previews = []
  test_intent_by_file = {}

  for preview in context_data.get("changedFilePreviews", []):
    if preview.get("exists"):
      source_path = preview.get("path")
      source_text = preview.get("contentPreview") or ""
      changed_previews.append({
        "path": source_path,
        "contentPreview": truncate_text(preview.get("contentPreview"), 5000),
      })
      if source_path:
        test_intent_by_file[source_path] = tools.source_test_intent(source_path, source_text)

  candidate_previews = []

  for preview in context_data.get("candidateTestPreviews", [])[:6]:
    candidate_previews.append({
      "path": preview.get("path"),
      "exists": preview.get("exists"),
      "contentPreview": truncate_text(preview.get("contentPreview"), 5000),
    })

  return {
    "changedFiles": context_data.get("changedFiles", []),
    "diffs": compact_diffs,
    "matchedSuites": context_data.get("matchedSuites", []),
    "allSuites": context_data.get("allSuites", []),
    "candidateTestFiles": context_data.get("candidateTestFiles", [])[:12],
    "candidateTestPreviews": candidate_previews,
    "invalidCandidateTests": context_data.get("invalidCandidateTests", [])[:6],
    "unregisteredCandidateTests": context_data.get("unregisteredCandidateTests", [])[:6],
    "nearbyTestsByChangedFile": context_data.get("nearbyTestsByChangedFile", []),
    "nearbyTestPreviews": nearby_previews,
    "matchedSuiteTestPreviews": matched_suite_test_previews,
    "changedFilePreviews": changed_previews,
    "testIntentByFile": test_intent_by_file,
    "existingHelpers": context_data.get("existingHelpers", [])[:10],
  }
  
def parse_test_write_plan(raw_plan):
  raw_plan = str(raw_plan).strip()

  try:
    return json.loads(raw_plan)
  except json.JSONDecodeError:
    pass

  start = raw_plan.find("{")
  end = raw_plan.rfind("}")

  if start != -1 and end != -1 and end > start:
    try:
      return json.loads(raw_plan[start:end + 1])
    except json.JSONDecodeError:
      pass

  repair_prompt = f"""
Convert the following response into valid JSON only.

Return exactly this JSON shape:
{{
  "writes": [],
  "registryUpdates": [],
  "suiteIdsToRun": [],
  "skipped": [],
  "notes": []
}}

Rules:
- Do not use markdown.
- Do not explain.
- If the response did not contain concrete test file writes, return writes as [].
- Put the reason in skipped.

Response to convert:
{raw_plan}
"""

  repaired = str(test_plan_agent(repair_prompt)).strip()

  try:
    return json.loads(repaired)
  except json.JSONDecodeError:
    start = repaired.find("{")
    end = repaired.rfind("}")

    if start != -1 and end != -1 and end > start:
      return json.loads(repaired[start:end + 1])

  raise ValueError("Agent did not return valid JSON for the test-write plan.")

def list_value(value):
  if value is None or value is False:
    return []

  if value is True:
    return []

  if isinstance(value, list):
    return value

  return [value]

def normalize_test_write_plan(plan):
  if not isinstance(plan, dict):
    return {
      "writes": [],
      "registryUpdates": [],
      "suiteIdsToRun": [],
      "skipped": ["Planner did not return a JSON object."],
      "notes": [],
    }

  normalized = {
    "writes": list_value(plan.get("writes")),
    "registryUpdates": list_value(plan.get("registryUpdates")),
    "suiteIdsToRun": list_value(plan.get("suiteIdsToRun")),
    "skipped": list_value(plan.get("skipped")),
    "notes": list_value(plan.get("notes")),
  }

  if plan.get("reason") and plan.get("reason") not in normalized["skipped"]:
    normalized["skipped"].append(plan.get("reason"))

  normalized["writes"] = [
    write
    for write in normalized["writes"]
    if isinstance(write, dict)
  ]

  for write in normalized["writes"]:
    action = str(write.get("action") or "").strip().lower()

    if action in {"overwrite", "replace", "rewrite"}:
      write["action"] = "create"
      write["overwrite"] = True
    elif action in {"update", "edit"}:
      if write.get("findText") or write.get("replaceText"):
        write["action"] = "patch"
      else:
        write["action"] = "create"
        write["overwrite"] = True
    elif action:
      write["action"] = action

    if "file_path" in write and "filePath" not in write:
      write["filePath"] = write.get("file_path")
    if "find_text" in write and "findText" not in write:
      write["findText"] = write.get("find_text")
    if "replace_text" in write and "replaceText" not in write:
      write["replaceText"] = write.get("replace_text")

  normalized["registryUpdates"] = [
    update
    for update in normalized["registryUpdates"]
    if isinstance(update, dict)
  ]
  normalized["suiteIdsToRun"] = [
    suite_id
    for suite_id in normalized["suiteIdsToRun"]
    if isinstance(suite_id, str) and suite_id
  ]
  normalized["skipped"] = [
    str(item)
    for item in normalized["skipped"]
    if item
  ]
  normalized["notes"] = [
    str(item)
    for item in normalized["notes"]
    if item
  ]

  return normalized

def source_import_path(source_path):
  if source_path.startswith("src/"):
    source_path = source_path.removeprefix("src/")

  return "@/" + str(Path(source_path).with_suffix("")).replace("\\", "/")

def has_named_export(source_text, export_name):
  escaped_name = re.escape(export_name)
  patterns = [
    rf"export\s+(?:async\s+)?function\s+{escaped_name}\b",
    rf"export\s+(?:const|let|var|class)\s+{escaped_name}\b",
    rf"export\s*{{[^}}]*\b{escaped_name}\b[^}}]*}}",
  ]

  return any(re.search(pattern, source_text) for pattern in patterns)

def default_export_name(source_text):
  default_function_match = re.search(
    r"export\s+default\s+(?:async\s+)?function\s+([A-Z][A-Za-z0-9_]*)",
    source_text,
  )
  if default_function_match:
    return default_function_match.group(1)

  default_class_match = re.search(r"export\s+default\s+class\s+([A-Z][A-Za-z0-9_]*)", source_text)
  if default_class_match:
    return default_class_match.group(1)

  default_identifier_match = re.search(r"export\s+default\s+([A-Z][A-Za-z0-9_]*)\b", source_text)
  if default_identifier_match:
    return default_identifier_match.group(1)

  return None

def has_default_export(source_text):
  return bool(re.search(r"\bexport\s+default\b", source_text))

def component_name_from_source(source_path, source_text):
  function_match = re.search(r"export\s+function\s+([A-Z][A-Za-z0-9_]*)", source_text)
  if function_match:
    return function_match.group(1)

  const_match = re.search(r"export\s+const\s+([A-Z][A-Za-z0-9_]*)", source_text)
  if const_match:
    return const_match.group(1)

  default_function_match = re.search(r"export\s+default\s+function\s+([A-Z][A-Za-z0-9_]*)", source_text)
  if default_function_match:
    return default_function_match.group(1)

  default_name = default_export_name(source_text)
  if default_name:
    return default_name

  return Path(source_path).stem

def import_statement_from_source(source_path, source_text, component_name):
  import_path = source_import_path(source_path)

  if has_default_export(source_text) and not has_named_export(source_text, component_name):
    return f"import {component_name} from '{import_path}';"

  return f"import {{ {component_name} }} from '{import_path}';"

def source_import_aliases(source_path):
  aliases = [source_import_path(source_path)]

  if source_path.startswith("src/"):
    src_alias = "@/" + str(Path(source_path).with_suffix("")).replace("\\", "/")
    if src_alias not in aliases:
      aliases.append(src_alias)

  return aliases

def normalize_generated_test_imports(content, planner_context):
  normalized = content

  for preview in planner_context.get("changedFilePreviews", []):
    source_path = preview.get("path")
    source_text = preview.get("contentPreview") or ""

    if not source_path:
      continue

    component_name = component_name_from_source(source_path, source_text)
    if not (has_default_export(source_text) and not has_named_export(source_text, component_name)):
      continue

    for alias in source_import_aliases(source_path):
      pattern = (
        rf"import\s*{{\s*{re.escape(component_name)}\s*}}\s*"
        rf"from\s*(['\"]){re.escape(alias)}\1\s*;"
      )
      replacement = f"import {component_name} from '{source_import_path(source_path)}';"
      normalized = re.sub(pattern, replacement, normalized)

  return normalized

def build_private_note_card_test_content(source_path, source_text, component_name):
  component_import = import_statement_from_source(source_path, source_text, component_name)

  return f"""import {{ beforeEach, describe, expect, it, vi }} from 'vitest';
import {{ screen }} from '@testing-library/react';
{component_import}
import {{ renderWithAppProviders }} from '@/test/utils/render';
import {{
  useDeleteProfileNote,
  useProfileNote,
  useSaveProfileNote,
}} from '@/hooks/useProfileNotes';

vi.mock('@/hooks/useProfileNotes', () => ({{
  useProfileNote: vi.fn(),
  useSaveProfileNote: vi.fn(),
  useDeleteProfileNote: vi.fn(),
}}));

vi.mock('@/hooks/use-toast', () => ({{
  useToast: () => ({{
    toast: vi.fn(),
  }}),
}}));

describe('{component_name}', () => {{
  beforeEach(() => {{
    vi.mocked(useProfileNote).mockReturnValue({{
      data: null,
      isLoading: false,
    }} as ReturnType<typeof useProfileNote>);

    vi.mocked(useSaveProfileNote).mockReturnValue({{
      mutateAsync: vi.fn(),
      isPending: false,
    }} as unknown as ReturnType<typeof useSaveProfileNote>);

    vi.mocked(useDeleteProfileNote).mockReturnValue({{
      mutateAsync: vi.fn(),
      isPending: false,
    }} as unknown as ReturnType<typeof useDeleteProfileNote>);
  }});

  it('renders an empty private note editor for a member', () => {{
    renderWithAppProviders(
      <{component_name} targetProfileId="profile-123" targetName="Maya Founder" />,
    );

    expect(screen.getByText('Private Note')).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText('Add private notes about Maya Founder... (only you can see this)'),
    ).toBeInTheDocument();
  }});

  it('shows a loading state while the private note is loading', () => {{
    vi.mocked(useProfileNote).mockReturnValue({{
      data: null,
      isLoading: true,
    }} as ReturnType<typeof useProfileNote>);

    renderWithAppProviders(
      <{component_name} targetProfileId="profile-123" targetName="Maya Founder" />,
    );

    expect(screen.getByText('Loading...')).toBeInTheDocument();
  }});

  it('renders an existing private note and follow-up tags', () => {{
    vi.mocked(useProfileNote).mockReturnValue({{
      data: {{
        id: 'note-123',
        note_text: 'Follow up after demo day.',
        follow_up_tags: ['investor', 'warm intro'],
        synced_at: null,
      }},
      isLoading: false,
    }} as ReturnType<typeof useProfileNote>);

    renderWithAppProviders(
      <{component_name} targetProfileId="profile-123" targetName="Maya Founder" />,
    );

    expect(screen.getByText('Follow up after demo day.')).toBeInTheDocument();
    expect(screen.getByText('investor')).toBeInTheDocument();
    expect(screen.getByText('warm intro')).toBeInTheDocument();
  }});
}});
"""

def build_org_constants_test_content(source_path):
  import_path = source_import_path(source_path)

  return f"""import {{ describe, expect, it }} from 'vitest';
import {{
  FREQUENCY_OPTIONS,
  LOCATION_OPTIONS,
  STEPS,
  normalizePhone,
  normalizeUrl,
  validateStep,
  type OrgFormData,
}} from '{import_path}';

function buildForm(overrides: Partial<OrgFormData> = {{}}): OrgFormData {{
  return {{
    name: 'Austin Founder Circle',
    website: 'https://fiesta.community',
    contact_name: 'Maya Founder',
    contact_email: 'maya@example.com',
    contact_phone: '512-970-0412',
    linkedin_url: '',
    instagram_url: '',
    tiktok_url: '',
    twitter_url: '',
    facebook_url: '',
    brief_overview: 'A local founder community.',
    programming: ['Networking'],
    fees: ['Free'],
    membership_fee: '',
    frequency: 'Monthly',
    meeting_format: ['Hybrid'],
    typical_day_time: 'Second Tuesday evening',
    meeting_location: 'Austin',
    locations_served: 'Austin',
    demographics_served: ['Founders'],
    primary_focus: ['Startup community'],
    founder_leadership: 'Maya Founder',
    year_founded: '2024',
    best_way_to_join: 'Apply online',
    anything_else: '',
    include_in_public_lists: true,
    contact_for_collaborations: true,
    ...overrides,
  }};
}}

describe('org constants and form helpers', () => {{
  it('keeps public option lists and step labels available for the organizer form', () => {{
    expect(FREQUENCY_OPTIONS).toContain('Monthly');
    expect(LOCATION_OPTIONS).toContain('Austin');
    expect(STEPS.map((step) => step.label)).toEqual(['Basics', 'Focus', 'Contact', 'Details']);
  }});

  it('normalizes phone numbers to local digits and strips a leading US country code', () => {{
    expect(normalizePhone('+1 (512) 970-0412')).toBe('5129700412');
    expect(normalizePhone('512.970.0412')).toBe('5129700412');
    expect(normalizePhone(null)).toBe('');
  }});

  it('normalizes optional URLs without changing existing protocols', () => {{
    expect(normalizeUrl(' fiesta.community/join ')).toBe('https://fiesta.community/join');
    expect(normalizeUrl('http://fiesta.community')).toBe('http://fiesta.community');
    expect(normalizeUrl('   ')).toBeNull();
  }});

  it('returns the first missing required field for each organizer form step', () => {{
    expect(validateStep(buildForm({{ name: ' ' }}), 0)).toBe('Organizer name is required.');
    expect(validateStep(buildForm({{ primary_focus: [] }}), 1)).toBe('Select at least one primary area of focus.');
    expect(validateStep(buildForm({{ contact_email: ' ' }}), 2)).toBe('Contact email is required.');
    expect(validateStep(buildForm({{ meeting_format: [] }}), 3)).toBe('Select at least one meeting format.');
    expect(validateStep(buildForm(), 3)).toBeNull();
  }});
}});
"""

def build_member_recommendations_test_content(source_path):
  import_path = source_import_path(source_path)

  return f"""import {{ describe, expect, it }} from 'vitest';
import {{ getRecommendedMembers }} from '{import_path}';

function member(overrides: Record<string, unknown>) {{
  return {{
    user_id: 'member-1',
    name: 'Maya Founder',
    role: 'founder',
    company: 'Climate Labs',
    title: 'Founder',
    member_identity: 'Founder',
    startup_industry: 'Climate',
    looking_for: ['funding introductions'],
    is_verified: false,
    is_mentor: false,
    engagement_score: 0,
    ...overrides,
  }} as any;
}}

describe('getRecommendedMembers', () => {{
  it('filters out the current user and profiles without real names', () => {{
    const result = getRecommendedMembers(
      [
        member({{ user_id: 'current-user', name: 'Current Member' }}),
        member({{ user_id: 'unknown-user', name: 'Unknown' }}),
        member({{ user_id: 'peer-user', name: 'Maya Founder' }}),
      ],
      'current-user',
      {{ goals: ['meet founders'] }},
    );

    expect(result.map((profile) => profile.name)).toEqual(['Maya Founder']);
  }});

  it('ranks mentor and industry matches ahead of weaker matches', () => {{
    const result = getRecommendedMembers(
      [
        member({{
          user_id: 'mentor-user',
          name: 'Ava Mentor',
          role: 'mentor',
          is_mentor: true,
          is_verified: true,
          startup_industry: 'Climate tech',
          looking_for: ['funding introductions'],
        }}),
        member({{
          user_id: 'founder-user',
          name: 'Zara Founder',
          role: 'founder',
          startup_industry: 'Retail',
          looking_for: ['community events'],
        }}),
      ],
      'current-user',
      {{
        identity: 'founder',
        industry: 'Climate / sustainability',
        goals: ['mentor', 'funding'],
        today: ['investor introductions'],
      }},
    );

    expect(result[0].name).toBe('Ava Mentor');
    expect(result[0].matchBadges).toContain('Mentor fit');
    expect(result[0].matchReason).toBe('You asked for mentor or operator help.');
    expect(result[0].matchScore).toBeGreaterThan(result[1].matchScore);
  }});
}});
"""

def build_admin_permissions_test_content(source_path):
  import_path = source_import_path(source_path)

  return f"""import {{ describe, expect, it }} from 'vitest';
import {{
  ADMIN_PERMISSION_DEFINITIONS,
  ALL_ADMIN_PERMISSIONS,
  ASSIGNABLE_ADMIN_PERMISSIONS,
  isOwnerAdminIdentity,
  normalizePhoneDigits,
}} from '{import_path}';

describe('admin permissions', () => {{
  it('derives assignable permissions by excluding owner-only permissions', () => {{
    expect(ALL_ADMIN_PERMISSIONS).toContain('admin_role_management');
    expect(ALL_ADMIN_PERMISSIONS).toContain('admin_error_monitor');
    expect(ASSIGNABLE_ADMIN_PERMISSIONS.map((definition) => definition.permission)).not.toContain('admin_role_management');
    expect(ASSIGNABLE_ADMIN_PERMISSIONS.map((definition) => definition.permission)).not.toContain('admin_error_monitor');
    expect(ASSIGNABLE_ADMIN_PERMISSIONS.length).toBeLessThan(ADMIN_PERMISSION_DEFINITIONS.length);
  }});

  it('normalizes profile phone digits for owner checks', () => {{
    expect(normalizePhoneDigits('+1 (512) 970-0412')).toBe('15129700412');
    expect(normalizePhoneDigits(null)).toBe('');
  }});

  it('recognizes owner admins by email or stored phone digits', () => {{
    expect(isOwnerAdminIdentity({{ email: ' JON@EVENTMAGIC.CO ', phone: null }}, null)).toBe(true);
    expect(isOwnerAdminIdentity({{ email: null, phone: '(512) 970-0412' }}, null)).toBe(true);
    expect(isOwnerAdminIdentity(null, {{ email: 'cherie@fiesta.community' }} as any)).toBe(true);
    expect(isOwnerAdminIdentity({{ email: 'member@example.com', phone: '555-0100' }}, null)).toBe(false);
  }});
}});
"""

def build_errors_test_content(source_path):
  import_path = source_import_path(source_path)

  return f"""import {{ describe, expect, it }} from 'vitest';
import {{ getErrorMessage }} from '{import_path}';

describe('getErrorMessage', () => {{
  it('returns messages from Error instances, strings, and error-like objects', () => {{
    expect(getErrorMessage(new Error('Network failed'))).toBe('Network failed');
    expect(getErrorMessage('Plain failure')).toBe('Plain failure');
    expect(getErrorMessage({{ message: 'Database rejected the request' }})).toBe('Database rejected the request');
  }});

  it('falls back when the error has no usable message', () => {{
    expect(getErrorMessage(new Error(''), 'Try again')).toBe('Try again');
    expect(getErrorMessage({{ message: '' }}, 'Try again')).toBe('Try again');
    expect(getErrorMessage(null, 'Try again')).toBe('Try again');
  }});
}});
"""

def build_resource_logo_upload_test_content(source_path):
  import_path = source_import_path(source_path)

  return f"""import {{ beforeEach, describe, expect, it, vi }} from 'vitest';

const storageFrom = vi.hoisted(() => vi.fn());

vi.mock('@/integrations/supabase/client', () => ({{
  supabase: {{
    storage: {{
      from: storageFrom,
    }},
  }},
}}));

import {{ uploadMarketplaceLogo, uploadResourceLogo, validateResourceLogo }} from '{import_path}';

describe('resource logo uploads', () => {{
  beforeEach(() => {{
    storageFrom.mockReset();
    vi.restoreAllMocks();
  }});

  it('accepts supported logo types by MIME type or filename extension', () => {{
    expect(validateResourceLogo(new File(['logo'], 'logo.webp', {{ type: 'image/webp' }}))).toBeNull();
    expect(validateResourceLogo(new File(['logo'], 'logo.SVG', {{ type: '' }}))).toBeNull();
  }});

  it('rejects unsupported logo types and oversized files', () => {{
    expect(validateResourceLogo(new File(['text'], 'logo.txt', {{ type: 'text/plain' }}))).toBe(
      'Please upload a PNG, JPG, WebP, GIF, SVG, AVIF, HEIC, or ICO logo.',
    );

    const oversized = new File([new Uint8Array(5 * 1024 * 1024 + 1)], 'logo.png', {{ type: 'image/png' }});
    expect(validateResourceLogo(oversized)).toBe('Logo must be under 5MB.');
  }});

  it('uploads resource logos with a safe user-scoped path and public URL', async () => {{
    vi.spyOn(Date, 'now').mockReturnValue(12345);
    const upload = vi.fn().mockResolvedValue({{ error: null }});
    const getPublicUrl = vi.fn().mockReturnValue({{ data: {{ publicUrl: 'https://cdn.test/resource.svg' }} }});
    storageFrom.mockReturnValue({{ upload, getPublicUrl }});
    const file = new File(['<svg />'], 'Original.SVG', {{ type: '' }});

    await expect(uploadResourceLogo(file, 'user-1', 'My Resource!')).resolves.toBe('https://cdn.test/resource.svg');

    expect(storageFrom).toHaveBeenCalledWith('resource-logos');
    expect(upload).toHaveBeenCalledWith(
      'user-1/my-resource-12345.svg',
      file,
      {{ contentType: 'image/svg+xml', upsert: true }},
    );
    expect(getPublicUrl).toHaveBeenCalledWith('user-1/my-resource-12345.svg');
  }});

  it('uploads marketplace logos without upserting and falls back to a safe listing name', async () => {{
    vi.spyOn(Date, 'now').mockReturnValue(98765);
    const upload = vi.fn().mockResolvedValue({{ error: null }});
    const getPublicUrl = vi.fn().mockReturnValue({{ data: {{ publicUrl: 'https://cdn.test/marketplace.png' }} }});
    storageFrom.mockReturnValue({{ upload, getPublicUrl }});
    const file = new File(['logo'], 'logo.png', {{ type: 'image/png' }});

    await expect(uploadMarketplaceLogo(file, 'user-2', '!!!')).resolves.toBe('https://cdn.test/marketplace.png');

    expect(upload).toHaveBeenCalledWith(
      'marketplace/user-2/marketplace-listing-98765.png',
      file,
      {{ contentType: 'image/png', upsert: false }},
    );
  }});
}});
"""

def build_session_refresh_test_content(source_path):
  import_path = source_import_path(source_path)

  return f"""import {{ beforeEach, describe, expect, it, vi }} from 'vitest';

const refreshSession = vi.hoisted(() => vi.fn());

vi.mock('@/integrations/supabase/client', () => ({{
  supabase: {{
    auth: {{
      refreshSession,
    }},
  }},
}}));

import {{ refreshSessionOnce }} from '{import_path}';

describe('refreshSessionOnce', () => {{
  beforeEach(() => {{
    refreshSession.mockReset();
  }});

  it('shares one in-flight refresh across concurrent callers', async () => {{
    let resolveRefresh: (value: {{ data: {{ session: unknown }}; error: null }}) => void = () => {{}};
    refreshSession.mockReturnValue(new Promise((resolve) => {{
      resolveRefresh = resolve;
    }}));

    const first = refreshSessionOnce();
    const second = refreshSessionOnce();
    resolveRefresh({{ data: {{ session: {{ access_token: 'token' }} }}, error: null }});

    await expect(first).resolves.toEqual({{ session: {{ access_token: 'token' }}, error: null }});
    await expect(second).resolves.toEqual({{ session: {{ access_token: 'token' }}, error: null }});
    expect(refreshSession).toHaveBeenCalledTimes(1);
  }});

  it('allows a new refresh after the previous one settles', async () => {{
    refreshSession
      .mockResolvedValueOnce({{ data: {{ session: {{ access_token: 'first' }} }}, error: null }})
      .mockResolvedValueOnce({{ data: {{ session: {{ access_token: 'second' }} }}, error: null }});

    await expect(refreshSessionOnce()).resolves.toEqual({{ session: {{ access_token: 'first' }}, error: null }});
    await expect(refreshSessionOnce()).resolves.toEqual({{ session: {{ access_token: 'second' }}, error: null }});

    expect(refreshSession).toHaveBeenCalledTimes(2);
  }});

  it('normalizes missing session and error values to null', async () => {{
    refreshSession.mockResolvedValue({{ data: {{}}, error: undefined }});

    await expect(refreshSessionOnce()).resolves.toEqual({{ session: null, error: null }});
  }});
}});
"""

def build_reputation_hook_test_content(source_path):
  import_path = source_import_path(source_path)

  return f"""import React, {{ type ReactNode }} from 'react';
import {{ QueryClient, QueryClientProvider }} from '@tanstack/react-query';
import {{ renderHook, waitFor }} from '@testing-library/react';
import {{ beforeEach, describe, expect, it, vi }} from 'vitest';

const rpc = vi.hoisted(() => vi.fn());

vi.mock('@/integrations/supabase/client', () => ({{
  supabase: {{
    rpc,
  }},
}}));

import {{ useReputation, type ReputationStats }} from '{import_path}';

function createWrapper() {{
  const queryClient = new QueryClient({{
    defaultOptions: {{ queries: {{ retry: false }} }},
  }});

  return function Wrapper({{ children }}: {{ children: ReactNode }}) {{
    return React.createElement(QueryClientProvider, {{ client: queryClient }}, children);
  }};
}}

function stats(overrides: Partial<ReputationStats> = {{}}): ReputationStats {{
  return {{
    completed_intros: 0,
    asks_created: 0,
    asks_closed: 0,
    offers_created: 0,
    offers_fulfilled: 0,
    sessions_hosted: 0,
    questions_answered: 0,
    responses_given: 0,
    ...overrides,
  }};
}}

describe('useReputation', () => {{
  beforeEach(() => {{
    rpc.mockReset();
  }});

  it('does not fetch reputation stats until a user id is available', () => {{
    const {{ result }} = renderHook(() => useReputation(undefined), {{
      wrapper: createWrapper(),
    }});

    expect(result.current.data).toBeUndefined();
    expect(rpc).not.toHaveBeenCalled();
  }});

  it('returns score, level, badges, and unlocked badges from reputation stats', async () => {{
    rpc.mockResolvedValue({{
      data: stats({{
        completed_intros: 10,
        asks_created: 1,
        asks_closed: 5,
        offers_created: 2,
        offers_fulfilled: 3,
        sessions_hosted: 2,
        questions_answered: 5,
      }}),
      error: null,
    }});

    const {{ result }} = renderHook(() => useReputation('user-1'), {{
      wrapper: createWrapper(),
    }});

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(rpc).toHaveBeenCalledWith('get_reputation_stats', {{ target_user_id: 'user-1' }});
    expect(result.current.data?.score).toBe(265);
    expect(result.current.data?.level).toEqual({{ level: 'Contributor', minScore: 150, maxScore: 300 }});
    expect(result.current.data?.badges.map((badge) => badge.id)).toEqual([
      'connector',
      'helper',
      'host',
      'mentor',
      'asker',
    ]);
    expect(result.current.data?.unlockedBadges.map((badge) => badge.id)).toEqual([
      'connector',
      'helper',
      'host',
      'mentor',
      'asker',
    ]);
  }});

  it('surfaces Supabase RPC failures through the query error state', async () => {{
    const error = new Error('permission denied');
    rpc.mockResolvedValue({{ data: null, error }});
    vi.spyOn(console, 'error').mockImplementation(() => {{}});

    const {{ result }} = renderHook(() => useReputation('user-1'), {{
      wrapper: createWrapper(),
    }});

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBe(error);
  }});
}});
"""

def build_incomplete_pitch_application_hook_test_content(source_path):
  import_path = source_import_path(source_path)

  return f"""import React, {{ type ReactNode }} from 'react';
import {{ QueryClient, QueryClientProvider }} from '@tanstack/react-query';
import {{ renderHook, waitFor }} from '@testing-library/react';
import {{ beforeEach, describe, expect, it, vi }} from 'vitest';

const from = vi.hoisted(() => vi.fn());

vi.mock('@/integrations/supabase/client', () => ({{
  supabase: {{
    from,
  }},
}}));

import {{ INCOMPLETE_PITCH_QUERY_KEY, useIncompletePitchApplication }} from '{import_path}';

function createWrapper() {{
  const queryClient = new QueryClient({{
    defaultOptions: {{ queries: {{ retry: false }} }},
  }});

  return function Wrapper({{ children }}: {{ children: ReactNode }}) {{
    return React.createElement(QueryClientProvider, {{ client: queryClient }}, children);
  }};
}}

function mockDraftQuery(result: {{ data: unknown; error: unknown }}) {{
  const query = {{
    select: vi.fn(() => query),
    eq: vi.fn(() => query),
    maybeSingle: vi.fn().mockResolvedValue(result),
  }};
  from.mockReturnValue(query);
  return query;
}}

describe('useIncompletePitchApplication', () => {{
  beforeEach(() => {{
    from.mockReset();
  }});

  it('does not query drafts until a user id is available', () => {{
    const {{ result }} = renderHook(() => useIncompletePitchApplication(undefined), {{
      wrapper: createWrapper(),
    }});

    expect(result.current.data).toBeUndefined();
    expect(result.current.fetchStatus).toBe('idle');
    expect(from).not.toHaveBeenCalled();
  }});

  it('loads the current in-progress pitch draft for the user', async () => {{
    const draft = {{
      id: 'draft-1',
      current_step: 2,
      updated_at: '2026-08-07T12:00:00.000Z',
      reminder_snoozed_until: null,
    }};
    const query = mockDraftQuery({{ data: draft, error: null }});

    const {{ result }} = renderHook(() => useIncompletePitchApplication('user-1'), {{
      wrapper: createWrapper(),
    }});

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(from).toHaveBeenCalledWith('pitch_application_drafts');
    expect(query.select).toHaveBeenCalledWith('id,current_step,status,updated_at,reminder_snoozed_until');
    expect(query.eq).toHaveBeenNthCalledWith(1, 'user_id', 'user-1');
    expect(query.eq).toHaveBeenNthCalledWith(2, 'status', 'in_progress');
    expect(result.current.data).toEqual(draft);
  }});

  it('surfaces draft query failures through the query error state', async () => {{
    const error = new Error('database unavailable');
    mockDraftQuery({{ data: null, error }});

    const {{ result }} = renderHook(() => useIncompletePitchApplication('user-1'), {{
      wrapper: createWrapper(),
    }});

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBe(error);
  }});

  it('exports a stable query key prefix for cache invalidation', () => {{
    expect(INCOMPLETE_PITCH_QUERY_KEY).toBe('my-incomplete-pitch-application');
  }});
}});
"""

def build_deterministic_utility_test_content(source_path, source_text):
  normalized_path = Path(source_path).as_posix()

  if normalized_path.endswith("src/lib/resourceLogoUpload.ts"):
    return build_resource_logo_upload_test_content(source_path)

  if normalized_path.endswith("src/lib/sessionRefresh.ts"):
    return build_session_refresh_test_content(source_path)

  if normalized_path.endswith("src/hooks/useReputation.ts"):
    return build_reputation_hook_test_content(source_path)

  if normalized_path.endswith("src/hooks/useIncompletePitchApplication.ts"):
    return build_incomplete_pitch_application_hook_test_content(source_path)

  if (
    "normalizePhone" in source_text
    and "validateStep" in source_text
    and "normalizeUrl" in source_text
  ):
    return build_org_constants_test_content(source_path)

  if "getRecommendedMembers" in source_text:
    return build_member_recommendations_test_content(source_path)

  if "isOwnerAdminIdentity" in source_text and "ASSIGNABLE_ADMIN_PERMISSIONS" in source_text:
    return build_admin_permissions_test_content(source_path)

  if "getErrorMessage" in source_text:
    return build_errors_test_content(source_path)

  return None

def deterministic_test_write_plan(planner_context):
  source_previews = [
    preview
    for preview in planner_context.get("changedFilePreviews", [])
    if preview.get("path")
  ]

  if not source_previews:
    return {
      "writes": [],
      "registryUpdates": [],
      "suiteIdsToRun": [],
      "skipped": ["No changed source file preview was available for deterministic test generation."],
      "notes": [],
    }

  suite_id = None
  if planner_context.get("matchedSuites"):
    suite_id = planner_context["matchedSuites"][0].get("id")

  plan = {
    "writes": [],
    "registryUpdates": [],
    "suiteIdsToRun": [suite_id] if suite_id else [],
    "skipped": [],
    "notes": ["Used deterministic fallback because the planner skipped or returned an unusable write plan."],
  }
  used_paths = set()

  for source_preview in source_previews:
    source_path = source_preview.get("path")
    source_text = source_preview.get("contentPreview") or ""
    component_name = component_name_from_source(source_path, source_text)
    intent = planner_context.get("testIntentByFile", {}).get(source_path) or tools.source_test_intent(source_path, source_text)
    candidate_paths = candidate_paths_for_source(source_path, planner_context)

    invalid_path = next(
      (
        item.get("path")
        for item in planner_context.get("invalidCandidateTests", [])
        if item.get("path") in candidate_paths
      ),
      None,
    )

    if not invalid_path and source_has_valid_candidate_test(source_path, planner_context):
      continue

    candidate_path = invalid_path
    if not candidate_path:
      missing_preview = next(
        (
          preview
          for preview in planner_context.get("candidateTestPreviews", [])
          if preview.get("path") in candidate_paths and not preview.get("exists")
        ),
        None,
      )
      if missing_preview:
        candidate_path = missing_preview.get("path")

    if not candidate_path and candidate_paths:
      candidate_path = candidate_paths[0]

    if not candidate_path or candidate_path in used_paths:
      continue

    if not intent.get("shouldTest"):
      plan["skipped"].append(
        f"{source_path}: skipped because no meaningful behavior target was detected ({'; '.join(intent.get('reasons', [])[:2])})."
      )
      continue

    if component_name == "PrivateNoteCard":
      content = build_private_note_card_test_content(source_path, source_text, component_name)
    else:
      content = build_deterministic_utility_test_content(source_path, source_text)
      if not content:
        plan["skipped"].append(
          f"{source_path}: deterministic fallback found intent ({'; '.join(intent.get('suggestedAssertions', [])[:2])}) but cannot safely infer the required mocks, props, or expected values."
        )
        continue

    plan["writes"].append({
      "action": "create",
      "filePath": candidate_path,
      "content": content,
      "overwrite": bool(invalid_path),
    })
    used_paths.add(candidate_path)

    if suite_id:
      plan["registryUpdates"].append({
        "suiteId": suite_id,
        "testFile": candidate_path,
        "relatedSource": [source_path],
        "runWhenChanged": [source_path],
      })

  if planner_context.get("unregisteredCandidateTests") and suite_id:
    registered_update_paths = {
      update.get("testFile")
      for update in plan["registryUpdates"]
      if update.get("testFile")
    }
    for test_file in planner_context["unregisteredCandidateTests"]:
      if test_file in registered_update_paths:
        continue

      source_path = source_path_for_test_file(test_file, planner_context)
      plan["registryUpdates"].append({
        "suiteId": suite_id,
        "testFile": test_file,
        "relatedSource": [source_path] if source_path else planner_context.get("changedFiles", []),
        "runWhenChanged": [source_path] if source_path else planner_context.get("changedFiles", []),
      })
      registered_update_paths.add(test_file)

  if not plan["writes"] and not plan["registryUpdates"] and not plan["skipped"]:
    plan["skipped"] = ["No candidate test file path was available."]

  return plan

def candidate_paths_for_source(source_path, planner_context):
  source_path = normalize_repo_path(source_path)
  source = Path(source_path)
  suffix = ".test.tsx" if source.suffix in [".tsx", ".jsx"] else ".test.ts"
  spec_suffix = ".spec.tsx" if source.suffix in [".tsx", ".jsx"] else ".spec.ts"
  preferred = []

  if len(source.parts) > 2 and source.parts[0] == "src":
    preferred.append(Path("src/test").joinpath(*source.parts[1:-1], f"{source.stem}{suffix}").as_posix())
    preferred.append(Path("src/test").joinpath(*source.parts[1:-1], f"{source.stem}{spec_suffix}").as_posix())

  if len(source.parts) > 1:
    preferred.append((Path("src/test") / source.parent.name / f"{source.stem}{suffix}").as_posix())
    preferred.append((Path("src/test") / source.parent.name / f"{source.stem}{spec_suffix}").as_posix())

  preferred.extend(candidate_tests_for_source_path(source_path))
  preferred = list(dict.fromkeys(preferred))

  known_candidates = [
    normalize_repo_path(path)
    for path in planner_context.get("candidateTestFiles", [])
  ]
  matching_known = [
    path
    for path in preferred
    if path in known_candidates
  ]

  if matching_known:
    return matching_known

  if not known_candidates:
    return preferred

  return [
    path
    for path in known_candidates
    if Path(path).stem == source.stem
  ]

def source_path_for_test_file(test_file, planner_context):
  normalized_test_file = normalize_repo_path(test_file)

  for source_path in planner_context.get("changedFiles", []):
    if normalized_test_file in candidate_paths_for_source(source_path, planner_context):
      return source_path

  return None

def source_has_valid_candidate_test(source_path, planner_context):
  candidate_paths = set(candidate_paths_for_source(source_path, planner_context))
  invalid_paths = {
    item.get("path")
    for item in planner_context.get("invalidCandidateTests", [])
    if item.get("path")
  }

  return any(
    preview.get("path") in candidate_paths
    and preview.get("exists")
    and preview.get("path") not in invalid_paths
    for preview in planner_context.get("candidateTestPreviews", [])
  )

def create_request_has_actionable_work(planner_context):
  if planner_context.get("invalidCandidateTests") or planner_context.get("unregisteredCandidateTests"):
    return True

  return any(
    not source_has_valid_candidate_test(source_path, planner_context)
    and (planner_context.get("testIntentByFile", {}).get(source_path) or {}).get("shouldTest", True)
    for source_path in planner_context.get("changedFiles", [])
  )

def complete_create_plan_for_requested_sources(plan, planner_context):
  if not plan_is_usable(plan, planner_context):
    return plan

  fallback_plan = deterministic_test_write_plan(planner_context)
  planned_paths = {
    write.get("filePath")
    for write in plan.get("writes", [])
    if write.get("filePath")
  }
  planned_sources = {
    source_path_for_test_file(path, planner_context)
    for path in planned_paths
  }
  planned_sources.discard(None)
  added_write_paths = set()

  for fallback_write in fallback_plan.get("writes", []):
    fallback_path = fallback_write.get("filePath")
    fallback_source = source_path_for_test_file(fallback_path, planner_context)

    if not fallback_path or fallback_path in planned_paths or fallback_source in planned_sources:
      continue

    plan.setdefault("writes", []).append(fallback_write)
    planned_paths.add(fallback_path)
    if fallback_source:
      planned_sources.add(fallback_source)
    added_write_paths.add(fallback_path)

  registry_paths = {
    update.get("testFile")
    for update in plan.get("registryUpdates", [])
    if update.get("testFile")
  }

  for fallback_update in fallback_plan.get("registryUpdates", []):
    test_file = fallback_update.get("testFile")
    if test_file and test_file not in registry_paths and (
      test_file in added_write_paths
      or test_file in planner_context.get("unregisteredCandidateTests", [])
    ):
      plan.setdefault("registryUpdates", []).append(fallback_update)
      registry_paths.add(test_file)

  if added_write_paths:
    for note in fallback_plan.get("notes", []):
      if note not in plan.setdefault("notes", []):
        plan["notes"].append(note)

  return plan

def plan_is_usable(plan, planner_context):
  candidate_paths = set(planner_context.get("candidateTestFiles", []))

  for write in plan.get("writes", []):
    if write.get("action") not in ("create", "patch"):
      continue

    file_path = write.get("filePath")
    if candidate_paths and file_path not in candidate_paths:
      return False

    if write.get("action") == "create":
      content_errors = tools.validate_test_file_content(write.get("content", ""))
      if content_errors:
        return False

    return True

  return bool(plan.get("registryUpdates") and planner_context.get("unregisteredCandidateTests"))

def edit_plan_is_usable(plan, planner_context):
  editable_paths = set(editable_test_file_paths(planner_context))
  candidate_paths = set(planner_context.get("candidateTestFiles", []))

  for write in plan.get("writes", []):
    action = write.get("action")
    file_path = write.get("filePath")

    if action == "patch":
      if not editable_paths or file_path not in editable_paths:
        return False
      if not write.get("findText"):
        return False
      if write.get("replaceText") is None:
        return False
      return True

    if action == "create":
      if not candidate_paths or file_path not in candidate_paths:
        return False
      if tools.validate_test_file_content(write.get("content", "")):
        return False
      return True

  return bool(plan.get("registryUpdates") and planner_context.get("unregisteredCandidateTests"))

def edit_tests_for_changed_files(changed_files):
  reset_test_creation_audit()

  diff_json = tools.get_changed_file_diff(
    repo_path=repo_path,
    changed_files=changed_files,
  )

  context_json = tools.inspect_test_context(
    repo_path=repo_path,
    changed_files=changed_files,
    registry_path=registry_path,
  )

  planner_context = build_compact_planner_context(diff_json, context_json)
  existing_previews = existing_test_previews_for_edit(planner_context)
  existing_paths = [
    preview.get("path")
    for preview in existing_previews
    if preview.get("path")
  ]

  prompt_context = dict(planner_context)
  prompt_context["existingRelevantTestPreviews"] = existing_previews

  plan_prompt = f"""
Create a concrete JSON plan to edit test cases for an updated existing feature.

Return only valid JSON. Do not use markdown. Do not explain.

JSON shape:
{{
  "writes": [
    {{
      "action": "patch",
      "filePath": "src/example.test.tsx",
      "findText": "exact existing text",
      "replaceText": "exact replacement text"
    }}
  ],
  "registryUpdates": [],
  "suiteIdsToRun": [],
  "skipped": [],
  "notes": []
}}

Rules:
- Use the git diff to identify intentionally updated behavior.
- Use testIntentByFile to identify the behavior that deserves coverage.
- Prefer patching an existing relevant test from existingRelevantTestPreviews.
- Only patch test files that appear in existingRelevantTestPreviews.
- Use exact findText and replaceText for patch actions.
- Keep meaningful existing assertions unless the feature behavior intentionally changed.
- Add assertions for new behavior when the existing test file already covers the feature area.
- Only create a new focused test file when no existing relevant test can cover the updated behavior.
- Create actions must target one of candidateTestFiles and include full valid Vitest test content.
- Import source files with the project's @/ alias without an extra src segment; use @/pages/Foo, not @/src/pages/Foo.
- If the source uses export default and does not separately export the component by name, use a default import in the test.
- If you create a new test file, include a registryUpdates entry for it.
- Include suiteIdsToRun for the suites that should verify the edited tests.
- If no safe test edit can be made, return writes as [] and explain the specific missing technical information in skipped.
- Do not patch product source files.
- Do not weaken assertions just to make tests pass.
- Do not add render-only smoke tests, container.firstChild assertions, hook-as-JSX tests, or lowercase JSX pseudo-component tests.
- Hooks must use renderHook; utilities must be called directly; components must assert visible state, interactions, callbacks, or navigation.

Planner context:
{json.dumps(prompt_context, indent=2)}
"""

  plan = normalize_test_write_plan(get_test_write_plan(plan_prompt))

  if not edit_plan_is_usable(plan, planner_context):
    plan["writes"] = []
    if not plan.get("skipped"):
      if existing_paths:
        plan["skipped"] = ["No safe exact patch was produced for the existing related tests."]
      else:
        plan["skipped"] = ["No existing related tests were found to edit."]

  Path("output.md").write_text(
    json.dumps(plan, indent=2),
    encoding="utf-8",
  )

  write_results = apply_repair_plan(plan, planner_context) if plan.get("writes") else []
  registry_updates_to_apply = list(plan.get("registryUpdates", []))

  default_suite_id = None
  if planner_context.get("matchedSuites"):
    default_suite_id = planner_context["matchedSuites"][0].get("id")

  def has_registry_update_for(test_file):
    return any(
      update.get("testFile") == test_file
      for update in registry_updates_to_apply
    )

  if default_suite_id:
    for result in write_results:
      test_file = result.get("path")
      if result.get("ok") and result.get("action") == "created" and test_file and not has_registry_update_for(test_file):
        registry_updates_to_apply.append({
          "suiteId": default_suite_id,
          "testFile": test_file,
          "relatedSource": planner_context.get("changedFiles", []),
          "runWhenChanged": planner_context.get("changedFiles", []),
        })

    for test_file in planner_context.get("unregisteredCandidateTests", []):
      if test_file in existing_paths and not has_registry_update_for(test_file):
        registry_updates_to_apply.append({
          "suiteId": default_suite_id,
          "testFile": test_file,
          "relatedSource": planner_context.get("changedFiles", []),
          "runWhenChanged": planner_context.get("changedFiles", []),
        })

  if not default_suite_id:
    bootstrap_suite_id = bootstrap_registry_suite_id(planner_context)
    for result in write_results:
      test_file = result.get("path")
      inferred_suite_id = infer_registry_suite_id(test_file, planner_context) if result.get("ok") and test_file else None
      suite_id = inferred_suite_id or bootstrap_suite_id

      if result.get("action") == "created" and suite_id and not has_registry_update_for(test_file):
        registry_updates_to_apply.append({
          "suiteId": suite_id,
          "testFile": test_file,
          "relatedSource": planner_context.get("changedFiles", []),
          "runWhenChanged": planner_context.get("changedFiles", []),
        })

  registry_results = []
  for update in registry_updates_to_apply:
    result_json = tools.update_test_registry(
      repo_path=repo_path,
      registry_path=registry_path,
      suite_id=update.get("suiteId", ""),
      test_file=update.get("testFile", ""),
      related_source=update.get("relatedSource", []),
      run_when_changed=update.get("runWhenChanged", []),
    )
    registry_results.append(json.loads(result_json))

  run_results = []
  can_run_tests = (
    not any(not result.get("ok") for result in write_results)
    and not any(not result.get("ok") for result in registry_results)
  )

  suite_ids_to_run = []
  if can_run_tests:
    suite_ids_to_run = list(dict.fromkeys(
      list(plan.get("suiteIdsToRun", []))
      + [
        result.get("suiteId")
        for result in registry_results
        if result.get("ok") and result.get("suiteId")
      ]
      + [
        suite.get("id")
        for suite in planner_context.get("matchedSuites", [])
        if suite.get("id")
      ]
    ))

  for suite_id in suite_ids_to_run:
    result_json = tools.run_test_suite(
      repo_path=repo_path,
      suite_id=suite_id,
      registry_path=registry_path,
    )
    result = json.loads(result_json)
    run_results.append(result)
    record_test_run_activity(result)

  written_test_files = [
    result.get("path")
    for result in write_results
    if result.get("ok") and result.get("path")
  ]

  if can_run_tests and written_test_files and not run_results:
    result_json = tools.run_test_files(
      repo_path=repo_path,
      test_files=list(dict.fromkeys(written_test_files)),
    )
    result = json.loads(result_json)
    run_results.append(result)
    record_test_run_activity(result)
    plan.setdefault("notes", []).append(
      "No registered suite was available for the edited tests, so the changed test files were run directly."
    )

  if can_run_tests and not written_test_files and existing_paths and not run_results:
    result_json = tools.run_test_files(
      repo_path=repo_path,
      test_files=list(dict.fromkeys(existing_paths)),
    )
    result = json.loads(result_json)
    run_results.append(result)
    record_test_run_activity(result)
    plan.setdefault("notes", []).append(
      "No tests were edited, so the existing relevant test files were run directly."
    )

  audit = load_test_creation_audit()
  created = [
    result["path"]
    for result in write_results
    if result.get("ok") and result.get("action") == "created"
  ]
  changed = [
    result["path"]
    for result in write_results
    if result.get("ok") and result.get("action") in ("updated", "patched")
  ]
  failed_writes = [
    result
    for result in write_results
    if not result.get("ok")
  ]
  registry_updates = [
    f"{result.get('suiteId')}: {result.get('testFile')}"
    for result in registry_results
    if result.get("ok")
  ]
  failed_registry_updates = [
    result
    for result in registry_results
    if not result.get("ok")
  ]
  commands_run = [
    result.get("command")
    for result in run_results
    if result.get("command")
  ]

  return f"""
Tests created:
{chr(10).join(f"- {path}" for path in created) if created else "- None"}

Tests changed:
{chr(10).join(f"- {path}" for path in changed) if changed else "- None"}

Relevant tests found:
{chr(10).join(f"- {path}" for path in existing_paths) if existing_paths else "- None"}

Registry updates:
{chr(10).join(f"- {item}" for item in registry_updates) if registry_updates else "- None"}

Commands run:
{chr(10).join(f"- {command}" for command in commands_run) if commands_run else "- None"}

Skipped:
{chr(10).join(f"- {item}" for item in plan.get("skipped", [])) if plan.get("skipped") else "- None"}

Failed writes:
{chr(10).join(f"- {json.dumps(item)}" for item in failed_writes) if failed_writes else "- None"}

Failed registry updates:
{chr(10).join(f"- {json.dumps(item)}" for item in failed_registry_updates) if failed_registry_updates else "- None"}

Test run failures:
{format_failure_details(run_results)}

Notes:
{chr(10).join(f"- {item}" for item in plan.get("notes", [])) if plan.get("notes") else "- None"}

Audit:
{json.dumps(audit, indent=2)}
"""

def create_tests_for_changed_files(changed_files, run_registered_suites=True):
  reset_test_creation_audit()

  diff_json = tools.get_changed_file_diff(
    repo_path=repo_path,
    changed_files=changed_files,
  )

  context_json = tools.inspect_test_context(
    repo_path=repo_path,
    changed_files=changed_files,
    registry_path=registry_path,
  )

  planner_context = build_compact_planner_context(diff_json, context_json)
  has_actionable_work = create_request_has_actionable_work(planner_context)

  if existing_valid_candidate_test_files(planner_context) and not has_actionable_work:
    return run_existing_tests_for_create_request(changed_files, planner_context)

  plan = None
  deterministic_plan = normalize_test_write_plan(deterministic_test_write_plan(planner_context))
  if deterministic_plan.get("writes") and plan_is_usable(deterministic_plan, planner_context):
    plan = deterministic_plan

  plan_prompt = f"""
Create a concrete JSON test-write plan for an already-written feature change.

Return only valid JSON. Do not use markdown. Do not explain.

Rules:
- Use the git diff to identify changed behavior.
- If there is no git diff because this is missing-coverage work, use changedFilePreviews and testIntentByFile as the source of behavior to cover.
- Use testIntentByFile to decide whether a new test is justified and what kind of behavior it should verify.
- Only create tests for files whose testIntentByFile entry has shouldTest: true, unless you are replacing an existing invalid test with a meaningfully better one.
- Each created test must assert a behavior from suggestedAssertions, a changed branch, or a user/caller-visible outcome.
- Use nearby test previews only to match project test style.
- Import source files with the project's @/ alias without an extra src segment; use @/pages/Foo, not @/src/pages/Foo.
- If the source uses export default and does not separately export the component by name, use a default import in the test.
- Prefer patching an existing nearby test when appropriate.
- If an existing generated test file is empty, placeholder-only, or contains no it(...)/test(...) calls, replace it with a complete test file by setting overwrite to true.
- Otherwise create one focused test file.
- Only plan writes to test files.
- Test file content must contain real Vitest tests using describe(...), it(...), or test(...).
- Do not create placeholder files, exported test functions, snapshot-only shells, or comments like "test code here".
- Do not create render-only smoke tests such as "renders without crashing" plus container.firstChild.
- Do not render hooks or utility functions as JSX. Use renderHook for hooks and direct function calls for utilities.
- Do not render lowercase JSX tags such as <parseMentions /> or <alert />; import and exercise the real exported symbol.
- Prefer assertions with screen queries, hook results, function outputs, mocked calls, disabled/loading/error states, submitted payloads, or navigation decisions.
- Whenever you create a new test file, also plan an update to the test registry so the new file is discoverable and runnable.
- Add the new test file to the most relevant existing suite when one matches the changed file.
- Whenever you create a new test file, include a registryUpdates entry for it.
- Prefer adding the new test file to the most relevant existing suite.
- registryUpdates[].suiteId must be one of the matched suite IDs when possible.
- registryUpdates[].testFile must exactly match the created test file path.
- registryUpdates[].relatedSource must include the changed source file that caused the test.
- registryUpdates[].runWhenChanged must include the changed source file or a narrow glob for that source area.
- If no suite matches, create a skipped registry update note explaining why.
- Include full test file content for create actions.
- Include overwrite: true only when replacing an existing invalid generated test file.
- Include exact findText and replaceText for patch actions.
- If any diff has addedLines, try to create or patch at least one focused test for that behavior.
- If there are no addedLines but testIntentByFile marks a file as shouldTest: true, create a focused missing-coverage test from the source preview when the required props, mocks, and expected outcomes are inferable.
- If you cannot create or patch a meaningful test, writes must be [] and skipped must explain the specific missing technical information.
- If the only available test would be a generic render smoke test, writes must be [] and skipped must say the file has no meaningful behavior target.
- Do not say the input is a test report, code coverage report, code inspection report, or nearby tool output.
- Do not summarize the input.

Planner context:
{json.dumps(planner_context, indent=2)}
"""

  if plan is None:
    plan = normalize_test_write_plan(get_test_write_plan(plan_prompt))

  has_actionable_candidate = has_actionable_work

  if has_actionable_candidate and not plan_is_usable(plan, planner_context):
    repair_prompt = f"""
Return only valid JSON. No prose. No markdown.

The previous plan skipped, but this request is actionable.

You must create or repair one focused test file when:
- candidateTestPreviews contains a missing candidate test file, or
- invalidCandidateTests contains an existing placeholder/invalid generated test, or
- unregisteredCandidateTests contains an existing test file missing from the registry.

Rules:
- Use testIntentByFile to choose the behavior target.
- Do not create a test for a source whose testIntentByFile entry has shouldTest: false, unless replacing an existing invalid test.
- If invalidCandidateTests is not empty, replace the first invalid file with a complete Vitest test and set overwrite to true.
- If no invalid test exists, create the best candidate test file from candidateTestFiles.
- If unregisteredCandidateTests is not empty, include a registryUpdates entry for that file even if no write is needed.
- Do not create render-only smoke tests, container.firstChild assertions, hook-as-JSX tests, or lowercase JSX pseudo-component tests.
- Hooks must use renderHook; utilities must be called directly; components must assert visible state, interactions, callbacks, or navigation.
- Import source files with the project's @/ alias without an extra src segment; use @/pages/Foo, not @/src/pages/Foo.
- If the source uses export default and does not separately export the component by name, use a default import in the test.
- Use changedFilePreviews as the source code context when git diff is empty.
- Use matchedSuites[0].id for registryUpdates[].suiteId when available.
- Include the chosen suite ID in suiteIdsToRun.
- Do not summarize this context.

Planner context:
{json.dumps(planner_context, indent=2)}
"""
    plan = normalize_test_write_plan(get_test_write_plan(repair_prompt))

  if has_actionable_candidate and not plan_is_usable(plan, planner_context):
    plan = normalize_test_write_plan(deterministic_test_write_plan(planner_context))

  if has_actionable_candidate:
    plan = normalize_test_write_plan(complete_create_plan_for_requested_sources(plan, planner_context))

  Path("output.md").write_text(
    json.dumps(plan, indent=2),
    encoding="utf-8",
  )

  write_results = []
  registry_results = []

  for write in plan.get("writes", []):
    action = write.get("action")
    file_path = write.get("filePath")

    if action == "create":
      content = normalize_generated_test_imports(
        write.get("content", ""),
        planner_context,
      )
      result_json = tools.write_test_file(
        repo_path=repo_path,
        file_path=file_path,
        content=content,
        overwrite=bool(write.get("overwrite", False)),
      )
      result = json.loads(result_json)
      write_results.append(result)
      record_test_write_activity("write_test_file", file_path, result)

    elif action == "patch":
      replace_text = normalize_generated_test_imports(
        write.get("replaceText", ""),
        planner_context,
      )
      result_json = tools.patch_test_file(
        repo_path=repo_path,
        file_path=file_path,
        find_text=write.get("findText", ""),
        replace_text=replace_text,
      )
      result = json.loads(result_json)
      write_results.append(result)
      record_test_write_activity("patch_test_file", file_path, result)

    else:
      write_results.append({
        "ok": False,
        "error": f"Unsupported write action: {action}",
        "path": file_path,
      })

  registry_updates_to_apply = list(plan.get("registryUpdates", []))
  default_suite_id = None
  if planner_context.get("matchedSuites"):
    default_suite_id = planner_context["matchedSuites"][0].get("id")

  def has_registry_update_for(test_file):
    return any(
      update.get("testFile") == test_file
      for update in registry_updates_to_apply
    )

  if default_suite_id:
    for result in write_results:
      test_file = result.get("path")
      if result.get("ok") and test_file and not has_registry_update_for(test_file):
        registry_updates_to_apply.append({
          "suiteId": default_suite_id,
          "testFile": test_file,
          "relatedSource": planner_context.get("changedFiles", []),
          "runWhenChanged": planner_context.get("changedFiles", []),
        })

    for test_file in planner_context.get("unregisteredCandidateTests", []):
      if not has_registry_update_for(test_file):
        registry_updates_to_apply.append({
          "suiteId": default_suite_id,
          "testFile": test_file,
          "relatedSource": planner_context.get("changedFiles", []),
          "runWhenChanged": planner_context.get("changedFiles", []),
        })

  if not default_suite_id:
    bootstrap_suite_id = bootstrap_registry_suite_id(planner_context)
    for result in write_results:
      test_file = result.get("path")
      inferred_suite_id = infer_registry_suite_id(test_file, planner_context) if result.get("ok") and test_file else None
      suite_id = inferred_suite_id or bootstrap_suite_id

      if suite_id and not has_registry_update_for(test_file):
        registry_updates_to_apply.append({
          "suiteId": suite_id,
          "testFile": test_file,
          "relatedSource": planner_context.get("changedFiles", []),
          "runWhenChanged": planner_context.get("changedFiles", []),
        })

    for test_file in planner_context.get("unregisteredCandidateTests", []):
      inferred_suite_id = infer_registry_suite_id(test_file, planner_context)
      suite_id = inferred_suite_id or bootstrap_suite_id

      if suite_id and not has_registry_update_for(test_file):
        registry_updates_to_apply.append({
          "suiteId": suite_id,
          "testFile": test_file,
          "relatedSource": planner_context.get("changedFiles", []),
          "runWhenChanged": planner_context.get("changedFiles", []),
        })

  for update in registry_updates_to_apply:
    result_json = tools.update_test_registry(
      repo_path=repo_path,
      registry_path=registry_path,
      suite_id=update.get("suiteId", ""),
      test_file=update.get("testFile", ""),
      related_source=update.get("relatedSource", []),
      run_when_changed=update.get("runWhenChanged", []),
    )
    registry_results.append(json.loads(result_json))

  run_results = []
  can_run_planned_suites = (
    not any(not result.get("ok") for result in write_results)
    and not any(not result.get("ok") for result in registry_results)
  )

  suite_ids_to_run = []

  if run_registered_suites:
    suite_ids_to_run = list(dict.fromkeys(
      (list(plan.get("suiteIdsToRun", [])) if can_run_planned_suites else [])
      + [
        result.get("suiteId")
        for result in registry_results
        if result.get("ok") and result.get("suiteId")
      ]
    ))

  for suite_id in suite_ids_to_run:
    result_json = tools.run_test_suite(
      repo_path=repo_path,
      suite_id=suite_id,
      registry_path=registry_path,
    )
    result = json.loads(result_json)
    run_results.append(result)
    record_test_run_activity(result)

  written_test_files = [
    result.get("path")
    for result in write_results
    if result.get("ok") and result.get("path")
  ]

  can_run_written_files_directly = not any(not result.get("ok") for result in write_results)

  if can_run_written_files_directly and written_test_files and not run_results:
    result_json = tools.run_test_files(
      repo_path=repo_path,
      test_files=list(dict.fromkeys(written_test_files)),
    )
    result = json.loads(result_json)
    run_results.append(result)
    record_test_run_activity(result)
    plan.setdefault("notes", []).append(
      "No registered suite was available for the written tests, so the created/changed test files were run directly."
    )

  audit = load_test_creation_audit()

  created = [
    result["path"]
    for result in write_results
    if result.get("ok") and result.get("action") == "created"
  ]

  changed = [
    result["path"]
    for result in write_results
    if result.get("ok") and result.get("action") in ("updated", "patched")
  ]

  failed_writes = [
    result
    for result in write_results
    if not result.get("ok")
  ]

  registry_updates = [
    f"{result.get('suiteId')}: {result.get('testFile')}"
    for result in registry_results
    if result.get("ok")
  ]

  failed_registry_updates = [
    result
    for result in registry_results
    if not result.get("ok")
  ]

  commands_run = [
    result.get("command")
    for result in run_results
    if result.get("command")
  ]

  return f"""
Tests created:
{chr(10).join(f"- {path}" for path in created) if created else "- None"}

Tests changed:
{chr(10).join(f"- {path}" for path in changed) if changed else "- None"}

Registry updates:
{chr(10).join(f"- {item}" for item in registry_updates) if registry_updates else "- None"}

Commands run:
{chr(10).join(f"- {command}" for command in commands_run) if commands_run else "- None"}

Skipped:
{chr(10).join(f"- {item}" for item in plan.get("skipped", [])) if plan.get("skipped") else "- None"}

Failed writes:
{chr(10).join(f"- {json.dumps(item)}" for item in failed_writes) if failed_writes else "- None"}

Failed registry updates:
{chr(10).join(f"- {json.dumps(item)}" for item in failed_registry_updates) if failed_registry_updates else "- None"}

Test run failures:
{format_failure_details(run_results)}

Notes:
{chr(10).join(f"- {item}" for item in plan.get("notes", [])) if plan.get("notes") else "- None"}

Audit:
{json.dumps(audit, indent=2)}
"""

def get_test_write_plan(plan_prompt):
  attempts = [
    plan_prompt,
    f"""
Return only valid JSON. No prose. No markdown.

Create or patch one focused test if the planner context contains a concrete behavior target.
For missing-coverage work with no git diff, use changedFilePreviews and testIntentByFile to identify existing behavior worth testing.
If you cannot, return writes as [] and put the specific missing technical information in skipped.

JSON shape:
{{
  "writes": [],
  "registryUpdates": [],
  "suiteIdsToRun": [],
  "skipped": [],
  "notes": []
}}

Original task:
{plan_prompt}
"""
  ]

  last_response = ""

  for attempt in attempts:
    with contextlib.redirect_stdout(io.StringIO()):
      try:
        raw_plan = str(test_plan_agent(attempt)).strip()
      except Exception as error:
        last_response = f"{type(error).__name__}: {error}"
        continue
    last_response = raw_plan

    try:
      return parse_test_write_plan(raw_plan)
    except Exception:
      continue

  Path("last-invalid-test-plan.txt").write_text(
    last_response,
    encoding="utf-8",
  )

  return {
    "writes": [],
    "suiteIdsToRun": [],
    "skipped": [
      "The test planner did not return a usable plan after retry."
    ],
    "notes": [
      "Raw invalid planner output or model error was written to last-invalid-test-plan.txt."
    ],
  }

def command_argument(user_input):
  parts = user_input.strip().split(maxsplit=1)
  return parts[1].strip() if len(parts) > 1 else ""

def parse_changed_files_argument(value):
  return tools.parse_file_paths_argument(value)

CREATE_MISSING_TESTS_BATCH_SIZE = 1

def chunks(items, size):
  for index in range(0, len(items), size):
    yield items[index:index + size]

def format_selected_test_intent(selected_files, discovery):
  intent_by_file = discovery.get("sourceTestIntentByFile", {})
  rows = []

  for source_file in selected_files:
    intent = intent_by_file.get(source_file, {})
    rows.append(
      f"{source_file}: intent {intent.get('score', 'unknown')}, "
      f"feasibility {intent.get('feasibilityScore', 'unknown')}, "
      f"selection {intent.get('selectionScore', 'unknown')}"
    )

  return markdown_list(rows)

def create_missing_tests_command(argument=None):
  discovery = tools.discover_missing_test_sources(
    repo_path=repo_path,
    registry_path=registry_path,
  )

  if not discovery.get("ok"):
    return discovery.get("error", "Could not discover source files missing tests.")

  missing_files = discovery.get("missingTestSourceFiles", [])
  selected_files = missing_files
  limit_note = None

  argument = (argument or "").strip()
  if argument:
    limit_match = re.fullmatch(r"(?:--limit\s+)?(\d+)", argument)
    if not limit_match:
      return "Usage: `create-missing-tests` or `create-missing-tests --limit <count>`."

    limit = int(limit_match.group(1))
    selected_files = missing_files[:limit]
    limit_note = f"Limited to the first {limit} missing source file(s)."

  if not selected_files:
    summary = discovery.get("summary", {})
    return f"""# Create Missing Tests

## Summary
Status: Completed
Source files scanned: {summary.get("sourceFiles", 0)}
Source files missing tests: 0
Low-intent source files deferred: {summary.get("deferredLowIntentSourceFiles", 0)}
Source files selected this run: 0
Source files processed this run: 0
Source files left for later: 0

## Details
Source files scanned: {summary.get("sourceFiles", 0)}
Existing test files found: {summary.get("testFiles", 0)}
Source files already covered: {summary.get("coveredSourceFiles", 0)}

No actionable source files missing tests were found.
"""

  batch_results = []
  interrupted_batch = None

  for batch_index, batch in enumerate(chunks(selected_files, CREATE_MISSING_TESTS_BATCH_SIZE), start=1):
    try:
      batch_results.append({
        "files": batch,
        "result": create_tests_for_changed_files(batch, run_registered_suites=False),
      })
    except KeyboardInterrupt:
      interrupted_batch = {
        "index": batch_index,
        "files": batch,
      }
      break
    except Exception as error:
      batch_results.append({
        "files": batch,
        "result": f"Batch failed with {type(error).__name__}: {error}",
      })

  summary = discovery.get("summary", {})
  result_sections = []
  total_batches = (len(selected_files) + CREATE_MISSING_TESTS_BATCH_SIZE - 1) // CREATE_MISSING_TESTS_BATCH_SIZE

  for index, batch_result in enumerate(batch_results, start=1):
    result_sections.append(f"""## Batch {index}

Source files:
{markdown_list(batch_result["files"])}

Result:
{batch_result["result"].strip()}
""")

  notes = []
  if limit_note:
    notes.append(limit_note)

  completed_file_count = sum(len(batch_result["files"]) for batch_result in batch_results)
  selected_unprocessed_files = selected_files[completed_file_count:]

  if interrupted_batch:
    notes.append(
      f"Stopped by user during batch {interrupted_batch['index']}. "
      "Some writes from that batch may already have completed; check git diff and last-test-creation-audit.json."
    )

  unprocessed_count = len(missing_files) - completed_file_count
  if unprocessed_count > 0:
    notes.append(f"{unprocessed_count} missing source file(s) were left for a later run.")

  status = "Stopped by user" if interrupted_batch else "Completed"
  completed_batch_count = len(batch_results)
  selected_unprocessed_count = len(selected_unprocessed_files)

  stopped_section = ""
  if interrupted_batch:
    stopped_section = f"""## Interrupted Batch
Stopped while processing:
{markdown_list(interrupted_batch["files"])}

Selected files not fully processed:
{markdown_list(selected_unprocessed_files)}
"""

  return f"""# Create Missing Tests

## Summary
Status: {status}
Batches completed: {completed_batch_count} of {total_batches}
Source files scanned: {summary.get("sourceFiles", 0)}
Source files missing tests: {summary.get("missingTestSourceFiles", 0)}
Low-intent source files deferred: {summary.get("deferredLowIntentSourceFiles", 0)}
Source files selected this run: {len(selected_files)}
Source files processed this run: {completed_file_count}
Selected source files left: {selected_unprocessed_count}
Total missing source files left: {unprocessed_count}

## Details
Source files scanned: {summary.get("sourceFiles", 0)}
Existing test files found: {summary.get("testFiles", 0)}
Source files already covered: {summary.get("coveredSourceFiles", 0)}
Source files missing tests: {summary.get("missingTestSourceFiles", 0)}
Low-intent source files deferred: {summary.get("deferredLowIntentSourceFiles", 0)}
Source files selected this run: {len(selected_files)}
Source files processed this run: {completed_file_count}

Missing source files selected:
{markdown_list(selected_files)}

Selected file scores:
{format_selected_test_intent(selected_files, discovery)}

Notes:
{markdown_list(notes)}

{stopped_section}
{chr(10).join(result_sections)}
"""

def format_failure_classification(classification):
  if not classification:
    return "unknown"

  label = classification.get("classification", "unknown")
  confidence = classification.get("confidence")
  repair_safe = classification.get("repairSafe")

  parts = [label]
  if confidence:
    parts.append(f"confidence={confidence}")
  if repair_safe is not None:
    parts.append(f"repairSafe={repair_safe}")

  if len(parts) == 1:
    return label

  return f"{label} ({', '.join(parts[1:])})"

def format_test_failure_list(result_json):
  data = json.loads(result_json)

  if not data.get("ok"):
    return data.get("error", "Could not list test failures.")

  failures = data.get("failures", [])
  if not failures:
    return "No captured test failures."

  rows = []
  for failure in failures:
    test_files = failure.get("testFiles") or []
    rows.append(
      f"- {failure.get('id')}: {failure.get('status')} "
      f"{format_failure_classification(failure.get('classification'))} "
      f"suite={failure.get('suiteId')} "
      f"files={', '.join(test_files) if test_files else 'unknown'} "
      f"fix=`fix-tests {failure.get('id')}`"
    )

  return (
    "Captured test failures:\n"
    + "\n".join(rows)
    + "\n\nUse `inspect-failure <id-or-file>` to view one, or `fix-tests <id-or-file>` to try a guarded repair."
  )

def format_test_failure_inspection(result_json):
  data = json.loads(result_json)

  if not data.get("ok"):
    return data.get("error", "Could not inspect test failure.")

  failure = data.get("failure", {})
  previews = data.get("testFilePreviews", [])
  classification = failure.get("classification") or {}
  test_files = failure.get("testFiles") or []
  preview_paths = [
    preview.get("path")
    for preview in previews
    if preview.get("path")
  ]

  return f"""Test failure: {failure.get("id")}

Status: {failure.get("status")}
Suite: {failure.get("suiteId")}
Classification: {format_failure_classification(classification)}
Command: `{failure.get("command") or "unknown"}`
Test files: {", ".join(test_files) if test_files else "unknown"}
Previewed files: {", ".join(preview_paths) if preview_paths else "none"}

Failure:
```text
{truncate_text(failure.get("failureReason") or "No failure output captured.", 2500)}
```

Repair guidance:
- {classification.get("guidance", "Inspect before patching.")}
- Use `fix-tests {failure.get("id")}` to try a guarded repair.
"""

def inspect_failure_command(failure_argument=None):
  failure_id = (
    find_failure_id_for_argument(failure_argument) or failure_argument
    if failure_argument
    else None
  )

  return format_test_failure_inspection(
    tools.inspect_test_failure(
      repo_path=repo_path,
      failure_id=failure_id or None,
    )
  )

def normalize_repo_path(path):
  return Path(path).as_posix().strip()

def normalized_repair_path_key(path):
  normalized = normalize_repo_path(path)

  try:
    candidate = Path(normalized)
    if candidate.is_absolute():
      normalized = candidate.resolve().relative_to(Path(repo_path).resolve()).as_posix()
  except Exception:
    pass

  return normalized.lower()

def looks_like_path(value):
  if not value:
    return False

  return (
    "/" in value
    or "\\" in value
    or bool(re.search(r"\.(ts|tsx|js|jsx)$", value))
  )

def is_test_path(path):
  normalized = normalize_repo_path(path)
  return bool(re.search(r"\.(test|spec)\.(ts|tsx|js|jsx)$", normalized))

def is_source_path(path):
  normalized = normalize_repo_path(path)
  suffix = Path(normalized).suffix.lower()

  if suffix not in (".ts", ".tsx", ".js", ".jsx"):
    return False

  if normalized.endswith(".d.ts") or is_test_path(normalized):
    return False

  return True

def split_diff_changes_for_test_workflow(changes):
  added_source_files = []
  modified_source_files = []
  renamed_source_files = []
  deleted_source_files = []
  changed_test_files = []
  deleted_test_files = []
  skipped = []

  for change in changes:
    path = normalize_repo_path(change.get("path") or "")
    old_path = normalize_repo_path(change.get("oldPath") or "")
    status_code = change.get("statusCode") or ""
    status = change.get("status") or status_code or "?"

    if not path:
      skipped.append(f"{status}: missing path")
      continue

    if status_code == "D":
      if is_test_path(path):
        deleted_test_files.append(path)
      elif is_source_path(path):
        deleted_source_files.append(path)
      else:
        skipped.append(f"{status}: {path} (deleted non-source/test file)")
      continue

    if is_test_path(path):
      changed_test_files.append(path)
      continue

    if is_source_path(path):
      if status_code in ("A", "C"):
        added_source_files.append(path)
      elif status_code == "R":
        renamed_source_files.append(path)
      else:
        modified_source_files.append(path)
      continue

    if old_path and is_source_path(old_path) and status_code == "R":
      skipped.append(f"{status}: {old_path} -> {path} (renamed to non-source/test file)")
    else:
      skipped.append(f"{status}: {path} (not a source or test file)")

  return {
    "addedSourceFiles": list(dict.fromkeys(added_source_files)),
    "modifiedSourceFiles": list(dict.fromkeys(modified_source_files)),
    "renamedSourceFiles": list(dict.fromkeys(renamed_source_files)),
    "deletedSourceFiles": list(dict.fromkeys(deleted_source_files)),
    "changedTestFiles": list(dict.fromkeys(changed_test_files)),
    "deletedTestFiles": list(dict.fromkeys(deleted_test_files)),
    "skipped": skipped,
  }

def run_tests_for_git_diff(base_ref="HEAD"):
  reset_test_creation_audit()

  diff_json = tools.get_changed_files_from_git_diff(
    repo_path=repo_path,
    base_ref=base_ref or "HEAD",
  )
  diff_data = json.loads(diff_json)

  if not diff_data.get("ok"):
    return diff_data.get("error", "Could not read git diff changed files.")

  split = split_diff_changes_for_test_workflow(diff_data.get("changes", []))
  added_source_files = split["addedSourceFiles"]
  modified_source_files = split["modifiedSourceFiles"]
  renamed_source_files = split["renamedSourceFiles"]
  deleted_source_files = split["deletedSourceFiles"]
  changed_test_files = split["changedTestFiles"]
  deleted_test_files = split["deletedTestFiles"]
  skipped = split["skipped"]
  edit_source_files = list(dict.fromkeys(modified_source_files + renamed_source_files))

  if not added_source_files and not edit_source_files and not changed_test_files and not deleted_source_files and not deleted_test_files:
    return f"""# Git Diff Test Workflow

Base ref: `{diff_data.get("baseRef")}`

No changed source or test files were found in the target repo diff.

Skipped:
{markdown_list(skipped)}
"""

  create_result = None
  if added_source_files:
    create_result = create_tests_for_changed_files(added_source_files)

  edit_result = None
  if edit_source_files:
    edit_result = edit_tests_for_changed_files(edit_source_files)

  cleanup_result = None
  if deleted_source_files or deleted_test_files:
    cleanup_json = tools.cleanup_tests_for_deleted_sources(
      repo_path=repo_path,
      registry_path=registry_path,
      deleted_source_files=deleted_source_files,
      deleted_test_files=deleted_test_files,
    )
    cleanup_result = json.loads(cleanup_json)

  changed_test_run = None
  if changed_test_files:
    result_json = tools.run_test_files(
      repo_path=repo_path,
      test_files=changed_test_files,
    )
    changed_test_run = json.loads(result_json)
    record_test_run_activity(changed_test_run)

  changed_test_commands = []
  if changed_test_run and changed_test_run.get("command"):
    changed_test_commands.append(changed_test_run.get("command"))

  changed_test_failures = format_failure_details([changed_test_run]) if changed_test_run else "- None"
  removed_tests = []
  skipped_cleanup = []
  registry_cleanup = "- None"

  if cleanup_result:
    removed_tests = [
      item.get("path")
      for item in cleanup_result.get("testFilesRemoved", [])
      if item.get("path")
    ]
    skipped_cleanup = [
      f"{item.get('path')}: {item.get('reason')}"
      for item in cleanup_result.get("testFilesSkipped", [])
      if item.get("path")
    ]
    registry_cleanup = (
      "Updated"
      if cleanup_result.get("registryUpdated")
      else "No registry changes"
    )

  return f"""# Git Diff Test Workflow

Base ref: `{diff_data.get("baseRef")}`

Added source files -> create-tests:
{markdown_list(added_source_files)}

Modified source files -> edit-tests:
{markdown_list(modified_source_files)}

Renamed source files -> edit-tests:
{markdown_list(renamed_source_files)}

Deleted source files -> cleanup:
{markdown_list(deleted_source_files)}

Changed test files from diff:
{markdown_list(changed_test_files)}

Deleted test files -> registry cleanup:
{markdown_list(deleted_test_files)}

Skipped:
{markdown_list(skipped)}

Create-tests result:
{create_result if create_result else "- None"}

Edit-tests result:
{edit_result if edit_result else "- None"}

Cleanup result:
- Tests removed: {", ".join(removed_tests) if removed_tests else "None"}
- Registry: {registry_cleanup}
- Skipped cleanup: {", ".join(skipped_cleanup) if skipped_cleanup else "None"}

Changed test file commands:
{markdown_list(changed_test_commands)}

Changed test file failures:
{changed_test_failures}
"""

def candidate_tests_for_source_path(source_path):
  source = Path(normalize_repo_path(source_path))
  suffix = ".test.tsx" if source.suffix in [".tsx", ".jsx"] else ".test.ts"
  spec_suffix = ".spec.tsx" if source.suffix in [".tsx", ".jsx"] else ".spec.ts"
  candidates = []

  if len(source.parts) > 2 and source.parts[0] == "src":
    candidates.append(Path("src/test").joinpath(*source.parts[1:-1], f"{source.stem}{suffix}").as_posix())
    candidates.append(Path("src/test").joinpath(*source.parts[1:-1], f"{source.stem}{spec_suffix}").as_posix())

  candidates.append(source.with_name(f"{source.stem}{suffix}").as_posix())
  candidates.append(source.with_name(f"{source.stem}{spec_suffix}").as_posix())

  return list(dict.fromkeys(candidates))

def failure_matches_argument(failure, argument):
  if not argument:
    return False

  normalized_arg = normalize_repo_path(argument)

  if failure.get("id") == argument:
    return True

  candidate_paths = {normalized_arg}
  if not is_test_path(normalized_arg):
    candidate_paths.update(candidate_tests_for_source_path(normalized_arg))

  command = normalize_repo_path(failure.get("command") or "")
  test_files = [
    normalize_repo_path(path)
    for path in failure.get("testFiles", [])
  ]

  if any(path in test_files for path in candidate_paths):
    return True

  if any(path and path in command for path in candidate_paths):
    return True

  arg_stem = Path(normalized_arg).stem
  return bool(arg_stem and any(Path(path).stem == arg_stem for path in test_files))

def find_failure_id_for_argument(argument):
  arguments = tools.parse_file_paths_argument(argument)
  if not arguments:
    return None

  list_json = tools.list_test_failures(repo_path=repo_path)
  data = json.loads(list_json)

  if not data.get("ok"):
    return None

  failures = data.get("failures", [])

  for argument_item in arguments:
    for failure in failures:
      if failure.get("id") == argument_item:
        return argument_item

  open_matches = [
    failure
    for failure in failures
    if failure.get("status") == "open"
    and any(failure_matches_argument(failure, argument_item) for argument_item in arguments)
  ]
  if open_matches:
    return open_matches[-1].get("id")

  matches = [
    failure
    for failure in failures
    if any(failure_matches_argument(failure, argument_item) for argument_item in arguments)
  ]

  return matches[-1].get("id") if matches else None

def run_tests_to_capture_failure(argument):
  arguments = [
    normalize_repo_path(argument_item)
    for argument_item in tools.parse_file_paths_argument(argument)
    if looks_like_path(argument_item)
  ]

  if not arguments:
    return {
      "ok": False,
      "message": "No captured test failure was found. Run a test first, or pass a source/test file path.",
    }

  run_results = []
  test_file_arguments = [
    argument_item
    for argument_item in arguments
    if is_test_path(argument_item)
  ]
  source_file_arguments = [
    argument_item
    for argument_item in arguments
    if not is_test_path(argument_item)
  ]

  if test_file_arguments:
    result = json.loads(tools.run_test_files(
      repo_path=repo_path,
      test_files=test_file_arguments,
    ))
    run_results.append(result)
    record_test_run_activity(result)

  if source_file_arguments:
    matched_data = json.loads(tools.find_relevant_test_suites(
      repo_path=repo_path,
      changed_files=source_file_arguments,
      registry_path=registry_path,
    ))
    matched_suites = matched_data.get("matchedSuites", []) if matched_data.get("ok") else []

    for suite in matched_suites:
      result = json.loads(tools.run_test_suite(
        repo_path=repo_path,
        suite_id=suite.get("id"),
        registry_path=registry_path,
      ))
      run_results.append(result)
      record_test_run_activity(result)
      if not result.get("ok"):
        break

    if not run_results:
      context_data = json.loads(tools.inspect_test_context(
        repo_path=repo_path,
        changed_files=source_file_arguments,
        registry_path=registry_path,
      ))
      candidate_tests = [
        preview.get("path")
        for preview in context_data.get("candidateTestPreviews", [])
        if preview.get("exists") and preview.get("path")
      ]
      if candidate_tests:
        result = json.loads(tools.run_test_files(
          repo_path=repo_path,
          test_files=candidate_tests[:3],
        ))
        run_results.append(result)
        record_test_run_activity(result)

  if not run_results:
    return {
      "ok": False,
      "message": f"No tests could be found for `{', '.join(arguments)}`.",
    }

  if all(result.get("ok") for result in run_results):
    return {
      "ok": False,
      "message": "The relevant tests passed, so there is no broken test to repair.",
      "runResults": run_results,
    }

  failure_id = find_failure_id_for_argument(argument)
  return {
    "ok": bool(failure_id),
    "failureId": failure_id,
    "runResults": run_results,
    "message": "A failing test run was captured." if failure_id else "A test failed, but no matching failure record could be found.",
  }

def build_repair_planner_context(inspect_data):
  failure = inspect_data.get("failure", {})
  test_file_previews = inspect_data.get("testFilePreviews", [])
  failing_test_files = tools.infer_failing_test_files_from_failure_text(failure.get("failureReason") or "")
  failing_test_file_keys = {
    normalized_repair_path_key(path)
    for path in failing_test_files
  }

  if failing_test_file_keys:
    narrowed_previews = [
      preview
      for preview in test_file_previews
      if normalized_repair_path_key(preview.get("path")) in failing_test_file_keys
    ]
    if narrowed_previews:
      test_file_previews = narrowed_previews

  return {
    "failure": {
      "id": failure.get("id"),
      "suiteId": failure.get("suiteId"),
      "command": failure.get("command"),
      "testFiles": failing_test_files or failure.get("testFiles", []),
      "failingTestFiles": failing_test_files,
      "classification": failure.get("classification"),
      "failureReason": truncate_text(failure.get("failureReason"), 5000),
    },
    "testFilePreviews": [
      {
        "path": preview.get("path"),
        "ok": preview.get("ok"),
        "contentPreview": truncate_text(preview.get("contentPreview"), 12000),
      }
      for preview in test_file_previews
    ],
  }

def quote_js_string(value):
  return json.dumps(value)

def extract_missing_test_setup_path(failure_text):
  if not failure_text:
    return None

  repo_root = Path(repo_path).resolve()
  absolute_match = re.search(
    r"(?P<path>[A-Za-z]:[^\n'\"`<>]*src[/\\]test[/\\]setup\.(?:ts|tsx|js|jsx))",
    failure_text,
    re.IGNORECASE,
  )
  if absolute_match:
    candidate = Path(absolute_match.group("path")).resolve()
    try:
      return candidate.relative_to(repo_root).as_posix()
    except ValueError:
      pass

  relative_match = re.search(
    r"(?P<path>src[/\\]test[/\\]setup\.(?:ts|tsx|js|jsx))",
    failure_text,
    re.IGNORECASE,
  )
  if relative_match:
    return Path(relative_match.group("path")).as_posix()

  return None

def stale_missing_test_setup_path(failure_text):
  return tools.stale_missing_test_setup_path(
    Path(repo_path).resolve(),
    failure_text,
  )

def test_setup_file_content(setup_path):
  return """import { vi } from 'vitest';
import '@testing-library/jest-dom/vitest';

if (typeof window !== 'undefined') {
  if (!window.matchMedia) {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation(query => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
  }

  if (!window.ResizeObserver) {
    Object.defineProperty(window, 'ResizeObserver', {
      writable: true,
      value: class ResizeObserver {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    });
  }

  if (!window.IntersectionObserver) {
    Object.defineProperty(window, 'IntersectionObserver', {
      writable: true,
      value: class IntersectionObserver {
        observe() {}
        unobserve() {}
        disconnect() {}
        takeRecords() {
          return [];
        }
      },
    });
  }

  if (!window.scrollTo) {
    Object.defineProperty(window, 'scrollTo', {
      writable: true,
      value: vi.fn(),
    });
  }
}
"""

def existing_test_setup_path():
  candidates = [
    "src/test/setup.ts",
    "src/test/setup.tsx",
    "src/test/setup.js",
    "src/test/setup.jsx",
    "src/setupTests.ts",
    "src/setupTests.tsx",
    "src/setupTests.js",
    "src/setupTests.jsx",
    "tests/setup.ts",
    "tests/setup.tsx",
    "tests/setup.js",
    "tests/setup.jsx",
  ]

  repo_root = Path(repo_path).resolve()
  for candidate in candidates:
    if (repo_root / candidate).exists():
      return candidate

  return "src/test/setup.ts"

def merged_test_setup_content(setup_path):
  repo_root = Path(repo_path).resolve()
  setup_file = (repo_root / setup_path).resolve()
  desired = test_setup_file_content(setup_path)

  if not setup_file.exists():
    return desired

  existing = setup_file.read_text(encoding="utf-8", errors="replace")
  additions = []

  if "@testing-library/jest-dom/vitest" not in existing:
    additions.append("import '@testing-library/jest-dom/vitest';")

  if "import { vi" not in existing and "vi." not in existing:
    additions.insert(0, "import { vi } from 'vitest';")

  if "window.matchMedia" not in existing:
    additions.append("""if (typeof window !== 'undefined' && !window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation(query => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}""")

  if "ResizeObserver" not in existing:
    additions.append("""if (typeof window !== 'undefined' && !window.ResizeObserver) {
  Object.defineProperty(window, 'ResizeObserver', {
    writable: true,
    value: class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  });
}""")

  if "IntersectionObserver" not in existing:
    additions.append("""if (typeof window !== 'undefined' && !window.IntersectionObserver) {
  Object.defineProperty(window, 'IntersectionObserver', {
    writable: true,
    value: class IntersectionObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return [];
      }
    },
  });
}""")

  if "window.scrollTo" not in existing:
    additions.append("""if (typeof window !== 'undefined' && !window.scrollTo) {
  Object.defineProperty(window, 'scrollTo', {
    writable: true,
    value: vi.fn(),
  });
}""")

  if not additions:
    return existing if existing.endswith("\n") else existing + "\n"

  separator = "\n" if existing.endswith("\n") else "\n\n"
  return existing + separator + "\n\n".join(additions) + "\n"

def needs_jsdom_browser_api_repair(failure_text):
  normalized = (failure_text or "").lower()
  return any(token in normalized for token in [
    "window.matchmedia is not a function",
    "matchmedia is not a function",
    "resizeobserver is not defined",
    "intersectionobserver is not defined",
    "window.scrollto is not a function",
  ])

def repair_missing_jsdom_browser_api(failure_id, failure):
  if not needs_jsdom_browser_api_repair(failure.get("failureReason") or ""):
    return None

  setup_path = existing_test_setup_path()
  result = json.loads(tools.write_test_support_file(
    repo_path=repo_path,
    file_path=setup_path,
    content=merged_test_setup_content(setup_path),
    overwrite=True,
  ))
  record_test_write_activity("write_test_support_file", setup_path, result)

  plan = {
    "notes": [
      f"Updated {setup_path} with jsdom browser API shims used by UI tests.",
      "Added stable shims for matchMedia, ResizeObserver, IntersectionObserver, and scrollTo when missing.",
    ],
  }

  if not result.get("ok"):
    record = json.loads(tools.record_test_repair_attempt(
      repo_path=repo_path,
      failure_id=failure_id,
      status="attempted",
      repaired_files=[],
      notes=[result.get("error", "Could not update the test setup file.")],
    ))
    return format_repair_summary(failure_id, plan, [result], None, record)

  run_result = rerun_failure_tests(failure)
  status = "repaired" if run_result.get("ok") else "attempted"
  record = json.loads(tools.record_test_repair_attempt(
    repo_path=repo_path,
    failure_id=failure_id,
    status=status,
    repaired_files=[result.get("path")],
    notes=plan.get("notes", []),
  ))

  return format_repair_summary(failure_id, plan, [result], run_result, record)

def missing_test_render_helper_path(failure_text):
  if not failure_text:
    return None

  normalized = failure_text.lower()
  if "test/utils/render" not in normalized:
    return None

  if "failed to resolve import" not in normalized and "cannot find module" not in normalized:
    return None

  return "src/test/utils/render.tsx"

def target_repo_file_exists(relative_path):
  try:
    candidate = (Path(repo_path).resolve() / relative_path).resolve()
    candidate.relative_to(Path(repo_path).resolve())
  except ValueError:
    return False

  return candidate.exists()

def target_package_json():
  package_path = Path(repo_path).resolve() / "package.json"
  if not package_path.exists():
    return {}

  try:
    return json.loads(package_path.read_text(encoding="utf-8"))
  except json.JSONDecodeError:
    return {}

def package_has_dependency(package_data, name):
  return (
    name in package_data.get("dependencies", {})
    or name in package_data.get("devDependencies", {})
  )

def test_render_helper_content():
  package_data = target_package_json()
  has_query = package_has_dependency(package_data, "@tanstack/react-query")
  has_router = package_has_dependency(package_data, "react-router-dom")
  has_tooltip = target_repo_file_exists("src/components/ui/tooltip.tsx")
  has_auth_context = (
    target_repo_file_exists("src/contexts/authContextValue.ts")
    and target_repo_file_exists("src/contexts/authTypes.ts")
  )

  imports = [
    "import type { ReactElement, ReactNode } from 'react';",
    "import { render, type RenderOptions } from '@testing-library/react';",
  ]
  if has_query:
    imports.append("import { QueryClient, QueryClientProvider } from '@tanstack/react-query';")
  if has_router:
    imports.append("import { MemoryRouter } from 'react-router-dom';")
  if has_tooltip:
    imports.append("import { TooltipProvider } from '@/components/ui/tooltip';")
  if has_auth_context:
    imports.extend([
      "import { AuthContext } from '@/contexts/authContextValue';",
      "import type { AuthContextType } from '@/contexts/authTypes';",
    ])

  auth_block = ""
  auth_type = ""
  auth_option = ""
  auth_merge = ""
  auth_open = ""
  auth_close = ""
  if has_auth_context:
    auth_type = "\n  auth?: Partial<AuthContextType>;"
    auth_option = "\n    auth,"
    auth_merge = """
  const authValue = {
    ...defaultAuthContext,
    ...auth,
  };
"""
    auth_open = "\n          <AuthContext.Provider value={authValue}>"
    auth_close = "\n          </AuthContext.Provider>"
    auth_block = """
const defaultAuthContext: AuthContextType = {
  user: null,
  session: null,
  profile: null,
  roles: [],
  adminPermissions: [],
  isOwnerAdmin: false,
  isLoading: false,
  hasRole: () => false,
  hasAnyRole: () => false,
  hasAdminPermission: () => false,
  hasAnyAdminPermission: () => false,
  signOut: async () => {},
  refreshRoles: async () => {},
  refreshAdminPermissions: async () => {},
  refreshProfile: async () => {},
  initiateEventMagicAuth: () => null,
};
"""

  query_setup = ""
  query_open = ""
  query_close = ""
  if has_query:
    query_setup = """  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
"""
    query_open = "      <QueryClientProvider client={queryClient}>\n"
    query_close = "\n      </QueryClientProvider>"

  tooltip_open = "        <TooltipProvider>\n" if has_tooltip else ""
  tooltip_close = "\n        </TooltipProvider>" if has_tooltip else ""
  router_open = "            <MemoryRouter initialEntries={[route]}>\n" if has_router else ""
  router_close = "\n            </MemoryRouter>" if has_router else ""
  route_type = "\n  route?: string;" if has_router else ""
  route_option = "\n    route = '/'," if has_router else ""

  child = "<>{children}</>"
  content = f"""{chr(10).join(imports)}
{auth_block}
type RenderWithAppProvidersOptions = RenderOptions & {{{auth_type}{route_type}
}};

export function renderWithAppProviders(
  ui: ReactElement,
  {{{auth_option}{route_option}
    ...renderOptions
  }}: RenderWithAppProvidersOptions = {{}},
) {{
{query_setup}{auth_merge}
  function Wrapper({{ children }}: {{ children: ReactNode }}) {{
    return (
{query_open}{tooltip_open}{auth_open}
{router_open}              {child}
{router_close}{auth_close}{tooltip_close}{query_close}
    );
  }}

  return render(ui, {{
    wrapper: Wrapper,
    ...renderOptions,
  }});
}}
"""

  return content.replace("\n\n\n", "\n\n")

def repair_missing_test_render_helper(failure_id, failure):
  helper_path = missing_test_render_helper_path(failure.get("failureReason") or "")
  if not helper_path:
    return None

  result = json.loads(tools.write_test_support_file(
    repo_path=repo_path,
    file_path=helper_path,
    content=test_render_helper_content(),
    overwrite=False,
  ))
  record_test_write_activity("write_test_support_file", helper_path, result)

  plan = {
    "notes": [
      f"Created missing generated-test render helper {helper_path}.",
      "Wrapped renders with available project providers discovered from the target repo.",
    ],
  }

  if not result.get("ok"):
    record = json.loads(tools.record_test_repair_attempt(
      repo_path=repo_path,
      failure_id=failure_id,
      status="attempted",
      repaired_files=[],
      notes=[result.get("error", "Could not create the missing generated-test render helper.")],
    ))
    return format_repair_summary(failure_id, plan, [result], None, record)

  run_result = rerun_failure_tests(failure)
  status = "repaired" if run_result.get("ok") else "attempted"
  record = json.loads(tools.record_test_repair_attempt(
    repo_path=repo_path,
    failure_id=failure_id,
    status=status,
    repaired_files=[result.get("path")],
    notes=plan.get("notes", []),
  ))

  return format_repair_summary(failure_id, plan, [result], run_result, record)

def repair_missing_test_setup_file(failure_id, failure):
  failure_reason = failure.get("failureReason") or ""
  stale_setup_path = stale_missing_test_setup_path(failure_reason)
  if stale_setup_path:
    record = json.loads(tools.record_test_repair_attempt(
      repo_path=repo_path,
      failure_id=failure_id,
      status="blocked",
      repaired_files=[],
      notes=[
        (
          "Vitest is configured to load a setup file outside the target repo: "
          f"{stale_setup_path}. Update the test configuration to use a repo-relative "
          "setupFiles path such as src/test/setup.ts, or run the repair against the repo "
          "that owns that absolute path."
        ),
      ],
    ))
    plan = {
      "notes": [
        "Blocked repair because the missing setup file path points outside the configured repo.",
        f"Stale setup path: {stale_setup_path}",
      ],
    }
    return format_repair_summary(failure_id, plan, [], None, record)

  setup_path = extract_missing_test_setup_path(failure_reason)
  if not setup_path:
    return None

  result = json.loads(tools.write_test_support_file(
    repo_path=repo_path,
    file_path=setup_path,
    content=test_setup_file_content(setup_path),
    overwrite=False,
  ))
  record_test_write_activity("write_test_support_file", setup_path, result)

  plan = {
    "notes": [
      f"Created missing Vitest setup file {setup_path}.",
      "Added jest-dom matcher setup for Testing Library assertions.",
    ],
  }

  if not result.get("ok"):
    record = json.loads(tools.record_test_repair_attempt(
      repo_path=repo_path,
      failure_id=failure_id,
      status="attempted",
      repaired_files=[],
      notes=[result.get("error", "Could not create the missing test setup file.")],
    ))
    return format_repair_summary(failure_id, plan, [result], None, record)

  run_result = rerun_failure_tests(failure)
  status = "repaired" if run_result.get("ok") else "attempted"
  record = json.loads(tools.record_test_repair_attempt(
    repo_path=repo_path,
    failure_id=failure_id,
    status=status,
    repaired_files=[result.get("path")],
    notes=plan.get("notes", []),
  ))

  return format_repair_summary(failure_id, plan, [result], run_result, record)

def extract_visible_redirect_text(failure_text):
  if not failure_text:
    return None

  preferred = re.search(r"Redirecting to [^<\n]+", failure_text)
  if preferred:
    return preferred.group(0).strip()

  text_match = re.search(r">\s*([^<>\n]*Redirecting[^<>\n]*)\s*<", failure_text)
  if text_match:
    return text_match.group(1).strip()

  return None

def extract_component_import(content):
  match = re.search(
    r"import\s+(?P<name>[A-Z][A-Za-z0-9_]*)\s+from\s+['\"](?P<path>[^'\"]+)['\"]",
    content or "",
  )
  if not match:
    return None

  return {
    "name": match.group("name"),
    "path": match.group("path"),
  }

def source_path_from_component_import(import_path):
  if not import_path:
    return None

  if import_path.startswith("@/"):
    base_path = "src/" + import_path.removeprefix("@/")
    repo_root = Path(repo_path).resolve()
    for suffix in [".tsx", ".ts", ".jsx", ".js"]:
      candidate = base_path + suffix
      if (repo_root / candidate).exists():
        return candidate

    return base_path + ".tsx"

  return None

def read_target_repo_text(relative_path):
  if not relative_path:
    return ""

  try:
    repo_root = Path(repo_path).resolve()
    target_file = (repo_root / relative_path).resolve()
    target_file.relative_to(repo_root)
  except ValueError:
    return ""

  if not target_file.exists():
    return ""

  return target_file.read_text(encoding="utf-8", errors="replace")

def calendar_redirect_repair_content(component_import):
  component_name = component_import["name"]
  component_path = component_import["path"]

  return f"""import {{ screen }} from '@testing-library/react';
import {{ describe, expect, it, vi }} from 'vitest';
import {{ renderWithAppProviders }} from '@/test/utils/render';
import {component_name} from '{component_path}';

vi.mock('@/lib/appConfig', () => ({{
  appConfig: {{
    functionUrl: vi.fn(() => '#calendar-feed'),
  }},
}}));

describe('{component_name}', () => {{
  it('shows the redirect status without leaving jsdom', () => {{
    renderWithAppProviders(<{component_name} />);

    expect(screen.getByText('Redirecting to calendar feed...')).toBeInTheDocument();
  }});
}});
"""

def auth_smoke_repair_content(component_import):
  component_name = component_import["name"]
  component_path = component_import["path"]

  return f"""import {{ screen }} from '@testing-library/react';
import type {{ User }} from '@supabase/supabase-js';
import {{ describe, expect, it }} from 'vitest';
import {{ renderWithAppProviders }} from '@/test/utils/render';
import {component_name} from '{component_path}';

const signedInUser = {{
  id: 'test-user',
  email: 'member@example.com',
}} as User;

describe('{component_name}', () => {{
  it('renders for signed-in members', () => {{
    renderWithAppProviders(<{component_name} />, {{
      auth: {{ user: signedInUser }},
    }});

    expect(screen.getAllByRole('heading').length).toBeGreaterThan(0);
  }});
}});
"""

def normalize_testing_library_screen_import(content):
  normalized = content or ""

  def remove_screen_from_vitest(match):
    names = [
      item.strip()
      for item in match.group("names").split(",")
      if item.strip() and item.strip() != "screen"
    ]

    if not names:
      return ""

    return f"import {{ {', '.join(names)} }} from {match.group('quote')}vitest{match.group('quote')};"

  normalized = re.sub(
    r"import\s*{\s*(?P<names>[^}]+)\s*}\s*from\s*(?P<quote>['\"])vitest(?P=quote)\s*;",
    remove_screen_from_vitest,
    normalized,
  )

  testing_library_import = re.search(
    r"import\s*{\s*(?P<names>[^}]+)\s*}\s*from\s*(?P<quote>['\"])@testing-library/react(?P=quote)\s*;",
    normalized,
  )

  if testing_library_import:
    names = [
      item.strip()
      for item in testing_library_import.group("names").split(",")
      if item.strip()
    ]
    if "screen" not in names:
      names.append("screen")
      replacement = (
        f"import {{ {', '.join(names)} }} from "
        f"{testing_library_import.group('quote')}@testing-library/react{testing_library_import.group('quote')};"
      )
      normalized = (
        normalized[:testing_library_import.start()]
        + replacement
        + normalized[testing_library_import.end():]
      )
  else:
    normalized = "import { screen } from '@testing-library/react';\n" + normalized

  normalized = re.sub(r"\n{3,}", "\n\n", normalized)
  return normalized if normalized.endswith("\n") else normalized + "\n"

def route_outlet_repair_content(component_import):
  component_name = component_import["name"]
  component_path = component_import["path"]

  return f"""import {{ screen }} from '@testing-library/react';
import {{ Route, Routes }} from 'react-router-dom';
import {{ describe, expect, it }} from 'vitest';
import {{ renderWithAppProviders }} from '@/test/utils/render';
import {component_name} from '{component_path}';

describe('{component_name}', () => {{
  it('renders nested route content', () => {{
    renderWithAppProviders(
      <Routes>
        <Route path="/test-route" element={{<{component_name} />}}>
          <Route index element={{<h1>Nested route content</h1>}} />
        </Route>
      </Routes>,
      {{ route: '/test-route' }},
    );

    expect(screen.getByRole('heading', {{ name: 'Nested route content' }})).toBeInTheDocument();
  }});
}});
"""

def deterministic_full_file_repair(path, content, note):
  return {
    "writes": [
      {
        "action": "create",
        "filePath": path,
        "content": content,
        "overwrite": True,
      }
    ],
    "registryUpdates": [],
    "suiteIdsToRun": [],
    "skipped": [],
    "notes": [note],
  }

def error_boundary_repair_content():
  return """import { beforeEach, describe, expect, it, vi } from 'vitest';

const { reportError, recoverFromChunkLoadError } = vi.hoisted(() => ({
  reportError: vi.fn(),
  recoverFromChunkLoadError: vi.fn(),
}));

vi.mock('@/lib/errorReporting', () => ({
  reportError,
}));

vi.mock('@/lib/chunkLoadRecovery', () => ({
  isChunkLoadError: (error: unknown) => String((error as Error)?.message || error).includes('dynamically imported module'),
  recoverFromChunkLoadError,
}));

import { ErrorBoundary } from './ErrorBoundary';

describe('ErrorBoundary chunk recovery', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    reportError.mockReset();
    recoverFromChunkLoadError.mockReset();
  });

  it('does not report a recoverable stale deployment chunk as fatal', () => {
    const boundary = new ErrorBoundary({ children: null });
    const error = new TypeError(
      'Failed to fetch dynamically imported module: https://www.fiesta.community/assets/StoriesPage-old.js',
    );

    boundary.componentDidCatch(error, { componentStack: 'at Lazy' });

    expect(recoverFromChunkLoadError).toHaveBeenCalledWith(error);
    expect(reportError).not.toHaveBeenCalled();
  });

  it('still reports genuine render errors', () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const boundary = new ErrorBoundary({ children: null });
    const error = new Error('Render failed');

    boundary.componentDidCatch(error, { componentStack: 'at StoriesPage' });

    expect(consoleErrorSpy).toHaveBeenCalledWith('Route render error:', error);
    expect(reportError).toHaveBeenCalledWith(error, expect.objectContaining({
      source: 'react_error_boundary',
      severity: 'fatal',
    }));
  });
});
"""

def deterministic_repair_plan(repair_context):
  failure = repair_context.get("failure", {})
  failure_reason = failure.get("failureReason") or ""
  normalized_failure = failure_reason.lower()
  failing_test_files = [
    normalized_repair_path_key(path)
    for path in tools.infer_failing_test_files_from_failure_text(failure_reason)
  ]
  visible_text = extract_visible_redirect_text(failure_reason)
  failure_test_files = failure.get("testFiles", [])
  if not visible_text and any("CalendarRedirectPage" in path for path in failure_test_files):
    visible_text = "Redirecting to calendar feed..."

  readable_previews = [
    preview
    for preview in repair_context.get("testFilePreviews", [])
    if preview.get("ok") and preview.get("path")
  ]
  previews = readable_previews or [
    preview
    for preview in repair_context.get("testFilePreviews", [])
    if preview.get("path")
  ]
  if failing_test_files:
    previews = sorted(
      previews,
      key=lambda preview: (
        normalized_repair_path_key(preview.get("path")) not in failing_test_files,
        normalized_repair_path_key(preview.get("path")),
      ),
    )

  for preview in previews:
    path = preview.get("path")
    content = preview.get("contentPreview") or ""

    if (
      path
      and normalized_repair_path_key(path) == "src/components/errorboundary.test.tsx"
      and "render failed" in normalized_failure
      and "componentDidCatch" in content
    ):
      return deterministic_full_file_repair(
        path,
        error_boundary_repair_content(),
        "Rewrote the ErrorBoundary test to suppress and assert the expected console.error from the genuine render-error case.",
      )

    component_import = extract_component_import(content)
    source_path = source_path_from_component_import(component_import["path"]) if component_import else None
    source_text = read_target_repo_text(source_path)

    if (
      path
      and "screen." in content
      and re.search(r"screen\.get(?:all)?by[a-z0-9_]* is not a function", normalized_failure)
    ):
      if component_import and re.search(r"\bOutlet\b|<Outlet\b", source_text):
        return deterministic_full_file_repair(
          path,
          route_outlet_repair_content(component_import),
          "Rewrote the route-layout test to render nested route content and import screen from Testing Library.",
        )

      return deterministic_full_file_repair(
        path,
        normalize_testing_library_screen_import(content),
        "Normalized the generated test imports so screen comes from Testing Library.",
      )

    if (
      path
      and component_import
      and "not implemented: navigation" in normalized_failure
      and "window.location.href" in source_text
      and "appConfig.functionUrl" in source_text
    ):
      return deterministic_full_file_repair(
        path,
        calendar_redirect_repair_content(component_import),
        "Rewrote the generated redirect-page smoke test to use a jsdom-safe mocked redirect URL and assert visible status text.",
      )

    if (
      path
      and component_import
      and "received value must be an htmlelement" in normalized_failure
      and "container.firstChild" in content
      and "useAuth()" in source_text
      and "<Navigate" in source_text
    ):
      return deterministic_full_file_repair(
        path,
        auth_smoke_repair_content(component_import),
        "Rewrote the generated smoke test to render with an authenticated test user and assert a visible heading.",
      )

    if (
      path
      and component_import
      and "received value must be an htmlelement" in normalized_failure
      and "container.firstChild" in content
      and re.search(r"\bOutlet\b|<Outlet\b", source_text)
    ):
      return deterministic_full_file_repair(
        path,
        route_outlet_repair_content(component_import),
        "Rewrote the generated route-layout smoke test to render nested route content through the Outlet.",
      )

    if not path or not visible_text:
      continue

    replacements = [
      (
        "expect(screen.getByRole('generic')).toBeInTheDocument();",
        f"expect(screen.getByText({quote_js_string(visible_text)})).toBeInTheDocument();",
      ),
      (
        'expect(screen.getByRole("generic")).toBeInTheDocument();',
        f"expect(screen.getByText({quote_js_string(visible_text)})).toBeInTheDocument();",
      ),
      (
        "expect(screen.getAllByRole('generic')[0]).toBeInTheDocument();",
        f"expect(screen.getByText({quote_js_string(visible_text)})).toBeInTheDocument();",
      ),
      (
        'expect(screen.getAllByRole("generic")[0]).toBeInTheDocument();',
        f"expect(screen.getByText({quote_js_string(visible_text)})).toBeInTheDocument();",
      ),
      (
        "screen.getByRole('generic')",
        f"screen.getByText({quote_js_string(visible_text)})",
      ),
      (
        'screen.getByRole("generic")',
        f"screen.getByText({quote_js_string(visible_text)})",
      ),
      (
        "screen.getAllByRole('generic')[0]",
        f"screen.getByText({quote_js_string(visible_text)})",
      ),
      (
        'screen.getAllByRole("generic")[0]',
        f"screen.getByText({quote_js_string(visible_text)})",
      ),
    ]

    for find_text, replace_text in replacements:
      if find_text in content:
        return {
          "writes": [
            {
              "action": "patch",
              "filePath": path,
              "findText": find_text,
              "replaceText": replace_text,
            }
          ],
          "registryUpdates": [],
          "suiteIdsToRun": [],
          "skipped": [],
          "notes": [
            "Replaced a generic DOM query with a stable visible-text assertion from the rendered output."
          ],
        }

  return None

def uses_unsafe_generic_query(text):
  if not text:
    return False

  return bool(re.search(r"\bget(?:All)?ByRole\(\s*['\"]generic['\"]", text))

def repair_plan_validation_errors(plan, allowed_test_files, repair_context=None):
  errors = []
  allowed = {
    normalized_repair_path_key(path)
    for path in allowed_test_files
    if path
  }
  failing_files = set()
  if repair_context:
    failing_files = {
      normalized_repair_path_key(path)
      for path in repair_context.get("failure", {}).get("failingTestFiles", [])
      if path
    }

  for write in plan.get("writes", []):
    if write.get("action") not in ("create", "patch"):
      errors.append(f"Unsupported write action: {write.get('action')}")
      continue

    file_path = write.get("filePath")
    file_key = normalized_repair_path_key(file_path)
    if allowed and file_key not in allowed:
      errors.append(f"Patch target is not one of the captured test files: {file_path}")
    if failing_files and file_key not in failing_files:
      errors.append(f"Patch target is not one of the failing test files: {file_path}")

    if write.get("action") == "create":
      if not write.get("overwrite", False):
        errors.append("Create repair actions must overwrite an existing broken test file.")
      if tools.validate_test_file_content(write.get("content", "")):
        errors.append("Replacement test content failed validation.")
      if uses_unsafe_generic_query(write.get("content", "")):
        errors.append("Replacement test content still uses a generic-role query.")

    if write.get("action") == "patch":
      if not write.get("findText"):
        errors.append("Patch repair actions must include exact findText.")
      if write.get("replaceText") is None:
        errors.append("Patch repair actions must include replaceText.")
      if uses_unsafe_generic_query(write.get("replaceText", "")):
        errors.append("Patch replacement still uses a generic-role query.")

  if not plan.get("writes"):
    errors.append("Repair plan did not include any test-file writes.")

  return errors

def repair_plan_is_usable(plan, allowed_test_files, repair_context=None):
  return not repair_plan_validation_errors(plan, allowed_test_files, repair_context)

def get_test_repair_plan(repair_context):
  prompt = f"""
Create a concrete JSON plan to repair a generated test failure.

Return only valid JSON. Do not use markdown. Do not explain.

JSON shape:
{{
  "writes": [
    {{
      "action": "patch",
      "filePath": "src/example.test.tsx",
      "findText": "exact existing text",
      "replaceText": "exact replacement text"
    }}
  ],
  "registryUpdates": [],
  "suiteIdsToRun": [],
  "skipped": [],
  "notes": []
}}

Rules:
- Only patch or overwrite files listed in testFilePreviews.
- If failure.failingTestFiles is non-empty, patch the failing file first and do not patch unrelated suite files.
- Prefer patch actions with exact findText and replaceText.
- Only use create actions with overwrite true when replacing a broken test file entirely.
- Do not patch product source files.
- Do not remove meaningful assertions to make tests pass.
- Do not replace the test with a vague smoke-only assertion.
- Do not keep or introduce container.firstChild-only assertions, hook-as-JSX tests, or lowercase JSX pseudo-component tests.
- Hooks must use renderHook; utilities must be called directly; components must assert visible state, interactions, callbacks, or navigation.
- Do not change product behavior expectations.
- If classification.repairSafe is false for a known product expectation mismatch, return writes as [] and explain in skipped.
- If classification is unknown, inspect the related test and failure text and propose only conservative test-mechanics repairs.
- For jsdom navigation errors, mock or isolate unsupported navigation instead of allowing real navigation.
- For Testing Library multiple-element errors, replace vague queries with stable user-facing or scoped queries.
- For missing provider/context errors, add or use project-standard test wrappers instead of changing the component.
- For null container.firstChild smoke failures, inspect whether the page needs auth, route, providers, or async waits; add the missing setup and assert a stable visible role/text.
- Keep the test focused on the same intended behavior.

Repair context:
{json.dumps(repair_context, indent=2)}
"""

  return normalize_test_write_plan(get_test_write_plan(prompt))

def apply_repair_plan(plan, planner_context):
  write_results = []

  for write in plan.get("writes", []):
    action = write.get("action")
    file_path = write.get("filePath")

    if action == "create":
      content = normalize_generated_test_imports(
        write.get("content", ""),
        planner_context,
      )
      result = json.loads(tools.write_test_file(
        repo_path=repo_path,
        file_path=file_path,
        content=content,
        overwrite=bool(write.get("overwrite", False)),
      ))
    elif action == "patch":
      replace_text = normalize_generated_test_imports(
        write.get("replaceText", ""),
        planner_context,
      )
      result = json.loads(tools.patch_test_file(
        repo_path=repo_path,
        file_path=file_path,
        find_text=write.get("findText", ""),
        replace_text=replace_text,
      ))
    else:
      result = {
        "ok": False,
        "error": f"Unsupported write action: {action}",
        "path": file_path,
      }

    write_results.append(result)
    record_test_write_activity(
      "write_test_file" if action == "create" else "patch_test_file",
      file_path,
      result,
    )

  return write_results

def rerun_failure_tests(failure):
  suite_id = failure.get("suiteId")

  if suite_id == "all-tests":
    result = json.loads(tools.run_all_tests(repo_path=repo_path))
  elif suite_id and suite_id not in ("direct-test-files", "unknown-suite"):
    result = json.loads(tools.run_test_suite(
      repo_path=repo_path,
      suite_id=suite_id,
      registry_path=registry_path,
    ))
  else:
    test_files = failure.get("testFiles") or []
    result = json.loads(tools.run_test_files(
      repo_path=repo_path,
      test_files=test_files,
    ))

  record_test_run_activity(result)
  return result

def format_repair_summary(failure_id, plan, write_results, run_result, repair_record):
  changed = [
    result.get("path")
    for result in write_results
    if result.get("ok") and result.get("path")
  ]
  failed_writes = [
    result
    for result in write_results
    if not result.get("ok")
  ]

  return f"""Repair result for {failure_id}

Tests changed:
{chr(10).join(f"- {path}" for path in changed) if changed else "- None"}

Commands run:
{f"- {run_result.get('command')}" if run_result and run_result.get("command") else "- None"}

Final status:
- Test run: {"Passed" if run_result and run_result.get("ok") else "Failed" if run_result else "Not run"}
- Repair record: {repair_record.get("status") if isinstance(repair_record, dict) else "not recorded"}

Failed writes:
{chr(10).join(f"- {json.dumps(item)}" for item in failed_writes) if failed_writes else "- None"}

Notes:
{chr(10).join(f"- {item}" for item in plan.get("notes", [])) if plan.get("notes") else "- None"}
"""

def resolve_failure_id_for_fix(argument):
  if argument:
    failure_id = find_failure_id_for_argument(argument)
    if failure_id:
      return {
        "ok": True,
        "failureId": failure_id,
      }

    capture = run_tests_to_capture_failure(argument)
    if capture.get("ok"):
      return {
        "ok": True,
        "failureId": capture.get("failureId"),
      }

    return capture

  inspect_data = json.loads(tools.inspect_test_failure(
    repo_path=repo_path,
    failure_id=None,
  ))
  if inspect_data.get("ok"):
    return {
      "ok": True,
      "failureId": inspect_data.get("failure", {}).get("id"),
    }

  return {
    "ok": False,
    "message": inspect_data.get("error", "No captured test failure was found."),
  }

def fix_tests_for_failure(argument=None):
  resolution = resolve_failure_id_for_fix(argument)

  if not resolution.get("ok"):
    return resolution.get("message", "No captured test failure was found.")

  failure_id = resolution.get("failureId")
  inspect_json = tools.inspect_test_failure(
    repo_path=repo_path,
    failure_id=failure_id,
  )
  inspect_data = json.loads(inspect_json)

  if not inspect_data.get("ok"):
    return inspect_data.get("error", "Could not inspect test failure.")

  failure = inspect_data.get("failure", {})
  current_classification = tools.classify_failure_reason(failure.get("failureReason") or "")
  stored_classification = failure.get("classification") or {}
  classification = (
    current_classification
    if current_classification.get("classification") != "unknown"
    else stored_classification
  )
  failure["classification"] = classification

  setup_repair_result = repair_missing_test_setup_file(failure_id, failure)
  if setup_repair_result:
    return setup_repair_result

  render_helper_repair_result = repair_missing_test_render_helper(failure_id, failure)
  if render_helper_repair_result:
    return render_helper_repair_result

  jsdom_browser_api_repair_result = repair_missing_jsdom_browser_api(failure_id, failure)
  if jsdom_browser_api_repair_result:
    return jsdom_browser_api_repair_result

  readable_preview_files = [
    preview.get("path")
    for preview in inspect_data.get("testFilePreviews", [])
    if preview.get("ok") and preview.get("path")
  ]
  inferred_failing_test_files = tools.infer_failing_test_files_from_failure_text(failure.get("failureReason") or "")
  inferred_failing_file_keys = {
    normalized_repair_path_key(path)
    for path in inferred_failing_test_files
  }
  if inferred_failing_file_keys:
    narrowed_preview_files = [
      path
      for path in readable_preview_files
      if normalized_repair_path_key(path) in inferred_failing_file_keys
    ]
    if narrowed_preview_files:
      readable_preview_files = narrowed_preview_files

  allowed_test_files = list(dict.fromkeys(
    readable_preview_files or inferred_failing_test_files or (failure.get("testFiles") or [])
  ))

  classification_name = classification.get("classification")
  is_unknown_classification = classification_name in (None, "unknown")
  is_known_unsafe_classification = (
    not classification.get("repairSafe")
    and not is_unknown_classification
  )

  repair_context = build_repair_planner_context(inspect_data)
  repair_context["failure"]["classification"] = (
    {
      "classification": "unknown",
      "confidence": "low",
      "repairSafe": True,
      "guidance": (
        "Inspect the captured test and failure text. Only propose conservative "
        "test-mechanics repairs such as providers, routing, mocks, waits, or stable "
        "queries. Do not change product expectations."
      ),
    }
    if is_unknown_classification
    else classification
  )
  plan = deterministic_repair_plan(repair_context)

  if plan is not None:
    plan_errors = repair_plan_validation_errors(plan, allowed_test_files, repair_context)
    if not plan_errors:
      planner_context = {
        "changedFilePreviews": [],
      }
      write_results = apply_repair_plan(plan, planner_context)

      if any(not result.get("ok") for result in write_results):
        record = json.loads(tools.record_test_repair_attempt(
          repo_path=repo_path,
          failure_id=failure_id,
          status="attempted",
          repaired_files=[
            result.get("path")
            for result in write_results
            if result.get("ok") and result.get("path")
          ],
          notes=["One or more deterministic test patches failed to apply."],
        ))
        return format_repair_summary(failure_id, plan, write_results, None, record)

      run_result = rerun_failure_tests(failure)
      status = "repaired" if run_result.get("ok") else "attempted"
      record = json.loads(tools.record_test_repair_attempt(
        repo_path=repo_path,
        failure_id=failure_id,
        status=status,
        repaired_files=[
          result.get("path")
          for result in write_results
          if result.get("ok") and result.get("path")
        ],
        notes=plan.get("notes", []),
      ))
      return format_repair_summary(failure_id, plan, write_results, run_result, record)

  if is_known_unsafe_classification:
    record = json.loads(tools.record_test_repair_attempt(
      repo_path=repo_path,
      failure_id=failure_id,
      status="blocked",
      repaired_files=[],
      notes=[classification.get("guidance", "Failure is not classified as safe for automatic repair.")],
    ))
    return (
      f"Blocked repair for {failure_id}: "
      f"{classification.get('guidance', 'Failure is not safe for automatic repair.')}\n\n"
      f"Repair record: {json.dumps(record, indent=2)}"
    )

  if not allowed_test_files:
    record = json.loads(tools.record_test_repair_attempt(
      repo_path=repo_path,
      failure_id=failure_id,
      status="blocked",
      repaired_files=[],
      notes=["No related test files were captured for this failure."],
    ))
    return (
      f"Blocked repair for {failure_id}: no related test files were captured.\n\n"
      f"Repair record: {json.dumps(record, indent=2)}"
    )

  plan = get_test_repair_plan(repair_context)

  plan_errors = repair_plan_validation_errors(plan, allowed_test_files, repair_context)
  if plan_errors:
    record = json.loads(tools.record_test_repair_attempt(
      repo_path=repo_path,
      failure_id=failure_id,
      status="blocked",
      repaired_files=[],
      notes=plan.get("skipped", []) or plan_errors,
    ))
    return (
      f"Blocked repair for {failure_id}: no safe patch was produced.\n\n"
      f"Reasons:\n{markdown_list(plan_errors)}\n\n"
      f"Skipped:\n{markdown_list(plan.get('skipped', []))}\n\n"
      f"Repair record: {json.dumps(record, indent=2)}"
    )

  planner_context = {
    "changedFilePreviews": [],
  }
  write_results = apply_repair_plan(plan, planner_context)

  if any(not result.get("ok") for result in write_results):
    record = json.loads(tools.record_test_repair_attempt(
      repo_path=repo_path,
      failure_id=failure_id,
      status="attempted",
      repaired_files=[
        result.get("path")
        for result in write_results
        if result.get("ok") and result.get("path")
      ],
      notes=["One or more planned test patches failed to apply."],
    ))
    return format_repair_summary(failure_id, plan, write_results, None, record)

  run_result = rerun_failure_tests(failure)
  status = "repaired" if run_result.get("ok") else "attempted"
  record = json.loads(tools.record_test_repair_attempt(
    repo_path=repo_path,
    failure_id=failure_id,
    status=status,
    repaired_files=[
      result.get("path")
      for result in write_results
      if result.get("ok") and result.get("path")
    ],
    notes=plan.get("notes", []),
  ))

  return format_repair_summary(failure_id, plan, write_results, run_result, record)


########################################################

while True:
  user_input = input("\n> ")
  normalized_input = user_input.lower().strip()

  if normalized_input in ("quit", "exit"):
    break
  if normalized_input == "doctor":
    result = tools.validate_project_access(
      repo_path=repo_path,
      registry_path=registry_path,
      write_probe=False,
    )
  elif normalized_input == "doctor-write":
    result = tools.validate_project_access(
      repo_path=repo_path,
      registry_path=registry_path,
      write_probe=True,
    )
  # if user_input.lower() == ("run"):
  #   result = summary_agent(user_input)
  elif normalized_input in ("failures", "list-failures", "test-failures"):
    result = format_test_failure_list(
      tools.list_test_failures(repo_path=repo_path)
    )
  elif normalized_input.startswith("inspect-failure"):
    result = inspect_failure_command(command_argument(user_input))
  elif normalized_input.startswith("fix-tests") or normalized_input.startswith("fix-test"):
    result = fix_tests_for_failure(command_argument(user_input))
  elif normalized_input in ("clear-failures", "clear-test-failures"):
    result = tools.clear_test_failures(repo_path=repo_path)
  elif normalized_input in ("test-all", "all-tests", "run-all-tests", "test all", "run all tests"):
    result = run_all_tests_command()
  elif normalized_input == "test-diff" or normalized_input.startswith("test-diff ") or normalized_input == "diff-tests" or normalized_input.startswith("diff-tests "):
    result = run_tests_for_git_diff(command_argument(user_input) or "HEAD")
  elif normalized_input == "create-missing-tests" or normalized_input.startswith("create-missing-tests "):
    result = create_missing_tests_command(command_argument(user_input))
  elif normalized_input.startswith("simulate "):
    changed_files = parse_changed_files_argument(command_argument(user_input))
    result = run_for_changed_files(changed_files)
  elif normalized_input.startswith("diff "):
    changed_files = parse_changed_files_argument(command_argument(user_input))
    result = tools.get_changed_file_diff(
      repo_path=repo_path,
      changed_files=changed_files,
    )
  elif normalized_input.startswith("edit-tests ") or normalized_input.startswith("update-tests "):
    changed_files = parse_changed_files_argument(command_argument(user_input))
    result = edit_tests_for_changed_files(changed_files)
  elif normalized_input.startswith("create-tests "):
    changed_files = parse_changed_files_argument(command_argument(user_input))
    result = create_tests_for_changed_files(changed_files)
  elif normalized_input.startswith("smoke-write-test "):
    file_paths = tools.parse_file_paths_argument(command_argument(user_input))

    reset_test_creation_audit()

    write_results = []

    for file_path in file_paths:
      write_result = tools.write_test_file(
        repo_path=repo_path,
        file_path=file_path,
        content="""import { describe, expect, it } from 'vitest';

  describe('smoke test', () => {
    it('writes test files through the guarded tool', () => {
      expect(true).toBe(true);
    });
  });
  """,
        overwrite=False,
      )
      write_results.append(json.loads(write_result))

    audit = load_test_creation_audit()

    Path("output.md").write_text(
      json.dumps({
        "writeResult": write_results[0] if len(write_results) == 1 else None,
        "writeResults": write_results,
        "audit": audit,
      }, indent=2),
      encoding="utf-8",
    )

    continue
  else: 
    result = agent(user_input)  
  Path("output.md").write_text(str(result), encoding="utf-8")
