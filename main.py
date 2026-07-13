from strands import Agent, tool
from strands.models.ollama import OllamaModel
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from fnmatch import fnmatch
import json
from pathlib import Path
import tools

model = OllamaModel(
    host="",  
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

agent = Agent(
  model=model,
  tools=[
    tools.find_relevant_test_suites, 
    tools.run_test_suite
    ],
  system_prompt=instructions
)

config = json.loads(Path("config.json").read_text(encoding="utf-8"))

project_name = config["defaultProject"]
project = config["projects"][project_name]

repo_path = project["path"]
registry_path = project["registry"]

matched_json = tools.find_relevant_test_suites(
  repo_path=repo_path,
  changed_files=[
    "src/components/community/ChannelMessage.tsx",
    "src/pages/community/ChannelDetailPage.tsx"
  ],
  registry_path=registry_path
)

matched_data = json.loads(matched_json)

run_results = []

for suite in matched_data["matchedSuites"]:
  run_json = tools.run_test_suite(
    repo_path=repo_path,
    suite_id=suite["id"],
    registry_path=registry_path
  )

  run_results.append(json.loads(run_json))

test_run = f"""
Summarize these verified results.

Do not call tools.
Do not write function-call JSON.
Do not use placeholders.
Only summarize the data below.

matchedSuites:
{json.dumps(matched_data["matchedSuites"], indent=2)}

runResults:
{json.dumps(run_results, indent=2)}
"""

result = agent(test_run)

Path("output.md").write_text(str(result), encoding="utf-8")

# agent(f"""
# Use the find_relevant_test_suites tool.

# repo_path = "{project["path"]}"
# registry_path = "{project["registry"]}"

# Ask the user which files changed if they have not provided them.
# Do not guess test files if the tool fails.
# """)

while True:
  user_input = input("\n> ")
  if user_input.lower() in ("quit", "exit"):
    break
  if user_input.lower() == ("run"):
    user_input = test_run
  result = agent(user_input) 
Path("output.md").write_text(str(result), encoding="utf-8")
