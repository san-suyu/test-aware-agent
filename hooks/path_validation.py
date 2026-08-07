from pathlib import Path
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

class PathValidationHook(HookProvider):
  """
  Validates config paths before a tool runs.
  """

  def register_hooks(self, registry: HookRegistry) -> None:
    registry.add_callback(BeforeToolCallEvent, self.validate_paths)

  def validate_paths(self, event: BeforeToolCallEvent) -> None:
    tool_name = event.tool_use["name"]

    if tool_name == "validate_project_access":
      return

    tool_input = event.tool_use.get("input", {})

    repo_path = tool_input.get("repo_path")
    if not repo_path:
      return

    repo_root = Path(repo_path).resolve()

    if not repo_root.exists():
      raise ValueError(
        f"Blocked {tool_name}: repo_path does not exist: {repo_root}"
      )

    paths_to_check = []

    registry_path = tool_input.get("registry_path")
    if registry_path:
      paths_to_check.append(registry_path)

    changed_files = tool_input.get("changed_files", [])
    paths_to_check.extend(changed_files)

    for raw_path in paths_to_check:
      candidate = Path(raw_path)

      if not candidate.is_absolute():
        candidate = repo_root / candidate

      resolved = candidate.resolve()

      if not str(resolved).startswith(str(repo_root)):
        raise ValueError(
          f"Blocked {tool_name}: path is outside repo: {resolved}"
        )

      if not resolved.exists():
        raise ValueError(
          f"Blocked {tool_name}: path does not exist: {resolved}"
        )
