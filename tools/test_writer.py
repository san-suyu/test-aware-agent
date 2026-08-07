from strands import tool
from fnmatch import fnmatch
from pathlib import Path
import json
import subprocess

from .shared import (
  normalize_test_import_aliases,
  source_test_intent,
  validate_test_file_content,
)

ALLOWED_TEST_PATTERNS = [
  "**/*.test.ts",
  "**/*.test.tsx",
  "**/*.test.js",
  "**/*.test.jsx",
  "**/*.spec.ts",
  "**/*.spec.tsx",
  "**/*.spec.js",
  "**/*.spec.jsx",
  "src/test/**",
  "tests/**",
  "__tests__/**",
  "**/__tests__/**",
]

SOURCE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}

SOURCE_SCAN_SKIP_DIRS = {
  ".cache",
  ".git",
  ".next",
  ".test-agent",
  ".turbo",
  "__mocks__",
  "__tests__",
  "build",
  "coverage",
  "dist",
  "node_modules",
  "out",
  "tests",
  "vendor",
}

SOURCE_SCAN_SKIP_NAME_PARTS = [
  ".fixture.",
  ".mock.",
  "fixture",
  "mock",
  "setup-test",
  "setupTests",
  "test-utils",
]

def is_allowed_test_path(relative_posix: str) -> bool:
  return any(
    fnmatch(relative_posix, pattern)
    for pattern in ALLOWED_TEST_PATTERNS
  )

def candidate_test_paths_for_source(source_path: str) -> list[str]:
  source = Path(source_path).as_posix()
  source_path_obj = Path(source)
  suffix = ".test.tsx" if source_path_obj.suffix in [".tsx", ".jsx"] else ".test.ts"
  spec_suffix = ".spec.tsx" if source_path_obj.suffix in [".tsx", ".jsx"] else ".spec.ts"
  candidates = []

  if len(source_path_obj.parts) > 2 and source_path_obj.parts[0] == "src":
    candidates.append(Path("src/test").joinpath(*source_path_obj.parts[1:-1], f"{source_path_obj.stem}{suffix}").as_posix())
    candidates.append(Path("src/test").joinpath(*source_path_obj.parts[1:-1], f"{source_path_obj.stem}{spec_suffix}").as_posix())

  if len(source_path_obj.parts) > 1:
    candidates.append((Path("src/test") / source_path_obj.parent.name / f"{source_path_obj.stem}{suffix}").as_posix())
    candidates.append((Path("src/test") / source_path_obj.parent.name / f"{source_path_obj.stem}{spec_suffix}").as_posix())

  candidates.append(source_path_obj.with_name(f"{source_path_obj.stem}{suffix}").as_posix())
  candidates.append(source_path_obj.with_name(f"{source_path_obj.stem}{spec_suffix}").as_posix())

  return list(dict.fromkeys(candidates))

def test_subject_stem(test_path: str) -> str:
  stem = Path(test_path).stem
  for suffix in [".test", ".spec"]:
    if stem.endswith(suffix):
      return stem[:-len(suffix)]
  return stem

def is_test_file_path(path: str) -> bool:
  name = Path(path).name.lower()
  return (
    ".test." in name
    or ".spec." in name
    or "__tests__" in Path(path).parts
  )

def is_source_scan_candidate(path: Path, repo_root: Path) -> bool:
  relative = path.relative_to(repo_root)
  relative_posix = relative.as_posix()
  suffix = path.suffix.lower()
  name = path.name
  lower_name = name.lower()
  parts = set(relative.parts)

  if suffix not in SOURCE_EXTENSIONS:
    return False

  if lower_name.endswith(".d.ts") or is_test_file_path(relative_posix):
    return False

  if parts.intersection(SOURCE_SCAN_SKIP_DIRS):
    return False

  if len(relative.parts) >= 2 and relative.parts[0] == "src" and relative.parts[1] == "test":
    return False

  return not any(part in name or part in lower_name for part in SOURCE_SCAN_SKIP_NAME_PARTS)

def registry_covers_source(registry: dict, source_file: str, repo_root: Path) -> bool:
  normalized_source = Path(source_file).as_posix()

  for suite in registry.get("suites", []):
    files = [
      Path(file_path).as_posix()
      for file_path in suite.get("files", [])
    ]
    existing_files = [
      file_path
      for file_path in files
      if (repo_root / file_path).exists()
    ]

    if not existing_files:
      continue

    related_sources = [
      Path(file_path).as_posix()
      for file_path in suite.get("relatedSource", [])
    ]
    run_patterns = [
      Path(pattern).as_posix()
      for pattern in suite.get("runWhenChanged", [])
    ]

    if normalized_source in related_sources:
      return True

    if any(fnmatch(normalized_source, pattern) for pattern in run_patterns):
      return True

  return False

def source_and_test_look_related(source_file: str, test_file: str) -> bool:
  source = Path(source_file)
  test = Path(test_file)

  if test_subject_stem(test_file).lower() != source.stem.lower():
    return False

  if source.parent == test.parent:
    return True

  source_segments = set(source.parent.parts) - {"src", "app"}
  test_segments = set(test.parent.parts) - {"src", "test", "tests", "__tests__"}

  return bool(source_segments.intersection(test_segments))

def existing_test_covers_source(source_file: str, test_files: list[str], repo_root: Path) -> bool:
  for candidate in candidate_test_paths_for_source(source_file):
    if (repo_root / candidate).exists():
      return True

  return any(
    source_and_test_look_related(source_file, test_file)
    for test_file in test_files
  )

def discover_missing_test_sources(
  repo_path: str,
  registry_path: str = "src/test/test-registry.json",
) -> dict:
  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return {
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }

  registry_file = (repo_root / registry_path).resolve()
  registry = {}

  if registry_file.exists() and registry_file.is_file():
    registry = json.loads(registry_file.read_text(encoding="utf-8"))

  source_files = []
  test_files = []

  for path in repo_root.rglob("*"):
    if not path.is_file():
      continue

    relative = path.relative_to(repo_root).as_posix()

    if is_test_file_path(relative):
      test_files.append(relative)

    if is_source_scan_candidate(path, repo_root):
      source_files.append(relative)

  source_files = sorted(dict.fromkeys(source_files))
  test_files = sorted(dict.fromkeys(test_files))
  missing_test_source_files = []
  covered_source_files = []
  deferred_low_intent_source_files = []
  source_test_intent_by_file = {}

  for source_file in source_files:
    covered = (
      registry_covers_source(registry, source_file, repo_root)
      or existing_test_covers_source(source_file, test_files, repo_root)
    )

    if covered:
      covered_source_files.append(source_file)
    else:
      source_path = repo_root / source_file
      source_text = source_path.read_text(encoding="utf-8", errors="replace")
      intent = source_test_intent(source_file, source_text)
      source_test_intent_by_file[source_file] = intent

      if intent.get("shouldTest"):
        missing_test_source_files.append(source_file)
      else:
        deferred_low_intent_source_files.append({
          "path": source_file,
          "score": intent.get("score"),
          "feasibilityScore": intent.get("feasibilityScore"),
          "selectionScore": intent.get("selectionScore"),
          "reasons": intent.get("reasons", []),
          "feasibilityReasons": intent.get("feasibilityReasons", []),
        })

  missing_test_source_files = sorted(
    missing_test_source_files,
    key=lambda source_file: (
      -source_test_intent_by_file[source_file].get("selectionScore", 0),
      -source_test_intent_by_file[source_file].get("feasibilityScore", 0),
      -source_test_intent_by_file[source_file].get("score", 0),
      source_file,
    ),
  )

  return {
    "ok": True,
    "repoPath": str(repo_root),
    "registryPath": str(registry_file),
    "sourceFiles": source_files,
    "testFiles": test_files,
    "coveredSourceFiles": covered_source_files,
    "missingTestSourceFiles": missing_test_source_files,
    "deferredLowIntentSourceFiles": deferred_low_intent_source_files,
    "sourceTestIntentByFile": source_test_intent_by_file,
    "summary": {
      "sourceFiles": len(source_files),
      "testFiles": len(test_files),
      "coveredSourceFiles": len(covered_source_files),
      "missingTestSourceFiles": len(missing_test_source_files),
      "deferredLowIntentSourceFiles": len(deferred_low_intent_source_files),
    },
  }

@tool
def get_changed_files_from_git_diff(
  repo_path: str,
  base_ref: str = "HEAD",
) -> str:
  """Return files changed in git diff for the target repo."""

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  if not base_ref or base_ref.startswith("-"):
    return json.dumps({
      "ok": False,
      "error": "base_ref must be a git ref such as HEAD, main, or origin/main.",
    }, indent=2)

  result = subprocess.run(
    [
      "git",
      "-c",
      f"safe.directory={repo_root}",
      "diff",
      "--name-status",
      "-z",
      base_ref,
      "--",
    ],
    cwd=repo_root,
    text=True,
    encoding="utf-8",
    errors="replace",
    capture_output=True,
    timeout=30,
  )

  if result.returncode != 0:
    return json.dumps({
      "ok": False,
      "repoPath": str(repo_root),
      "baseRef": base_ref,
      "error": result.stderr.strip() or result.stdout.strip(),
    }, indent=2)

  entries = result.stdout.split("\0")
  if entries and entries[-1] == "":
    entries.pop()

  changes = []
  index = 0

  while index < len(entries):
    status = entries[index]
    index += 1

    if not status or index >= len(entries):
      break

    status_code = status[0]

    if status_code in ("R", "C"):
      old_path = Path(entries[index]).as_posix()
      index += 1

      if index >= len(entries):
        break

      path = Path(entries[index]).as_posix()
      index += 1

      changes.append({
        "path": path,
        "oldPath": old_path,
        "status": status,
        "statusCode": status_code,
      })
      continue

    path = Path(entries[index]).as_posix()
    index += 1

    changes.append({
      "path": path,
      "status": status,
      "statusCode": status_code,
    })

  return json.dumps({
    "ok": True,
    "repoPath": str(repo_root),
    "baseRef": base_ref,
    "changedFiles": [change["path"] for change in changes],
    "changes": changes,
  }, indent=2)

@tool
def write_test_file(
  repo_path: str,
  file_path: str,
  content: str,
  overwrite: bool = False
) -> str:
  """Create or replace a test file inside the target repo."""

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

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  candidate = Path(file_path)

  if candidate.is_absolute():
    target_file = candidate.resolve()
  else:
    target_file = (repo_root / candidate).resolve()

  try:
    relative_path = target_file.relative_to(repo_root)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": f"file_path is outside repo: {target_file}",
    }, indent=2)

  relative_posix = relative_path.as_posix()

  is_allowed_test_file = any(
    fnmatch(relative_posix, pattern)
    for pattern in allowed_test_patterns
  )

  if not is_allowed_test_file:
    return json.dumps({
      "ok": False,
      "error": "Only test files may be written.",
      "rejectedPath": relative_posix,
      "allowedPatterns": allowed_test_patterns,
    }, indent=2)

  if target_file.exists() and not overwrite:
    return json.dumps({
      "ok": False,
      "error": "Target test file already exists. Pass overwrite=True to replace it.",
      "path": relative_posix,
    }, indent=2)

  normalized_content = normalize_test_import_aliases(content)
  content_errors = validate_test_file_content(normalized_content)

  if content_errors:
    return json.dumps({
      "ok": False,
      "error": "Generated test content failed validation.",
      "validationErrors": content_errors,
      "path": relative_posix,
    }, indent=2)

  normalized_content = normalized_content.replace("\r\n", "\n").replace("\r", "\n")

  if not normalized_content.endswith("\n"):
    normalized_content += "\n"

  target_file.parent.mkdir(parents=True, exist_ok=True)

  existed_before = target_file.exists()
  target_file.write_text(normalized_content, encoding="utf-8")

  return json.dumps({
    "ok": True,
    "action": "updated" if existed_before else "created",
    "path": relative_posix,
    "sizeBytes": target_file.stat().st_size,
  }, indent=2)

@tool
def get_changed_file_diff(
  repo_path: str,
  changed_files: list[str],
  base_ref: str = "HEAD",
  max_chars_per_file: int = 20000
) -> str:
  """Return git diffs for changed files so the agent can understand what behavior changed."""

  if isinstance(changed_files, str):
    changed_files = json.loads(changed_files)

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  if not base_ref or base_ref.startswith("-"):
    return json.dumps({
      "ok": False,
      "error": "base_ref must be a git ref such as HEAD, main, or origin/main.",
    }, indent=2)

  diffs = []

  for raw_file in changed_files:
    candidate = Path(raw_file)

    if candidate.is_absolute():
      file_path = candidate.resolve()
    else:
      file_path = (repo_root / candidate).resolve()

    try:
      relative_path = file_path.relative_to(repo_root)
    except ValueError:
      return json.dumps({
        "ok": False,
        "error": f"changed file is outside repo: {file_path}",
      }, indent=2)

    relative_posix = relative_path.as_posix()

    if not file_path.exists():
      diffs.append({
        "path": relative_posix,
        "exists": False,
        "diff": "",
        "notes": ["File does not exist in working tree. It may have been deleted."],
      })
      continue

    result = subprocess.run(
      [
        "git",
        "-c",
        f"safe.directory={repo_root}",
        "diff",
        "--no-ext-diff",
        f"--unified=80",
        base_ref,
        "--",
        relative_posix,
      ],
      cwd=repo_root,
      text=True,
      encoding="utf-8",
      errors="replace",
      capture_output=True,
      timeout=30,
    )

    if result.returncode != 0:
      diffs.append({
        "path": relative_posix,
        "ok": False,
        "error": result.stderr.strip() or result.stdout.strip(),
        "diff": "",
      })
      continue

    diff_text = result.stdout

    if not diff_text.strip():
      tracked_check = subprocess.run(
        [
          "git",
          "-c",
          f"safe.directory={repo_root}",
          "ls-files",
          "--error-unmatch",
          relative_posix,
        ],
        cwd=repo_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
      )

      if tracked_check.returncode != 0:
        content = file_path.read_text(encoding="utf-8", errors="replace")

        diffs.append({
          "path": relative_posix,
          "ok": True,
          "isUntracked": True,
          "diff": "",
          "contentPreview": content[:max_chars_per_file],
          "truncated": len(content) > max_chars_per_file,
          "notes": [
            "File is untracked, so git diff did not produce a normal diff.",
            "Use contentPreview as the new-file context."
          ],
        })
        continue

    diffs.append({
      "path": relative_posix,
      "ok": True,
      "isUntracked": False,
      "diff": diff_text[:max_chars_per_file],
      "truncated": len(diff_text) > max_chars_per_file,
    })

  return json.dumps({
    "ok": True,
    "repoPath": str(repo_root),
    "baseRef": base_ref,
    "changedFiles": changed_files,
    "diffs": diffs,
  }, indent=2)

@tool
def patch_test_file(
  repo_path: str,
  file_path: str,
  find_text: str,
  replace_text: str
) -> str:
  """Patch an existing test file by replacing an exact text block."""

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

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  candidate = Path(file_path)

  if candidate.is_absolute():
    target_file = candidate.resolve()
  else:
    target_file = (repo_root / candidate).resolve()

  try:
    relative_path = target_file.relative_to(repo_root)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": f"file_path is outside repo: {target_file}",
    }, indent=2)

  relative_posix = relative_path.as_posix()

  is_allowed_test_file = any(
    fnmatch(relative_posix, pattern)
    for pattern in allowed_test_patterns
  )

  if not is_allowed_test_file:
    return json.dumps({
      "ok": False,
      "error": "Only test files may be patched.",
      "rejectedPath": relative_posix,
      "allowedPatterns": allowed_test_patterns,
    }, indent=2)

  if not target_file.exists():
    return json.dumps({
      "ok": False,
      "error": "Target test file does not exist.",
      "path": relative_posix,
    }, indent=2)

  if not find_text:
    return json.dumps({
      "ok": False,
      "error": "find_text must not be empty.",
      "path": relative_posix,
    }, indent=2)

  original = target_file.read_text(encoding="utf-8", errors="replace")

  if find_text not in original:
    return json.dumps({
      "ok": False,
      "error": "find_text was not found in target file.",
      "path": relative_posix,
    }, indent=2)

  patched = normalize_test_import_aliases(original.replace(find_text, replace_text, 1))

  if patched == original:
    return json.dumps({
      "ok": False,
      "error": "Patch did not change file content.",
      "path": relative_posix,
    }, indent=2)

  content_errors = validate_test_file_content(patched)

  if content_errors:
    return json.dumps({
      "ok": False,
      "error": "Patched test content failed validation.",
      "validationErrors": content_errors,
      "path": relative_posix,
    }, indent=2)

  target_file.write_text(patched, encoding="utf-8")

  return json.dumps({
    "ok": True,
    "action": "patched",
    "path": relative_posix,
    "replacements": 1,
    "sizeBytes": target_file.stat().st_size,
  }, indent=2)

@tool
def write_test_support_file(
  repo_path: str,
  file_path: str,
  content: str,
  overwrite: bool = False,
) -> str:
  """Create or replace a test setup/support file inside the target repo."""

  allowed_support_files = {
    "src/test/setup.ts",
    "src/test/setup.tsx",
    "src/test/setup.js",
    "src/test/setup.jsx",
    "src/test/utils/render.tsx",
    "src/test/utils/render.ts",
    "src/test/utils/render.jsx",
    "src/test/utils/render.js",
    "src/setupTests.ts",
    "src/setupTests.tsx",
    "src/setupTests.js",
    "src/setupTests.jsx",
    "tests/setup.ts",
    "tests/setup.tsx",
    "tests/setup.js",
    "tests/setup.jsx",
  }

  repo_root = Path(repo_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  candidate = Path(file_path)
  if candidate.is_absolute():
    target_file = candidate.resolve()
  else:
    target_file = (repo_root / candidate).resolve()

  try:
    relative_path = target_file.relative_to(repo_root)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": f"file_path is outside repo: {target_file}",
    }, indent=2)

  relative_posix = relative_path.as_posix()
  if relative_posix not in allowed_support_files:
    return json.dumps({
      "ok": False,
      "error": "Only known test setup/support files may be written.",
      "rejectedPath": relative_posix,
      "allowedFiles": sorted(allowed_support_files),
    }, indent=2)

  if target_file.exists() and not overwrite:
    return json.dumps({
      "ok": False,
      "error": "Target test support file already exists. Pass overwrite=True to replace it.",
      "path": relative_posix,
    }, indent=2)

  normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
  if not normalized_content.strip():
    return json.dumps({
      "ok": False,
      "error": "content must not be empty.",
      "path": relative_posix,
    }, indent=2)

  if not normalized_content.endswith("\n"):
    normalized_content += "\n"

  target_file.parent.mkdir(parents=True, exist_ok=True)
  existed_before = target_file.exists()
  target_file.write_text(normalized_content, encoding="utf-8")

  return json.dumps({
    "ok": True,
    "action": "updated" if existed_before else "created",
    "path": relative_posix,
    "sizeBytes": target_file.stat().st_size,
  }, indent=2)

@tool
def update_test_registry(
  repo_path: str,
  registry_path: str,
  suite_id: str,
  test_file: str,
  related_source: list[str] | None = None,
  run_when_changed: list[str] | None = None,
) -> str:
  """Add a test file and source mappings to a test registry suite."""

  repo_root = Path(repo_path).resolve()
  registry_file = (repo_root / registry_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  try:
    registry_file.relative_to(repo_root)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": f"registry_path is outside repo: {registry_file}",
    }, indent=2)

  if registry_file.name != "test-registry.json":
    return json.dumps({
      "ok": False,
      "error": "Only test-registry.json may be updated.",
      "path": str(registry_file),
    }, indent=2)

  test_path = Path(test_file)
  if test_path.is_absolute():
    return json.dumps({
      "ok": False,
      "error": "test_file must be repo-relative.",
      "testFile": test_file,
    }, indent=2)

  test_file_posix = test_path.as_posix()
  test_file_path = (repo_root / test_file_posix).resolve()

  try:
    test_file_path.relative_to(repo_root)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": f"test_file is outside repo: {test_file_path}",
      "testFile": test_file_posix,
    }, indent=2)

  allowed_test_file = any(fnmatch(test_file_posix, pattern) for pattern in [
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "src/test/**",
    "tests/**",
    "__tests__/**",
    "**/__tests__/**",
  ])

  if not allowed_test_file:
    return json.dumps({
      "ok": False,
      "error": "Only test files may be added to the registry.",
      "testFile": test_file_posix,
    }, indent=2)

  if not test_file_path.exists():
    return json.dumps({
      "ok": False,
      "error": "test_file does not exist, so it cannot be added to the registry.",
      "testFile": test_file_posix,
    }, indent=2)

  registry_created = False
  suite_created = False

  if registry_file.exists():
    registry = json.loads(registry_file.read_text(encoding="utf-8"))
  else:
    registry = {"suites": []}
    registry_created = True

  registry.setdefault("suites", [])
  suite = next(
    (item for item in registry.get("suites", []) if item.get("id") == suite_id),
    None,
  )

  if suite is None:
    if registry_created or not registry.get("suites"):
      suite_id = suite_id or "generated-tests"
      suite = {
        "id": suite_id,
        "name": "Generated tests",
        "type": "generated",
        "command": "",
        "files": [],
        "relatedSource": [],
        "runWhenChanged": [],
        "cost": "low",
      }
      registry["suites"].append(suite)
      suite_created = True
    else:
      return json.dumps({
        "ok": False,
        "error": f"No suite found with id: {suite_id}",
      }, indent=2)

  def append_unique(key: str, values: list[str]) -> None:
    current = suite.setdefault(key, [])
    for value in values:
      normalized = Path(value).as_posix()
      if normalized not in current:
        current.append(normalized)

  stale_files = []
  for file_path in suite.get("files", []):
    normalized = Path(file_path).as_posix()
    same_test_name = Path(normalized).name == Path(test_file_posix).name and normalized != test_file_posix
    missing_file = not (repo_root / normalized).exists()

    if same_test_name or missing_file:
      stale_files.append(normalized)

  if stale_files:
    suite["files"] = [
      file_path
      for file_path in suite.get("files", [])
      if Path(file_path).as_posix() not in stale_files
    ]

  append_unique("files", [test_file_posix])
  append_unique("relatedSource", related_source or [])
  append_unique("runWhenChanged", run_when_changed or [])

  suite["command"] = "npm run test -- " + " ".join(suite.get("files", []))

  registry_file.parent.mkdir(parents=True, exist_ok=True)
  registry_file.write_text(
    json.dumps(registry, indent=2) + "\n",
    encoding="utf-8",
  )

  return json.dumps({
    "ok": True,
    "action": "created" if registry_created else "updated",
    "suiteId": suite_id,
    "suiteCreated": suite_created,
    "testFile": test_file_posix,
    "registryPath": str(registry_file.relative_to(repo_root)),
  }, indent=2)

@tool
def cleanup_tests_for_deleted_sources(
  repo_path: str,
  registry_path: str,
  deleted_source_files: list[str],
  deleted_test_files: list[str] | None = None,
) -> str:
  """Remove safely matched tests and registry references for deleted source/test files."""

  if isinstance(deleted_source_files, str):
    deleted_source_files = json.loads(deleted_source_files)

  if isinstance(deleted_test_files, str):
    deleted_test_files = json.loads(deleted_test_files)

  deleted_test_files = deleted_test_files or []
  repo_root = Path(repo_path).resolve()
  registry_file = (repo_root / registry_path).resolve()

  if not repo_root.exists():
    return json.dumps({
      "ok": False,
      "error": f"repo_path does not exist: {repo_root}",
    }, indent=2)

  try:
    registry_file.relative_to(repo_root)
  except ValueError:
    return json.dumps({
      "ok": False,
      "error": f"registry_path is outside repo: {registry_file}",
    }, indent=2)

  if registry_file.name != "test-registry.json":
    return json.dumps({
      "ok": False,
      "error": "Only test-registry.json may be updated.",
      "path": str(registry_file),
    }, indent=2)

  normalized_deleted_sources = [
    Path(path).as_posix()
    for path in deleted_source_files
    if path
  ]
  normalized_deleted_tests = [
    Path(path).as_posix()
    for path in deleted_test_files
    if path
  ]

  candidate_paths_by_source = {
    source: candidate_test_paths_for_source(source)
    for source in normalized_deleted_sources
  }
  test_files_to_remove = set(normalized_deleted_tests)
  removal_reasons = {}

  for test_file in normalized_deleted_tests:
    removal_reasons.setdefault(test_file, []).append("test file was deleted")

  registry = {"suites": []}
  registry_exists = registry_file.exists()
  if registry_exists:
    registry = json.loads(registry_file.read_text(encoding="utf-8"))

  for suite in registry.get("suites", []):
    suite_files = [
      Path(path).as_posix()
      for path in suite.get("files", [])
    ]
    related_source = [
      Path(path).as_posix()
      for path in suite.get("relatedSource", [])
    ]
    run_when_changed = [
      Path(path).as_posix()
      for path in suite.get("runWhenChanged", [])
    ]

    for source in normalized_deleted_sources:
      source_stem = Path(source).stem.lower()
      source_candidates = set(candidate_paths_by_source.get(source, []))
      suite_maps_source = source in related_source or source in run_when_changed

      for test_file in suite_files:
        normalized_test_file = Path(test_file).as_posix()
        is_conventional_match = normalized_test_file in source_candidates
        is_registry_match = (
          suite_maps_source
          and test_subject_stem(normalized_test_file).lower() == source_stem
        )

        if is_conventional_match or is_registry_match:
          test_files_to_remove.add(normalized_test_file)
          reason = (
            f"matched deleted source {source}"
            if is_conventional_match
            else f"registry mapped test to deleted source {source}"
          )
          removal_reasons.setdefault(normalized_test_file, []).append(reason)

  for source, candidates in candidate_paths_by_source.items():
    for test_file in candidates:
      if (repo_root / test_file).exists():
        test_files_to_remove.add(test_file)
        removal_reasons.setdefault(test_file, []).append(
          f"conventional test path for deleted source {source}"
        )

  removed_files = []
  skipped_files = []

  for test_file in sorted(test_files_to_remove):
    normalized_test_file = Path(test_file).as_posix()

    if not is_allowed_test_path(normalized_test_file):
      skipped_files.append({
        "path": normalized_test_file,
        "reason": "not an allowed test path",
      })
      continue

    target_file = (repo_root / normalized_test_file).resolve()

    try:
      target_file.relative_to(repo_root)
    except ValueError:
      skipped_files.append({
        "path": normalized_test_file,
        "reason": "resolved outside repo",
      })
      continue

    if not target_file.exists():
      skipped_files.append({
        "path": normalized_test_file,
        "reason": "test file already absent",
      })
      continue

    if not target_file.is_file():
      skipped_files.append({
        "path": normalized_test_file,
        "reason": "target is not a file",
      })
      continue

    target_file.unlink()
    removed_files.append({
      "path": normalized_test_file,
      "reasons": removal_reasons.get(normalized_test_file, []),
    })

  registry_updated = False
  removed_registry_files = []

  if registry_exists:
    removed_paths = {
      item["path"]
      for item in removed_files
    } | set(normalized_deleted_tests)

    for suite in registry.get("suites", []):
      original_files = [
        Path(path).as_posix()
        for path in suite.get("files", [])
      ]
      filtered_files = [
        path
        for path in original_files
        if path not in removed_paths
      ]

      if filtered_files != original_files:
        suite["files"] = filtered_files
        removed_registry_files.extend(
          path
          for path in original_files
          if path not in filtered_files
        )
        registry_updated = True

        command = suite.get("command") or ""
        if command.startswith("npm run test --"):
          suite["command"] = "npm run test -- " + " ".join(filtered_files)

      for key in ["relatedSource", "runWhenChanged"]:
        original_values = [
          Path(path).as_posix()
          for path in suite.get(key, [])
        ]
        filtered_values = [
          path
          for path in original_values
          if path not in normalized_deleted_sources
        ]

        if filtered_values != original_values:
          suite[key] = filtered_values
          registry_updated = True

    if registry_updated:
      registry_file.write_text(
        json.dumps(registry, indent=2) + "\n",
        encoding="utf-8",
      )

  return json.dumps({
    "ok": True,
    "repoPath": str(repo_root),
    "registryPath": str(registry_file.relative_to(repo_root)) if registry_exists else registry_path,
    "deletedSourceFiles": normalized_deleted_sources,
    "deletedTestFiles": normalized_deleted_tests,
    "testFilesRemoved": removed_files,
    "testFilesSkipped": skipped_files,
    "registryUpdated": registry_updated,
    "registryFilesRemoved": sorted(set(removed_registry_files)),
  }, indent=2)
