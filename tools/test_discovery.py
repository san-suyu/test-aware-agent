from strands import tool
from fnmatch import fnmatch
from pathlib import Path
import json

from .shared import (
  import_export_mismatch_errors,
  source_test_intent,
  validate_test_file_content,
)

@tool
def find_relevant_test_suites(
  repo_path: str,
  changed_files: list[str],
  registry_path: str = "src/test/test-registry.json"
) -> str:
  """Find test suites that should run for the given changed files in a target repo."""

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

  normalized_changed_files = [
    Path(file).as_posix()
    for file in changed_files
  ]

  matched_suites = []

  for suite in registry.get("suites", []):
    patterns = suite.get("runWhenChanged", [])

    matching_files = [
      file
      for file in normalized_changed_files
      if any(fnmatch(file, pattern) for pattern in patterns)
    ]

    if matching_files:
      matched_suites.append({
        "id": suite["id"],
        "name": suite.get("name", suite["id"]),
        # "kind": suite.get("kind"),
        "command": suite["command"],
        "matchedFiles": matching_files
      })

  return json.dumps({
  "ok": True,
  "matchedSuiteCount": len(matched_suites),
  "repoPath": str(repo_root),
  "registryPath": str(registry_file),
  "changedFiles": normalized_changed_files,
  "matchedSuites": matched_suites,
  "reason": f"Matched suite count: {len(matched_suites)}",
}, indent=2)

@tool
def inspect_test_context(
  repo_path: str,
  changed_files: list[str],
  registry_path: str = "src/test/test-registry.json"
) -> str:
  """Collect safe read-only context needed to plan tests for changed feature files."""

  if not registry_path:
    registry_path = "src/test/test-registry.json"

  if isinstance(changed_files, str):
    changed_files = json.loads(changed_files)

  repo_root = Path(repo_path).resolve()
  registry_file = (repo_root / registry_path).resolve()

  def is_inside_repo(path: Path) -> bool:
    try:
      path.resolve().relative_to(repo_root)
      return True
    except ValueError:
      return False

  def read_preview(path: Path, max_chars: int = 6000) -> dict:
    if not path.exists() or not path.is_file():
      return {
        "path": path.relative_to(repo_root).as_posix() if is_inside_repo(path) else str(path),
        "exists": False,
        "contentPreview": None,
      }

    text = path.read_text(encoding="utf-8", errors="replace")

    return {
      "path": path.relative_to(repo_root).as_posix(),
      "exists": True,
      "sizeBytes": path.stat().st_size,
      "contentPreview": text[:max_chars],
      "truncated": len(text) > max_chars,
    }

  def is_test_file(path: Path) -> bool:
    name = path.name.lower()
    return (
      ".test." in name
      or ".spec." in name
      or path.parts[-2:] and "__tests__" in path.parts
    )

  def shared_segments_score(source: Path, test_file: Path) -> int:
    source_parts = set(source.with_suffix("").parts)
    test_parts = set(test_file.with_suffix("").parts)
    score = len(source_parts.intersection(test_parts))

    if source.stem.lower() in test_file.stem.lower():
      score += 5

    if source.parent == test_file.parent:
      score += 3

    return score

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  if not is_inside_repo(registry_file):
    return json.dumps({
      "ok": False,
      "error": f"registry_path is outside repo: {registry_file}",
    }, indent=2)

  normalized_changed_files = []
  changed_file_previews = []
  test_intent_by_file = {}

  for raw_file in changed_files:
    candidate = Path(raw_file)

    if candidate.is_absolute():
      resolved = candidate.resolve()
    else:
      resolved = (repo_root / candidate).resolve()

    if not is_inside_repo(resolved):
      return json.dumps({
        "ok": False,
        "error": f"changed file is outside repo: {resolved}",
      }, indent=2)

    relative_path = resolved.relative_to(repo_root).as_posix()
    normalized_changed_files.append(relative_path)
    preview = read_preview(resolved)
    changed_file_previews.append(preview)
    if preview.get("exists"):
      test_intent_by_file[relative_path] = source_test_intent(
        relative_path,
        preview.get("contentPreview") or "",
      )

  registry = {}
  matched_suites = []
  all_suites = []

  if registry_file.exists() and registry_file.is_file():
    registry = json.loads(registry_file.read_text(encoding="utf-8"))

    for suite in registry.get("suites", []):
      all_suites.append({
        "id": suite.get("id"),
        "name": suite.get("name", suite.get("id")),
        "type": suite.get("type", suite.get("kind")),
        "command": suite.get("command"),
        "cost": suite.get("cost"),
        "covers": suite.get("covers", []),
        "files": [
          Path(file_path).as_posix()
          for file_path in suite.get("files", [])
        ],
        "runWhenChanged": [
          Path(pattern).as_posix()
          for pattern in suite.get("runWhenChanged", [])
        ],
        "testLocations": [
          Path(location).as_posix()
          for location in suite.get("testLocations", [])
        ],
      })

      patterns = suite.get("runWhenChanged", [])

      matching_files = [
        file
        for file in normalized_changed_files
        if any(fnmatch(file, pattern) for pattern in patterns)
      ]

      if matching_files:
        matched_suites.append({
          "id": suite.get("id"),
          "name": suite.get("name", suite.get("id")),
          "type": suite.get("type", suite.get("kind")),
          "command": suite.get("command"),
          "cost": suite.get("cost"),
          "covers": suite.get("covers", []),
          "files": [
            Path(file_path).as_posix()
            for file_path in suite.get("files", [])
          ],
          "matchedFiles": matching_files,
          "testLocations": suite.get("testLocations", []),
        })

  all_test_files = [
    path
    for path in repo_root.rglob("*")
    if path.is_file()
    and is_test_file(path)
    and "node_modules" not in path.parts
    and "dist" not in path.parts
    and "build" not in path.parts
  ]

  nearby_tests_by_changed_file = []

  for raw_file in normalized_changed_files:
    source_file = repo_root / raw_file

    scored_tests = sorted(
      [
        {
          "path": test_file.relative_to(repo_root).as_posix(),
          "score": shared_segments_score(source_file, test_file),
        }
        for test_file in all_test_files
      ],
      key=lambda item: item["score"],
      reverse=True,
    )

    nearby_tests_by_changed_file.append({
      "changedFile": raw_file,
      "nearbyTests": [
        item
        for item in scored_tests[:10]
        if item["score"] > 0
      ],
    })

  nearby_test_paths = []
  for group in nearby_tests_by_changed_file:
    for test in group["nearbyTests"]:
      if test["path"] not in nearby_test_paths:
        nearby_test_paths.append(test["path"])

  nearby_test_previews = [
    read_preview(repo_root / path, max_chars=8000)
    for path in nearby_test_paths[:10]
  ]

  matched_suite_test_paths = []
  for suite in matched_suites:
    for file_path in suite.get("files", []):
      normalized_file_path = Path(file_path).as_posix()
      if normalized_file_path not in matched_suite_test_paths:
        matched_suite_test_paths.append(normalized_file_path)

  matched_suite_test_previews = [
    read_preview(repo_root / path, max_chars=8000)
    for path in matched_suite_test_paths[:12]
  ]

  helper_candidates = []

  for path in repo_root.rglob("*"):
    if not path.is_file():
      continue

    relative = str(path.relative_to(repo_root)).replace("\\", "/")
    lower_name = path.name.lower()

    if "node_modules" in path.parts or "dist" in path.parts or "build" in path.parts:
      continue

    if (
      "test-utils" in lower_name
      or "setupTests" in path.name
      or "setup-tests" in lower_name
      or "__mocks__" in path.parts
      or "mock" in lower_name
      or "fixture" in lower_name
    ):
      helper_candidates.append(relative)

  candidate_test_files = []

  for raw_file in normalized_changed_files:
    source_file = Path(raw_file)
    stem = source_file.stem
    suffix = source_file.suffix

    if suffix in [".tsx", ".jsx"]:
      test_suffix = ".test.tsx"
    else:
      test_suffix = ".test.ts"

    if len(source_file.parts) > 2 and source_file.parts[0] == "src":
      candidate_test_files.append(Path("src/test").joinpath(*source_file.parts[1:-1], f"{stem}{test_suffix}").as_posix())

    if len(source_file.parts) > 1:
      candidate_test_files.append((Path("src/test") / source_file.parent.name / f"{stem}{test_suffix}").as_posix())

    candidate_test_files.append(source_file.with_name(f"{stem}{test_suffix}").as_posix())

  candidate_test_files = list(dict.fromkeys(candidate_test_files))
  candidate_test_previews = [
    read_preview(repo_root / path, max_chars=8000)
    for path in candidate_test_files
  ]

  invalid_candidate_tests = []
  for preview in candidate_test_previews:
    if not preview.get("exists"):
      continue

    preview_content = preview.get("contentPreview") or ""
    validation_errors = (
      validate_test_file_content(preview_content)
      + import_export_mismatch_errors(preview_content, changed_file_previews)
    )
    if validation_errors:
      invalid_candidate_tests.append({
        "path": preview.get("path"),
        "validationErrors": validation_errors,
        "contentPreview": preview.get("contentPreview"),
      })

  registered_test_files = []
  for suite in registry.get("suites", []):
    for file_path in suite.get("files", []):
      normalized_file_path = Path(file_path).as_posix()
      if normalized_file_path not in registered_test_files:
        registered_test_files.append(normalized_file_path)

  unregistered_candidate_tests = [
    preview.get("path")
    for preview in candidate_test_previews
    if preview.get("exists") and preview.get("path") not in registered_test_files
  ]

  return json.dumps({
    "ok": True,
    "repoPath": str(repo_root),
    "registryPath": str(registry_file),
    "changedFiles": normalized_changed_files,
    "changedFilePreviews": changed_file_previews,
    "testIntentByFile": test_intent_by_file,
    "matchedSuites": matched_suites,
    "allSuites": all_suites,
    "nearbyTestsByChangedFile": nearby_tests_by_changed_file,
    "nearbyTestPreviews": nearby_test_previews,
    "matchedSuiteTestPreviews": matched_suite_test_previews,
    "candidateTestFiles": candidate_test_files,
    "candidateTestPreviews": candidate_test_previews,
    "invalidCandidateTests": invalid_candidate_tests,
    "unregisteredCandidateTests": unregistered_candidate_tests,
    "existingHelpers": helper_candidates[:25],
    "notes": [
      "This tool is read-only.",
      "Use nearbyTestPreviews to copy project test style.",
      "Use testIntentByFile to decide whether a new test is worthwhile and what behavior to assert.",
      "Use candidateTestPreviews to repair an existing generated test or create a missing test.",
      "invalidCandidateTests must be replaced with real Vitest tests.",
      "unregisteredCandidateTests must be added to the registry.",
      "Prefer editing an existing nearby test when it already covers the changed area.",
      "Only create a new test file when no existing test location fits."
    ],
  }, indent=2)
