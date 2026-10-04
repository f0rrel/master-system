from pathlib import Path
import json
import requests

BOB_DIR = Path("agents/bob")
WORKSPACE = BOB_DIR / "workspace"

identity = (BOB_DIR / "identity.md").read_text()


def list_files():
    files = []

    for path in WORKSPACE.rglob("*"):
        if path.is_file():
            files.append(str(path.relative_to(WORKSPACE)))

    return files


tools = """
You have access to this tool:

list_files
- Lists all files in your workspace.
- Takes no arguments.
"""

task = """
Investigate the reported bug in the workspace.

You need to determine which files are relevant.

You may use the available tools when necessary.

When you have enough information, explain what you would investigate next.
Do not modify any files.
"""

prompt = f"""
{identity}

AVAILABLE TOOLS:
{tools}

TASK:
{task}

You must decide whether you need to use a tool.

If you want to use list_files, respond EXACTLY in this format:

TOOL: list_files

If you don't need a tool, respond:

FINAL:
<your answer>
"""

while True:
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "qwen2.5-coder:7b",
            "prompt": prompt,
            "stream": False,
        },
    )

    response.raise_for_status()

    answer = response.json()["response"].strip()

    print("\n--- BOB ---\n")
    print(answer)

    if answer.startswith("TOOL: list_files"):
        result = list_files()

        prompt = f"""
{identity}

AVAILABLE TOOLS:
{tools}

TASK:
{task}

You requested the tool:

list_files

The tool returned:

{json.dumps(result, indent=2)}

Now continue your investigation.

If you need another tool call, request it using:

TOOL: list_files

Otherwise respond:

FINAL:
<your answer>
"""
    else:
        break
