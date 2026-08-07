import importlib
import builtins
import sys
import types
import unittest


def install_fake_strands():
  if "strands" in sys.modules:
    return

  strands_module = types.ModuleType("strands")
  strands_module.tool = lambda function: function

  class FakeAgent:
    def __init__(self, *args, **kwargs):
      pass

    def __call__(self, *args, **kwargs):
      return "{}"

  strands_module.Agent = FakeAgent

  models_module = types.ModuleType("strands.models")
  ollama_module = types.ModuleType("strands.models.ollama")
  hooks_module = types.ModuleType("strands.hooks")

  class FakeOllamaModel:
    def __init__(self, *args, **kwargs):
      pass

  ollama_module.OllamaModel = FakeOllamaModel

  class FakeHookProvider:
    pass

  class FakeHookRegistry:
    def add_callback(self, *args, **kwargs):
      pass

  class FakeHookEvent:
    pass

  hooks_module.HookProvider = FakeHookProvider
  hooks_module.HookRegistry = FakeHookRegistry
  hooks_module.BeforeToolCallEvent = FakeHookEvent
  hooks_module.AfterToolCallEvent = FakeHookEvent

  sys.modules["strands"] = strands_module
  sys.modules["strands.models"] = models_module
  sys.modules["strands.models.ollama"] = ollama_module
  sys.modules["strands.hooks"] = hooks_module


install_fake_strands()
from tools.shared import validate_test_file_content

original_input = builtins.input
builtins.input = lambda prompt="": "exit"
main = importlib.import_module("main")
builtins.input = original_input


class DeterministicGeneratorTest(unittest.TestCase):
  def test_first_missing_batch_sources_have_safe_fallback_tests(self):
    for source_path in [
      "src/lib/resourceLogoUpload.ts",
      "src/lib/sessionRefresh.ts",
      "src/hooks/useReputation.ts",
      "src/hooks/useIncompletePitchApplication.ts",
    ]:
      with self.subTest(source_path=source_path):
        content = main.build_deterministic_utility_test_content(source_path, "")

        self.assertIsNotNone(content)
        self.assertEqual(validate_test_file_content(content), [])

  def test_missing_coverage_batches_are_single_source(self):
    self.assertEqual(main.CREATE_MISSING_TESTS_BATCH_SIZE, 1)


if __name__ == "__main__":
  unittest.main()
