import unittest
import importlib.util
from pathlib import Path


def load_shared_module():
  shared_path = Path(__file__).resolve().parents[1] / "tools" / "shared.py"
  spec = importlib.util.spec_from_file_location("shared", shared_path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


validate_test_file_content = load_shared_module().validate_test_file_content
parse_file_paths_argument = load_shared_module().parse_file_paths_argument
component_requires_props = load_shared_module().component_requires_props
source_test_intent = load_shared_module().source_test_intent


class ValidateTestFileContentTest(unittest.TestCase):
  def test_accepts_raw_vitest_source(self):
    content = """import { describe, expect, it } from 'vitest';

describe('Example', () => {
  it('works', () => {
    expect(true).toBe(true);
  });
});
"""

    self.assertEqual(validate_test_file_content(content), [])

  def test_rejects_json_wrapped_concatenated_source(self):
    content = (
      '{"import { describe, expect, it } from '
      "'vitest';\\n\" + \"import CalendarRedirectPage from "
      "'@/pages/CalendarRedirectPage';\\n\\ndescribe('CalendarRedirectPage', () => {\\n"
      "  it('redirects to calendar features', () => {});\\n});\"}"
    )

    self.assertIn(
      "content appears to be serialized or string-concatenated test source; pass raw test file content instead.",
      validate_test_file_content(content),
    )

  def test_rejects_shallow_render_smoke_test(self):
    content = """import { describe, expect, it } from 'vitest';

describe('Card', () => {
  it('renders without crashing', () => {
    const { container } = renderWithAppProviders(<Card />);

    expect(container.firstChild).toBeInTheDocument();
  });
});
"""

    self.assertIn(
      "test only asserts a shallow render smoke check; assert user-visible behavior, hook return values, function output, or interactions instead.",
      validate_test_file_content(content),
    )

  def test_rejects_lowercase_jsx_pseudo_component(self):
    content = """import { describe, expect, it } from 'vitest';

describe('parseMentions', () => {
  it('renders mentions', () => {
    render(<parseMentions />);
  });
});
"""

    self.assertIn(
      "test renders a lowercase JSX tag; call utilities directly or render the real exported React component with an uppercase name.",
      validate_test_file_content(content),
    )


class ParseFilePathsArgumentTest(unittest.TestCase):
  def test_parses_comma_separated_paths(self):
    self.assertEqual(
      parse_file_paths_argument("src/App.tsx, src/hooks/useApp.ts"),
      ["src/App.tsx", "src/hooks/useApp.ts"],
    )

  def test_ignores_empty_items_and_outer_whitespace(self):
    self.assertEqual(
      parse_file_paths_argument(" src/App.tsx, ,src/Card.tsx, "),
      ["src/App.tsx", "src/Card.tsx"],
    )

  def test_blank_input_returns_empty_list(self):
    self.assertEqual(parse_file_paths_argument(""), [])
    self.assertEqual(parse_file_paths_argument(None), [])


class ComponentRequiresPropsTest(unittest.TestCase):
  def test_function_without_props_does_not_require_props(self):
    self.assertFalse(
      component_requires_props(
        "export function EmptyState() { return <div />; }",
        "EmptyState",
      )
    )

  def test_function_with_typed_props_requires_props(self):
    self.assertTrue(
      component_requires_props(
        "export function MemberCard({ member }: MemberCardProps) { return <div />; }",
        "MemberCard",
      )
    )

  def test_arrow_component_with_typed_props_requires_props(self):
    self.assertTrue(
      component_requires_props(
        "export const CommunityHeader = ({ community }: CommunityHeaderProps) => <div />;",
        "CommunityHeader",
      )
    )

  def test_typed_react_fc_requires_props(self):
    self.assertTrue(
      component_requires_props(
        "export const FlagContentDialog: React.FC<FlagContentDialogProps> = (props) => <div />;",
        "FlagContentDialog",
      )
    )


class SourceTestIntentTest(unittest.TestCase):
  def test_scores_hooks_with_async_states_as_actionable(self):
    result = source_test_intent(
      "src/hooks/useConnections.ts",
      """export function useConnections() {
  const { data, isLoading, error } = useQuery({ queryKey: ['connections'] });
  return { data, isLoading, error };
}
""",
    )

    self.assertTrue(result["shouldTest"])
    self.assertIn("hook", result["testKinds"])
    self.assertGreaterEqual(result["score"], result["threshold"])
    self.assertIn("feasibilityScore", result)
    self.assertIn("selectionScore", result)

  def test_scores_simple_utilities_as_more_feasible_than_complex_pages(self):
    utility = source_test_intent(
      "src/lib/formatPitchStatus.ts",
      """export function formatPitchStatus(status: string) {
  if (status === 'approved') return 'Approved';
  if (status === 'rejected') return 'Rejected';
  return 'Pending';
}
""",
    )
    page = source_test_intent(
      "src/pages/admin/PitchApplicationsPage.tsx",
      """import { useQuery, useMutation } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
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
    )

    self.assertTrue(utility["shouldTest"])
    self.assertTrue(page["shouldTest"])
    self.assertGreater(utility["feasibilityScore"], page["feasibilityScore"])
    self.assertGreater(utility["selectionScore"], page["selectionScore"])

  def test_defers_presentational_ui_wrapper(self):
    result = source_test_intent(
      "src/components/ui/alert.tsx",
      "export const Alert = ({ children }) => <div role=\"alert\">{children}</div>;\n",
    )

    self.assertFalse(result["shouldTest"])
    self.assertEqual(result["priority"], "low")


if __name__ == "__main__":
  unittest.main()
