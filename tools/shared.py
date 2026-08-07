from pathlib import Path
import re

TEST_INTENT_THRESHOLD = 4

LOW_INTENT_SEGMENTS = {
  "ui",
  "icons",
  "layout",
}

HTML_JSX_TAGS = {
  "a",
  "article",
  "aside",
  "button",
  "div",
  "fieldset",
  "footer",
  "form",
  "h1",
  "h2",
  "h3",
  "header",
  "input",
  "label",
  "li",
  "main",
  "nav",
  "ol",
  "option",
  "p",
  "section",
  "select",
  "span",
  "table",
  "tbody",
  "td",
  "textarea",
  "th",
  "thead",
  "tr",
  "ul",
}

def parse_file_paths_argument(value: str) -> list[str]:
  """Parse comma-separated file path input from prompt commands."""

  return [
    item.strip()
    for item in (value or "").split(",")
    if item.strip()
  ]

def validate_test_file_content(content: str) -> list[str]:
  """Return validation errors for generated test content."""

  errors = []
  normalized = content.replace("\r\n", "\n").replace("\r", "\n")
  stripped = normalized.lstrip("\ufeff \t\n")
  first_line = stripped.split("\n", 1)[0]

  if not normalized.strip():
    errors.append("content must not be empty.")

  if (
    re.match(r"^\{\s*['\"]", first_line)
    or (
      re.match(r"^['\"]", first_line)
      and re.search(r"['\"]\s*\+", first_line)
    )
  ) and re.search(r"\\[nrt]|['\"]\s*\+", first_line) and re.search(r"\b(import|describe|it|test|expect|vi)\b", normalized):
    errors.append(
      "content appears to be serialized or string-concatenated test source; pass raw test file content instead."
    )

  if not re.search(r"\b(describe|it|test)(?:\.\w+)?\s*\(", normalized):
    errors.append("content must contain at least one describe(...), it(...), or test(...).")

  placeholder_patterns = [
    r"(?im)^\s*//\s*test code here\b",
    r"(?im)^\s*/\*\s*test code here\b",
    r"(?im)^\s*//\s*todo\b",
    r"(?im)^\s*/\*\s*todo\b",
    r"(?im)^\s*//\s*placeholder\b",
    r"(?im)^\s*/\*\s*placeholder\b",
    r"(?im)^\s*//\s*to be implemented\b",
    r"(?im)^\s*/\*\s*to be implemented\b",
  ]

  for pattern in placeholder_patterns:
    if re.search(pattern, normalized):
      errors.append("content contains placeholder test comments.")

  if "export function" in normalized and not re.search(r"\b(it|test)(?:\.\w+)?\s*\(", normalized):
    errors.append("exported functions are not test cases; use it(...) or test(...).")

  if (
    re.search(r"\bcontainer\.firstChild\b", normalized)
    and re.search(r"\brenders without crashing\b", normalized, re.IGNORECASE)
  ):
    errors.append(
      "test only asserts a shallow render smoke check; assert user-visible behavior, hook return values, function output, or interactions instead."
    )

  lowercase_render_tag = re.search(
    r"\brender(?:WithAppProviders)?\s*\(\s*<(?P<tag>[a-z][A-Za-z0-9_.:-]*)\b",
    normalized,
  )
  if lowercase_render_tag and lowercase_render_tag.group("tag") not in HTML_JSX_TAGS:
    errors.append(
      "test renders a lowercase JSX tag; call utilities directly or render the real exported React component with an uppercase name."
    )

  return errors

def source_test_intent(source_path: str, source_text: str) -> dict:
  """Score whether a source file is worth new test coverage and what to assert."""

  path = Path(source_path)
  normalized_path = path.as_posix()
  path_parts = set(path.parts)
  name = path.stem
  score = 0
  feasibility_score = 5
  reasons = []
  feasibility_reasons = []
  suggested_assertions = []
  test_kinds = []

  def add(points: int, reason: str, assertion: str | None = None, kind: str | None = None) -> None:
    nonlocal score
    score += points
    reasons.append(reason)
    if assertion and assertion not in suggested_assertions:
      suggested_assertions.append(assertion)
    if kind and kind not in test_kinds:
      test_kinds.append(kind)

  if "hooks" in path_parts or re.match(r"^use[A-Z0-9]", name):
    add(
      3,
      "hook or hook-adjacent module",
      "assert returned values, state transitions, and mocked API outcomes with renderHook",
      "hook",
    )
    feasibility_score += 1
    feasibility_reasons.append("hook can often be exercised with renderHook and mocked dependencies")

  if path_parts.intersection({"pages", "routes"}) or name.endswith(("Page", "Route", "Redirect")):
    add(
      3,
      "user-facing page or route",
      "assert the visible route state, redirect, loading, empty, or permission outcome",
      "component",
    )
    feasibility_score -= 2
    feasibility_reasons.append("page-level tests usually need router, auth, provider, and data mocks")

  if name.endswith(("Dialog", "Form", "Modal")) or re.search(r"\b(onSubmit|handleSubmit|form|input|textarea|select)\b", source_text):
    add(
      3,
      "form or dialog workflow",
      "assert validation, disabled/pending states, submitted payloads, or close/cancel behavior",
      "component",
    )
    feasibility_score -= 1
    feasibility_reasons.append("form/dialog tests need concrete props, validation rules, and user events")

  if name.endswith("Provider") or re.search(r"\b(createContext|useContext|Provider)\b", source_text):
    add(
      2,
      "context provider behavior",
      "assert values exposed to a consumer and provider state changes",
      "component",
    )

  if path_parts.intersection({"lib", "utils", "services", "api"}) and re.search(r"\bexport\s+(?:async\s+)?(?:function|const)\b", source_text):
    add(
      2,
      "exported utility or service logic",
      "call exported functions directly with representative inputs and outputs",
      "unit",
    )
    feasibility_score += 3
    feasibility_reasons.append("exported utility/service logic is usually testable with direct inputs and outputs")

  if re.search(r"\b(if|switch)\b|[?]\s*[^:]+:|&&|\|\|", source_text):
    add(
      2,
      "branching or conditional rendering",
      "cover the meaningful branches, not only the default path",
    )

  if re.search(r"\b(await|async|fetch|axios|supabase|mutate|useMutation|useQuery|queryClient)\b", source_text):
    add(
      2,
      "async data or mutation behavior",
      "mock success and failure/loading states",
    )
    feasibility_score -= 1
    feasibility_reasons.append("async/data dependencies require mocks")

  if re.search(r"\b(onClick|onChange|onSubmit|onKeyDown|userEvent|fireEvent|disabled|aria-)\b", source_text):
    add(
      2,
      "user interaction or accessibility state",
      "assert user-triggered behavior and accessible output",
      "component",
    )

  if re.search(r"\b(loading|isLoading|pending|isPending|error|empty|unauthorized|forbidden|notFound)\b", source_text, re.IGNORECASE):
    add(
      2,
      "loading/error/empty/permission states",
      "assert each user-visible state with mocked inputs",
    )

  if re.search(r"\b(navigate|redirect|Navigate|useNavigate|useLocation|router|route)\b", source_text):
    add(
      2,
      "navigation behavior",
      "assert navigation or redirect decisions without relying on real browser navigation",
    )

  if re.search(r"\b(role|permission|auth|session|currentUser|userId|isAdmin)\b", source_text):
    add(
      2,
      "auth or permission logic",
      "assert allowed and denied outcomes",
    )

  if re.search(r"\b(parse|format|normalize|serialize|deserialize|filter|sort|reduce|map)\b", source_text):
    add(
      1,
      "data transformation logic",
      "assert representative input/output examples including edge cases",
      "unit",
    )

  likely_presentational_wrapper = (
    path_parts.intersection(LOW_INTENT_SEGMENTS)
    and not re.search(r"\b(if|switch|onClick|onChange|onSubmit|useState|useEffect|async|await)\b", source_text)
  )
  if likely_presentational_wrapper:
    score -= 3
    reasons.append("likely low-value presentational wrapper")

  import_count = len(re.findall(r"(?m)^\s*import\s+", source_text))
  hook_call_count = len(re.findall(r"\buse[A-Z][A-Za-z0-9_]*\s*\(", source_text))
  external_touchpoints = len(re.findall(
    r"\b(window|document|localStorage|sessionStorage|FileReader|ResizeObserver|IntersectionObserver|URL\.createObjectURL)\b",
    source_text,
  ))

  if import_count > 12:
    feasibility_score -= 3
    feasibility_reasons.append("many imports make required mocks harder to infer")
  elif import_count > 6:
    feasibility_score -= 1
    feasibility_reasons.append("several imports may require project-specific mocks")

  if hook_call_count > 5:
    feasibility_score -= 2
    feasibility_reasons.append("many hook calls make provider and mock setup harder to infer")
  elif hook_call_count > 2:
    feasibility_score -= 1
    feasibility_reasons.append("multiple hook calls may need coordinated mocks")

  if external_touchpoints:
    feasibility_score -= min(2, external_touchpoints)
    feasibility_reasons.append("browser/global APIs need test-environment setup")

  if path.suffix in {".ts", ".js"} and "jsx" not in source_text.lower() and "<" not in source_text:
    feasibility_score += 1
    feasibility_reasons.append("non-JSX module is easier to test without rendering")

  if not reasons:
    reasons.append("no obvious behavior beyond composition or rendering")

  feasibility_score = max(0, min(10, feasibility_score))
  selection_score = score + (feasibility_score * 2)
  should_test = score >= TEST_INTENT_THRESHOLD
  priority = "high" if score >= 8 else "medium" if should_test else "low"

  return {
    "path": normalized_path,
    "score": score,
    "feasibilityScore": feasibility_score,
    "selectionScore": selection_score,
    "threshold": TEST_INTENT_THRESHOLD,
    "priority": priority,
    "shouldTest": should_test,
    "testKinds": test_kinds or ["component" if path.suffix in {".tsx", ".jsx"} else "unit"],
    "reasons": reasons,
    "feasibilityReasons": feasibility_reasons,
    "suggestedAssertions": suggested_assertions,
  }

def normalize_test_import_aliases(content: str) -> str:
  """Normalize common Vite src alias mistakes in generated test imports."""

  return (
    content
    .replace("from '@/src/", "from '@/")
    .replace('from "@/src/', 'from "@/')
    .replace("import('@/src/", "import('@/")
    .replace('import("@/src/', 'import("@/')
  )

def source_import_path(source_path: str) -> str:
  if source_path.startswith("src/"):
    source_path = source_path.removeprefix("src/")

  return "@/" + Path(source_path).with_suffix("").as_posix()

def source_import_aliases(source_path: str) -> list[str]:
  aliases = [source_import_path(source_path)]

  if source_path.startswith("src/"):
    src_alias = "@/" + Path(source_path).with_suffix("").as_posix()
    if src_alias not in aliases:
      aliases.append(src_alias)

  return aliases

def has_named_export(source_text: str, export_name: str) -> bool:
  escaped_name = re.escape(export_name)
  patterns = [
    rf"export\s+(?:async\s+)?function\s+{escaped_name}\b",
    rf"export\s+(?:const|let|var|class)\s+{escaped_name}\b",
    rf"export\s*{{[^}}]*\b{escaped_name}\b[^}}]*}}",
  ]

  return any(re.search(pattern, source_text) for pattern in patterns)

def default_export_name(source_text: str) -> str | None:
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

def has_default_export(source_text: str) -> bool:
  return bool(re.search(r"\bexport\s+default\b", source_text))

def component_name_from_source(source_path: str, source_text: str) -> str:
  function_match = re.search(r"export\s+function\s+([A-Z][A-Za-z0-9_]*)", source_text)
  if function_match:
    return function_match.group(1)

  const_match = re.search(r"export\s+const\s+([A-Z][A-Za-z0-9_]*)", source_text)
  if const_match:
    return const_match.group(1)

  default_name = default_export_name(source_text)
  if default_name:
    return default_name

  return Path(source_path).stem

def component_requires_props(source_text: str, component_name: str) -> bool:
  """Conservatively detect components that should not be smoke-rendered with no props."""

  escaped_name = re.escape(component_name)
  function_match = re.search(
    rf"(?:export\s+default\s+)?(?:export\s+)?function\s+{escaped_name}\s*\((?P<params>[^)]*)\)",
    source_text,
    re.DOTALL,
  )

  if function_match:
    return bool(function_match.group("params").strip())

  arrow_match = re.search(
    rf"(?:export\s+)?const\s+{escaped_name}\s*(?::[^=]+)?=\s*(?P<params>\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
    source_text,
    re.DOTALL,
  )

  if arrow_match:
    params = arrow_match.group("params").strip()
    if params == "()":
      return False

    if params.startswith("(") and params.endswith(")"):
      params = params[1:-1].strip()

    return bool(params)

  typed_component_match = re.search(
    rf"(?:export\s+)?const\s+{escaped_name}\s*:\s*(?:React\.)?(?:FC|FunctionComponent)\s*<",
    source_text,
  )

  return bool(typed_component_match)

def import_export_mismatch_errors(test_content: str, changed_file_previews: list[dict]) -> list[str]:
  errors = []

  for preview in changed_file_previews:
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
        rf"from\s*['\"]{re.escape(alias)}['\"]"
      )

      if re.search(pattern, test_content):
        errors.append(
          f"test imports {component_name} as a named export from {alias}, but the source only exposes it as a default export."
        )
        break

  return errors
