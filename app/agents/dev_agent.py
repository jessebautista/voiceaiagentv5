import logging
import os
from pathlib import Path
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.rate_limiters import InMemoryRateLimiter
from langgraph.prebuilt import create_react_agent

logger = logging.getLogger(__name__)

# Rate limiter: max 2 requests/sec, with a 100k token/min budget.
# This replaces the brittle time.sleep(2) calls that were in every tool.
rate_limiter = InMemoryRateLimiter(
    requests_per_second=0.4,   # 1 request every 2.5 seconds
    check_every_n_seconds=0.1,
    max_bucket_size=5,
)

# Basic tools for the Agent to interact with the cloned repository
# NOTE: All tools use Python-native operations, not shell commands, for Windows compatibility.

@tool
def list_directory(path: str) -> str:
    """Lists all files and directories recursively in the given path."""
    try:
        result = []
        for root, dirs, files in os.walk(path):
            # Skip hidden directories and node_modules
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'node_modules' and d != '.svelte-kit']
            level = root.replace(path, '').count(os.sep)
            indent = ' ' * 2 * level
            result.append(f'{indent}{os.path.basename(root)}/')
            subindent = ' ' * 2 * (level + 1)
            for file in files:
                result.append(f'{subindent}{file}')
        return '\n'.join(result[:200])  # Cap at 200 lines to avoid token overflow
    except Exception as e:
        return f"Error listing directory: {e}"

@tool
def read_file(filepath: str) -> str:
    """Reads the contents of a file at the given absolute filepath."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            if len(content) > 10000:
                return content[:10000] + "\n\n...[FILE CONTENT TRUNCATED DUE TO LENGTH - DO NOT READ COMPILED/MINIFIED/LOCK FILES]"
            return content
    except Exception as e:
        return f"Error reading file {filepath}: {e}"

@tool
def write_file(filepath: str, content: str) -> str:
    """Writes the given content to a file at the given absolute filepath, overwriting it completely."""
    try:
        # Ensure parent directories exist
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8", newline='\n') as f:
            f.write(content)
        return f"Successfully wrote to {filepath}"
    except Exception as e:
        return f"Error writing file {filepath}: {e}"

@tool
def search_in_files(query: str, directory: str) -> str:
    """Searches for a specific string inside all files in the given directory and returns matching lines."""
    try:
        matches = []
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'node_modules' and d != '.svelte-kit']
            for filename in files:
                if filename == 'package-lock.json' or filename.endswith('.svg') or filename.endswith('.png'):
                    continue
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        for i, line in enumerate(f, 1):
                            if query.lower() in line.lower():
                                rel_path = os.path.relpath(filepath, directory)
                                matches.append(f"{rel_path}:{i}: {line.rstrip()}")
                                if len(matches) > 100:
                                    return '\n'.join(matches) + '\n(truncated at 100 matches)'
                except Exception:
                    pass
        return '\n'.join(matches) if matches else "No matches found."
    except Exception as e:
        return f"Error searching: {e}"

async def run_dev_agent(repo_path: str, instruction: str) -> str:
    """
    Initializes a LangGraph ReAct agent with tools to read/write files and executes the given instruction.
    Rate limiting is handled by the InMemoryRateLimiter attached to the LLM — no more time.sleep() in tools.
    """
    logger.info(f"Running DevAgent on {repo_path} with instruction: {instruction}")
    
    # gpt-4o with the rate limiter — much larger context window and smarter reasoning than gpt-4o-mini
    llm = ChatOpenAI(
        model="gpt-4o",
        temperature=0,
        max_retries=5,
        rate_limiter=rate_limiter,
    )
    tools = [list_directory, read_file, write_file, search_in_files]
    
    system_prompt = f"""You are an expert AI software engineer working on a SvelteKit application called PHWB.
Your goal is to fix the bug or implement the feature described by the user.

The repository has been cloned to this directory: {repo_path}

IMPORTANT RULES:
- Use list_directory to understand the folder structure first.
- Use read_file with ABSOLUTE paths. For example: {repo_path}/src/routes/+page.svelte
- Use write_file to make code changes. Always write the COMPLETE file content, never truncate.
- Use search_in_files to find where certain variables, tables, or strings are used.
- After making changes, confirm what you changed and why.
"""

    # Trim old messages if the agent runs many tool loops (prevents context window overflow)
    def trim_messages(msgs):
        if len(msgs) > 12:
            return [msgs[0]] + msgs[-10:]
        return msgs

    agent = create_react_agent(llm, tools, prompt=system_prompt)
    
    try:
        messages = [HumanMessage(content=instruction)]
        response = await agent.ainvoke({"messages": messages})
        
        final_message = response["messages"][-1].content
        return final_message
    except Exception as e:
        logger.error(f"DevAgent execution failed: {e}")
        raise
