from pathlib import Path
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from fnmatch import fnmatch

class TestWriteGuardHook(HookProvider):
  """
  Blocks write tools from editing product code.
  Test creation agents may only create or update approved test files.
  """

  WRITE_TOOL_NAMES = {
    "write_test_file",
    "patch_test_file",
    "write_test_support_file",
  }

  ALLOWED_TEST_PATTERNS = [
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "src/test/**",
    "tests/**",
    "__tests__/**",
    "**/__tests__/**",
  ]

  ALLOWED_REGISTRY_FILENAMES = {
    "test-registry.json",
  }

  ALLOWED_SUPPORT_FILES = {
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

  def register_hooks(self, registry: HookRegistry) -> None:
    registry.add_callback(BeforeToolCallEvent, self.validate_test_write)

  def validate_test_write(self, event: BeforeToolCallEvent) -> None:
    tool_name = event.tool_use["name"]

    if tool_name not in self.WRITE_TOOL_NAMES:
      return

    tool_input = event.tool_use.get("input", {})

    repo_path = tool_input.get("repo_path")
    file_path = tool_input.get("file_path")

    if not repo_path:
      raise ValueError(
        f"Blocked {tool_name}: repo_path is required."
      )

    if not file_path:
      raise ValueError(
        f"Blocked {tool_name}: file_path is required."
      )

    repo_root = Path(repo_path).resolve()

    if not repo_root.exists():
      raise ValueError(
        f"Blocked {tool_name}: repo_path does not exist: {repo_root}"
      )

    candidate = Path(file_path)

    if candidate.is_absolute():
      resolved = candidate.resolve()
    else:
      resolved = (repo_root / candidate).resolve()

    try:
      relative_path = resolved.relative_to(repo_root)
    except ValueError:
      raise ValueError(
        f"Blocked {tool_name}: file_path is outside repo: {resolved}"
      )

    relative_posix = relative_path.as_posix()

    allow_registry_update = bool(tool_input.get("allow_registry_update", False))

    if allow_registry_update and resolved.name in self.ALLOWED_REGISTRY_FILENAMES:
      return

    if tool_name == "write_test_support_file" and relative_posix in self.ALLOWED_SUPPORT_FILES:
      return

    is_allowed_test_file = any(
      fnmatch(relative_posix, pattern)
      for pattern in self.ALLOWED_TEST_PATTERNS
    )

    if not is_allowed_test_file:
      raise ValueError(
        f"Blocked {tool_name}: only test files may be written. "
        f"Rejected path: {relative_posix}"
      )
