from strands import Agent, tool
from strands.models.ollama import OllamaModel
import json
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

# tool agent - discover & run tests
agent = Agent(
  model=model,
  tools=[
    tools.find_relevant_test_suites, 
    tools.run_test_suite
    ],
  system_prompt=instructions,
    hooks=[
      hooks.PathValidationHook(),
      hooks.TestResultAuditHook(),
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


config = json.loads(Path("config.json").read_text(encoding="utf-8"))

format_template = Path("templates/results-summary.md").read_text(encoding="utf-8")

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
Summarize these verified results using the format template below.

Rules:
- Use only the data provided in this prompt.
- Do not invent file paths, test names, commands, counts, or errors.
- If something is missing, write "Not provided".
- Do not call tools.

Format template:
{format_template}

matchedSuites:
{json.dumps(matched_data["matchedSuites"], indent=2)}

runResults:
{json.dumps(run_results, indent=2)}
"""

result = summary_agent(test_run)

Path("output.md").write_text(str(result), encoding="utf-8")

while True:
  user_input = input("\n> ")
  if user_input.lower() in ("quit", "exit"):
    break
  if user_input.lower() == ("run"):
    result = summary_agent(user_input)
  else: 
    result = agent(user_input)  
Path("output.md").write_text(str(result), encoding="utf-8")
