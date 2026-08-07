import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


def install_fake_strands():
  if "strands" in sys.modules:
    return

  module = types.ModuleType("strands")
  module.tool = lambda function: function
  sys.modules["strands"] = module


install_fake_strands()

import tools.test_runner
from tools.test_discovery import inspect_test_context
from tools.test_repair import (
  build_failure_record,
  classify_failure_reason,
  find_failure,
  infer_failing_test_files_from_failure_text,
  stale_missing_test_setup_path,
)
from tools.test_runner import parse_vitest_summary
from tools.test_runner import run_all_tests, run_test_files, run_test_suite
from tools.test_writer import (
  cleanup_tests_for_deleted_sources,
  discover_missing_test_sources,
  get_changed_files_from_git_diff,
  update_test_registry,
  write_test_support_file,
)


TEST_CONTENT = """import { describe, expect, it } from 'vitest';

describe('HomePage', () => {
  it('renders', () => {
    expect(true).toBe(true);
  });
});
"""


class RegistryBootstrapTest(unittest.TestCase):
  def test_get_changed_files_from_git_diff_parses_name_status_output(self):
    with tempfile.TemporaryDirectory() as directory:
      calls = []

      class FakeCompletedProcess:
        returncode = 0
        stdout = "M\0src/App.tsx\0A\0src/test/App.test.tsx\0R100\0src/Old.ts\0src/New.ts\0"
        stderr = ""

      def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeCompletedProcess()

      import tools.test_writer

      original_run = tools.test_writer.subprocess.run
      tools.test_writer.subprocess.run = fake_run

      try:
        result = json.loads(get_changed_files_from_git_diff(
          repo_path=directory,
          base_ref="origin/main",
        ))
      finally:
        tools.test_writer.subprocess.run = original_run

      self.assertTrue(result["ok"])
      self.assertEqual(result["baseRef"], "origin/main")
      self.assertEqual(
        result["changedFiles"],
        ["src/App.tsx", "src/test/App.test.tsx", "src/New.ts"],
      )
      self.assertEqual(result["changes"][2]["oldPath"], "src/Old.ts")
      self.assertEqual(result["changes"][2]["statusCode"], "R")
      self.assertEqual(calls[0][0][0][0:4], [
        "git",
        "-c",
        f"safe.directory={Path(directory).resolve()}",
        "diff",
      ])

  def test_get_changed_files_from_git_diff_rejects_option_like_base_ref(self):
    with tempfile.TemporaryDirectory() as directory:
      result = json.loads(get_changed_files_from_git_diff(
        repo_path=directory,
        base_ref="--cached",
      ))

      self.assertFalse(result["ok"])
      self.assertIn("base_ref must be a git ref", result["error"])

  def test_cleanup_tests_for_deleted_sources_removes_matching_test_and_registry_refs(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      test_file = repo_root / "src/test/pages/HomePage.test.tsx"
      other_test_file = repo_root / "src/test/pages/AboutPage.test.tsx"
      registry_file = repo_root / "src/test/test-registry.json"
      test_file.parent.mkdir(parents=True)
      test_file.write_text(TEST_CONTENT, encoding="utf-8")
      other_test_file.write_text(TEST_CONTENT, encoding="utf-8")
      registry_file.write_text(json.dumps({
        "suites": [
          {
            "id": "pages",
            "name": "Pages",
            "type": "generated",
            "command": "npm run test -- src/test/pages/HomePage.test.tsx src/test/pages/AboutPage.test.tsx",
            "files": [
              "src/test/pages/HomePage.test.tsx",
              "src/test/pages/AboutPage.test.tsx",
            ],
            "relatedSource": [
              "src/pages/HomePage.tsx",
              "src/pages/AboutPage.tsx",
            ],
            "runWhenChanged": [
              "src/pages/HomePage.tsx",
              "src/pages/AboutPage.tsx",
            ],
          }
        ],
      }), encoding="utf-8")

      result = json.loads(cleanup_tests_for_deleted_sources(
        repo_path=str(repo_root),
        registry_path="src/test/test-registry.json",
        deleted_source_files=["src/pages/HomePage.tsx"],
      ))
      registry = json.loads(registry_file.read_text(encoding="utf-8"))

      self.assertTrue(result["ok"])
      self.assertFalse(test_file.exists())
      self.assertTrue(other_test_file.exists())
      self.assertEqual(
        result["testFilesRemoved"][0]["path"],
        "src/test/pages/HomePage.test.tsx",
      )
      self.assertTrue(result["registryUpdated"])
      self.assertEqual(registry["suites"][0]["files"], ["src/test/pages/AboutPage.test.tsx"])
      self.assertEqual(registry["suites"][0]["relatedSource"], ["src/pages/AboutPage.tsx"])
      self.assertEqual(registry["suites"][0]["runWhenChanged"], ["src/pages/AboutPage.tsx"])
      self.assertEqual(
        registry["suites"][0]["command"],
        "npm run test -- src/test/pages/AboutPage.test.tsx",
      )

  def test_cleanup_tests_for_deleted_sources_prunes_deleted_test_from_registry(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      registry_file = repo_root / "src/test/test-registry.json"
      registry_file.parent.mkdir(parents=True)
      registry_file.write_text(json.dumps({
        "suites": [
          {
            "id": "pages",
            "command": "npm run test -- src/test/pages/HomePage.test.tsx",
            "files": ["src/test/pages/HomePage.test.tsx"],
            "relatedSource": ["src/pages/HomePage.tsx"],
            "runWhenChanged": ["src/pages/HomePage.tsx"],
          }
        ],
      }), encoding="utf-8")

      result = json.loads(cleanup_tests_for_deleted_sources(
        repo_path=str(repo_root),
        registry_path="src/test/test-registry.json",
        deleted_source_files=[],
        deleted_test_files=["src/test/pages/HomePage.test.tsx"],
      ))
      registry = json.loads(registry_file.read_text(encoding="utf-8"))

      self.assertTrue(result["ok"])
      self.assertTrue(result["registryUpdated"])
      self.assertEqual(registry["suites"][0]["files"], [])
      self.assertEqual(result["testFilesSkipped"][0]["reason"], "test file already absent")

  def test_run_all_tests_uses_project_test_script(self):
    with tempfile.TemporaryDirectory() as directory:
      calls = []

      class FakeCompletedProcess:
        returncode = 0
        stdout = """
 Test Files  2 passed (2)
      Tests  5 passed (5)
   Duration  1.25s
"""
        stderr = ""

      def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeCompletedProcess()

      original_run = tools.test_runner.subprocess.run
      tools.test_runner.subprocess.run = fake_run

      try:
        result = json.loads(run_all_tests(repo_path=directory))
      finally:
        tools.test_runner.subprocess.run = original_run

      self.assertTrue(result["ok"])
      self.assertEqual(result["tool"], "run_all_tests")
      self.assertEqual(result["suiteId"], "all-tests")
      self.assertEqual(result["command"], "npm run test")
      self.assertEqual(result["summary"]["testFiles"]["total"], 2)
      self.assertEqual(calls[0][0][0], "npm run test")
      self.assertEqual(calls[0][1]["cwd"], Path(directory).resolve())
      self.assertEqual(calls[0][1]["timeout"], 300)

  def test_run_test_files_reports_timeout_without_raising(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      test_file = repo_root / "src/test/pages/HomePage.test.tsx"
      test_file.parent.mkdir(parents=True)
      test_file.write_text(TEST_CONTENT, encoding="utf-8")

      def fake_run(*args, **kwargs):
        raise tools.test_runner.subprocess.TimeoutExpired(
          cmd=args[0],
          timeout=kwargs["timeout"],
          output="partial stdout",
          stderr="partial stderr",
        )

      original_run = tools.test_runner.subprocess.run
      tools.test_runner.subprocess.run = fake_run

      try:
        result = json.loads(run_test_files(
          repo_path=directory,
          test_files=["src/test/pages/HomePage.test.tsx"],
        ))
      finally:
        tools.test_runner.subprocess.run = original_run

      self.assertFalse(result["ok"])
      self.assertEqual(result["tool"], "run_test_files")
      self.assertEqual(result["timeoutSeconds"], 120)
      self.assertIn("timed out", result["failureReason"])
      self.assertEqual(result["stdout"], "partial stdout")

  def test_run_test_suite_reports_timeout_without_raising(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      test_file = repo_root / "src/test/pages/HomePage.test.tsx"
      registry_file = repo_root / "src/test/test-registry.json"
      test_file.parent.mkdir(parents=True)
      test_file.write_text(TEST_CONTENT, encoding="utf-8")
      registry_file.write_text(json.dumps({
        "suites": [
          {
            "id": "pages",
            "name": "Pages",
            "command": "npm run test -- src/test/pages/HomePage.test.tsx",
            "files": ["src/test/pages/HomePage.test.tsx"],
          }
        ],
      }), encoding="utf-8")

      def fake_run(*args, **kwargs):
        raise tools.test_runner.subprocess.TimeoutExpired(
          cmd=args[0],
          timeout=kwargs["timeout"],
        )

      original_run = tools.test_runner.subprocess.run
      tools.test_runner.subprocess.run = fake_run

      try:
        result = json.loads(run_test_suite(
          repo_path=directory,
          suite_id="pages",
          registry_path="src/test/test-registry.json",
        ))
      finally:
        tools.test_runner.subprocess.run = original_run

      self.assertFalse(result["ok"])
      self.assertEqual(result["tool"], "run_test_suite")
      self.assertEqual(result["suiteId"], "pages")
      self.assertEqual(result["timeoutSeconds"], 120)

  def test_build_failure_record_infers_test_file_from_failure_output(self):
    failure = build_failure_record(
      {
        "ok": False,
        "suiteId": "all-tests",
        "suiteName": "All tests",
        "command": "npm run test",
        "exitCode": 1,
        "failureReason": "FAIL  src/test/pages/HomePage.test.tsx > HomePage > renders",
        "testFiles": [],
      },
      source="run_all_tests",
    )

    self.assertEqual(failure["testFiles"], ["src/test/pages/HomePage.test.tsx"])

  def test_update_test_registry_creates_missing_registry_and_suite(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      test_file = repo_root / "src/test/pages/HomePage.test.tsx"
      test_file.parent.mkdir(parents=True)
      test_file.write_text(TEST_CONTENT, encoding="utf-8")

      result = json.loads(update_test_registry(
        repo_path=str(repo_root),
        registry_path="src/test/test-registry.json",
        suite_id="generated-tests",
        test_file="src/test/pages/HomePage.test.tsx",
        related_source=["src/pages/HomePage.tsx"],
        run_when_changed=["src/pages/HomePage.tsx"],
      ))

      registry = json.loads((repo_root / "src/test/test-registry.json").read_text(encoding="utf-8"))

      self.assertTrue(result["ok"])
      self.assertEqual(result["action"], "created")
      self.assertTrue(result["suiteCreated"])
      self.assertEqual(registry["suites"][0]["id"], "generated-tests")
      self.assertEqual(registry["suites"][0]["files"], ["src/test/pages/HomePage.test.tsx"])
      self.assertEqual(registry["suites"][0]["runWhenChanged"], ["src/pages/HomePage.tsx"])
      self.assertEqual(
        registry["suites"][0]["command"],
        "npm run test -- src/test/pages/HomePage.test.tsx",
      )

  def test_discover_missing_test_sources_reports_uncovered_source(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      source_file = repo_root / "src/pages/HomePage.tsx"
      source_file.parent.mkdir(parents=True)
      source_file.write_text(
        "export default function HomePage({ isLoading }) { return isLoading ? <div>Loading</div> : <div>Ready</div>; }\n",
        encoding="utf-8",
      )

      result = discover_missing_test_sources(
        repo_path=str(repo_root),
        registry_path="src/test/test-registry.json",
      )

      self.assertTrue(result["ok"])
      self.assertIn("src/pages/HomePage.tsx", result["missingTestSourceFiles"])
      self.assertEqual(result["summary"]["sourceFiles"], 1)
      self.assertEqual(result["summary"]["deferredLowIntentSourceFiles"], 0)

  def test_discover_missing_test_sources_defers_low_intent_source(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      source_file = repo_root / "src/components/ui/Alert.tsx"
      source_file.parent.mkdir(parents=True)
      source_file.write_text(
        "export const Alert = ({ children }) => <div role=\"alert\">{children}</div>;\n",
        encoding="utf-8",
      )

      result = discover_missing_test_sources(
        repo_path=str(repo_root),
        registry_path="src/test/test-registry.json",
      )

      self.assertTrue(result["ok"])
      self.assertEqual(result["missingTestSourceFiles"], [])
      self.assertEqual(result["summary"]["deferredLowIntentSourceFiles"], 1)
      self.assertEqual(
        result["deferredLowIntentSourceFiles"][0]["path"],
        "src/components/ui/Alert.tsx",
      )

  def test_discover_missing_test_sources_prefers_feasible_meaningful_sources(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      page_file = repo_root / "src/pages/admin/PitchApplicationsPage.tsx"
      utility_file = repo_root / "src/lib/formatPitchStatus.ts"
      page_file.parent.mkdir(parents=True)
      utility_file.parent.mkdir(parents=True)
      page_file.write_text(
        """import { useQuery, useMutation } from '@tanstack/react-query';
import { useNavigate, Navigate } from 'react-router-dom';
import { useToast } from '@/hooks/use-toast';
import { useAuth } from '@/hooks/useAuth';
import { AdminLayout } from '@/components/admin/AdminLayout';
import { PitchTable } from '@/components/admin/PitchTable';
import { PitchFilters } from '@/components/admin/PitchFilters';
import { Button } from '@/components/ui/button';

export default function PitchApplicationsPage() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { toast } = useToast();
  const { data, isLoading, error } = useQuery({ queryKey: ['pitches'] });
  const mutation = useMutation({ mutationFn: async () => null });
  if (!user) return <Navigate to="/login" />;
  if (isLoading) return <div>Loading</div>;
  if (error) return <div>Error</div>;
  return <form onSubmit={() => mutation.mutate()}><button disabled={mutation.isPending}>Approve</button></form>;
}
""",
        encoding="utf-8",
      )
      utility_file.write_text(
        """export function formatPitchStatus(status: string) {
  if (status === 'approved') return 'Approved';
  if (status === 'rejected') return 'Rejected';
  return 'Pending';
}
""",
        encoding="utf-8",
      )

      result = discover_missing_test_sources(
        repo_path=str(repo_root),
        registry_path="src/test/test-registry.json",
      )

      self.assertTrue(result["ok"])
      self.assertEqual(
        result["missingTestSourceFiles"][0],
        "src/lib/formatPitchStatus.ts",
      )

  def test_discover_missing_test_sources_uses_existing_tests_and_registry(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      source_file = repo_root / "src/pages/HomePage.tsx"
      registered_source_file = repo_root / "src/pages/AboutPage.tsx"
      test_file = repo_root / "src/test/pages/HomePage.test.tsx"
      registered_test_file = repo_root / "src/test/pages/AboutPage.test.tsx"
      registry_file = repo_root / "src/test/test-registry.json"

      source_file.parent.mkdir(parents=True)
      test_file.parent.mkdir(parents=True)
      source_file.write_text(
        "export default function HomePage({ isLoading }) { return isLoading ? <div>Loading</div> : <div>Ready</div>; }\n",
        encoding="utf-8",
      )
      registered_source_file.write_text("export default function AboutPage() { return <div />; }\n", encoding="utf-8")
      test_file.write_text(TEST_CONTENT, encoding="utf-8")
      registered_test_file.write_text(TEST_CONTENT, encoding="utf-8")
      registry_file.write_text(json.dumps({
        "suites": [
          {
            "id": "pages",
            "command": "npm run test -- src/test/pages/AboutPage.test.tsx",
            "files": ["src/test/pages/AboutPage.test.tsx"],
            "relatedSource": ["src/pages/AboutPage.tsx"],
            "runWhenChanged": ["src/pages/AboutPage.tsx"],
          }
        ],
      }), encoding="utf-8")

      result = discover_missing_test_sources(
        repo_path=str(repo_root),
        registry_path="src/test/test-registry.json",
      )

      self.assertTrue(result["ok"])
      self.assertEqual(result["missingTestSourceFiles"], [])
      self.assertEqual(
        sorted(result["coveredSourceFiles"]),
        ["src/pages/AboutPage.tsx", "src/pages/HomePage.tsx"],
      )

  def test_inspect_context_reports_unregistered_candidate_tests_without_registry(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)
      source_file = repo_root / "src/pages/HomePage.tsx"
      test_file = repo_root / "src/test/pages/HomePage.test.tsx"
      source_file.parent.mkdir(parents=True)
      test_file.parent.mkdir(parents=True)
      source_file.write_text("export default function HomePage() { return <div />; }\n", encoding="utf-8")
      test_file.write_text(TEST_CONTENT, encoding="utf-8")

      result = json.loads(inspect_test_context(
        repo_path=str(repo_root),
        changed_files=["src/pages/HomePage.tsx"],
        registry_path="src/test/test-registry.json",
      ))

      self.assertTrue(result["ok"])
      self.assertEqual(result["matchedSuites"], [])
      self.assertEqual(result["allSuites"], [])
      self.assertIn("src/test/pages/HomePage.test.tsx", result["unregisteredCandidateTests"])

  def test_classifies_missing_vitest_setup_file_as_repairable(self):
    result = classify_failure_reason(
      "Error: Cannot find module '/@id/C:/Users/linds/fiestaconnect/src/test/setup.ts'"
    )

    self.assertEqual(result["classification"], "missing_test_setup_file")
    self.assertTrue(result["repairSafe"])

  def test_classifies_missing_generated_render_helper_as_repairable(self):
    result = classify_failure_reason(
      'Error: Failed to resolve import "@/test/utils/render" from "src/test/pages/HomePage.test.tsx". Does the file exist?'
    )

    self.assertEqual(result["classification"], "missing_test_support_import")
    self.assertTrue(result["repairSafe"])

  def test_classifies_null_smoke_render_as_repairable(self):
    result = classify_failure_reason(
      "received value must be an HTMLElement or an SVGElement. Received has type: Null. Received has value: null"
    )

    self.assertEqual(result["classification"], "null_smoke_render")
    self.assertTrue(result["repairSafe"])

  def test_classifies_missing_match_media_as_repairable(self):
    result = classify_failure_reason(
      "TypeError: window.matchMedia is not a function"
    )

    self.assertEqual(result["classification"], "missing_jsdom_browser_api")
    self.assertTrue(result["repairSafe"])

  def test_classifies_bad_testing_library_screen_import_as_repairable(self):
    result = classify_failure_reason(
      "TypeError: screen.getByRole is not a function"
    )

    self.assertEqual(result["classification"], "testing_library_screen_import_bug")
    self.assertTrue(result["repairSafe"])

  def test_classifies_missing_package_as_not_test_repair_safe(self):
    result = classify_failure_reason(
      "Error: Cannot find package '@vercel/og' imported from C:/repo/api/og.tsx"
    )

    self.assertEqual(result["classification"], "missing_dependency")
    self.assertFalse(result["repairSafe"])
    self.assertIn("@vercel/og", result["matched"])

  def test_classifies_failed_resolve_import_as_missing_dependency(self):
    result = classify_failure_reason(
      'Error: Failed to resolve import "qrcode" from "src/lib/pitchQRCode.ts". Does the file exist?'
    )

    self.assertEqual(result["classification"], "missing_dependency")
    self.assertFalse(result["repairSafe"])
    self.assertIn("qrcode", result["matched"])

  def test_infers_failing_test_file_from_stack_trace(self):
    failure_text = """
 FAIL  src/test/pages/community/CommunityPage.test.tsx > CommunityPage > renders without crashing
TypeError: screen.getByRole is not a function
 ❯ C:/Users/linds/fiestaconnect/src/test/pages/community/CommunityPage.test.tsx:9:19
"""

    self.assertEqual(
      infer_failing_test_files_from_failure_text(failure_text),
      ["src/test/pages/community/CommunityPage.test.tsx"],
    )

  def test_infers_failing_test_file_without_warning_stderr_headers(self):
    failure_text = """
Error: Render failed
    at C:/Users/linds/fiestaconnect/src/components/ErrorBoundary.test.tsx:36:19

stderr | src/test/pages/LandingPage.test.tsx > LandingPage > renders without crashing
React Router Future Flag Warning

stderr | src/test/pages/CalendarRedirectPage.test.tsx > CalendarRedirectPage > shows the redirect status
React Router Future Flag Warning
"""

    self.assertEqual(
      infer_failing_test_files_from_failure_text(failure_text),
      ["src/components/ErrorBoundary.test.tsx"],
    )

  def test_parses_pipe_separated_vitest_summary_counts(self):
    summary = parse_vitest_summary("""
 Test Files  1 failed | 2 passed (3)
      Tests  4 failed | 9 passed (13)
   Duration  8.65s
""")

    self.assertEqual(summary["testFiles"], {
      "total": 3,
      "passed": 2,
      "failed": 1,
    })
    self.assertEqual(summary["tests"], {
      "total": 13,
      "passed": 9,
      "failed": 4,
    })

  def test_parses_vitest_no_tests_summary(self):
    summary = parse_vitest_summary("""
 Test Files  3 failed (3)
      Tests  no tests
""")

    self.assertEqual(summary["testFiles"], {
      "total": 3,
      "passed": 0,
      "failed": 3,
    })
    self.assertEqual(summary["tests"], {
      "total": 0,
      "passed": 0,
      "failed": 0,
    })

  def test_find_failure_without_id_ignores_blocked_failures(self):
    queue = {
      "failures": [
        {"id": "old-failure", "status": "blocked"},
      ],
    }

    self.assertIsNone(find_failure(queue, None))

  def test_find_failure_without_id_returns_latest_open_failure(self):
    queue = {
      "failures": [
        {"id": "old-open", "status": "open"},
        {"id": "blocked", "status": "blocked"},
        {"id": "new-open", "status": "open"},
      ],
    }

    self.assertEqual(find_failure(queue, None)["id"], "new-open")

  def test_detects_stale_absolute_vitest_setup_file_path(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory) / "current-repo"
      repo_root.mkdir()
      stale_path = Path(directory) / "old-repo" / "src" / "test" / "setup.ts"
      failure_text = f"Error: Cannot find module '/@id/{stale_path}'"

      result = stale_missing_test_setup_path(repo_root, failure_text)

      self.assertEqual(result, stale_path.resolve())

  def test_allows_absolute_vitest_setup_file_path_inside_repo(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory) / "current-repo"
      setup_path = repo_root / "src" / "test" / "setup.ts"
      repo_root.mkdir()
      failure_text = f"Error: Cannot find module '/@id/{setup_path}'"

      result = stale_missing_test_setup_path(repo_root, failure_text)

      self.assertIsNone(result)

  def test_write_test_support_file_creates_setup_file(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)

      result = json.loads(write_test_support_file(
        repo_path=str(repo_root),
        file_path="src/test/setup.ts",
        content="import '@testing-library/jest-dom/vitest';\n",
      ))

      self.assertTrue(result["ok"])
      self.assertEqual(result["action"], "created")
      self.assertEqual(
        (repo_root / "src/test/setup.ts").read_text(encoding="utf-8"),
        "import '@testing-library/jest-dom/vitest';\n",
      )

  def test_write_test_support_file_creates_render_helper(self):
    with tempfile.TemporaryDirectory() as directory:
      repo_root = Path(directory)

      result = json.loads(write_test_support_file(
        repo_path=str(repo_root),
        file_path="src/test/utils/render.tsx",
        content="export function renderWithAppProviders() { return null; }\n",
      ))

      self.assertTrue(result["ok"])
      self.assertEqual(result["action"], "created")
      self.assertEqual(
        (repo_root / "src/test/utils/render.tsx").read_text(encoding="utf-8"),
        "export function renderWithAppProviders() { return null; }\n",
      )

  def test_write_test_support_file_rejects_arbitrary_source_file(self):
    with tempfile.TemporaryDirectory() as directory:
      result = json.loads(write_test_support_file(
        repo_path=directory,
        file_path="src/pages/HomePage.tsx",
        content="export default function HomePage() { return null; }\n",
      ))

      self.assertFalse(result["ok"])
      self.assertEqual(result["error"], "Only known test setup/support files may be written.")


if __name__ == "__main__":
  unittest.main()
