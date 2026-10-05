from openhands.sdk import Agent, Conversation, LLM, Tool
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool

llm = LLM(
    model="ollama/qwen3:8b",
    ollama_base_url="http://127.0.0.1:11434",
    temperature=0,
)

agent = Agent(
    llm=llm,
    tools=[
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
    ],
    system_prompt="""
You are Bob, Head of QA.

Your job is to investigate software problems methodically.

You have access only to the project workspace provided to you.
Inspect files and execute commands when useful.
Do not modify files unless explicitly asked.

When investigating a bug:
1. Inspect the project.
2. Read relevant documentation and bug reports.
3. Reproduce the problem if possible.
4. Identify the likely root cause.
5. Report concrete evidence and your conclusion.

You are operating inside an isolated Docker workspace.
""",
)

conversation = Conversation(
    agent=agent,
    workspace="/workspace",
    persistence_dir="/bob-state",
    delete_on_close=False,
)

conversation.send_message(
    """
Reply with exactly:
BOB IS ONLINE
"""
)

conversation.run()
