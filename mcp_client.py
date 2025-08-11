#!/usr/bin/env python3
"""
Multi-MCP REPL (native Ollama)
- Spawns DB2 server via STDIO:
    1) DB2 db2_mcp.py
- Keeps both sessions open; merges tools for one LangGraph agent
- Commands:
    /tools                 -> list all tool names
    /health                -> call db2.healthcheck (if present)
    /toy                   -> call toy.ping
    /systems [days]        -> call db2.show_systems 
    /health [system_id]    -> call db2.system_health or db2.all_systems_health
    /problems              -> call db2.problem_areas
    /call <tool> <json>    -> call a tool directly with JSON args
    /manifest              -> summarize db2 schema manifest if available
    /ids [days]            -> call db2.discover_context
    /help  /exit
"""

import os, re, json, asyncio
from typing import List, Optional

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from langchain_ollama import ChatOllama

load_dotenv()

# --- paths (adjust if you moved them) ---
DB2_DIR = os.getenv("MCP_SERVER_DIR", r"C:\Users\danje\Projects\mcp-experiment")
DB2_PY  = os.getenv("MCP_SERVER_PATH", rf"{DB2_DIR}\servers\db2_mcp.py")

# TOY_DIR = os.getenv("TOY_SERVER_DIR", r"C:\Users\danje\Projects\mcp-experiment\servers")
# TOY_PY  = os.getenv("TOY_SERVER_PATH", rf"{TOY_DIR}\toy_server.py")

# --- ollama ---
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen3:14b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:28114")

SYSTEM_HINT = (
    "You have DB2 health monitoring tools available.\n"
    "If a query needs system/LPAR/processor IDs and they are not provided,\n"
    "call `db2.discover_context(days=30)` first. Prefer concise answers and do not reveal <think>.\n"
)

def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

async def _get_tool_by_name(tools, name: str):
    return next((t for t in tools if t.name == name), None)

async def _preload_manifest(tools) -> Optional[dict]:
    t = await _get_tool_by_name(tools, "db2.schema_manifest")
    if not t:
        return None
    res = await t.ainvoke({})
    raw = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
    d = json.loads(raw)
    return d.get("manifest", d)

def _summarize_manifest(manifest: dict) -> str:
    out = []
    for tbl, meta in manifest.get("tables", {}).items():
        keys = meta.get("key_columns", [])
        out.append(f"{tbl}: {', '.join(keys)}")
    return "\n".join(out) or "(no tables)"

async def _direct_call(tools, name: str, args: dict):
    t = await _get_tool_by_name(tools, name)
    if not t:
        print(f"❌ Tool not found: {name}")
        return
    res = await t.ainvoke(args)
    text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
    print(text)

async def repl(tools, llm):
    agent = create_react_agent(llm, tools)

    manifest = await _preload_manifest(tools)
    sys_msgs = [SystemMessage(content=SYSTEM_HINT)]
    if manifest:
        sys_msgs.append(SystemMessage(content=f"DB2_SCHEMA_MANIFEST:\n{json.dumps(manifest, separators=(',',':'))}"))

    print("\nCommands: /tools  /health  /toy  /systems [days]  /health [system_id]  /problems  /ids [days]  /manifest  /call <tool> <json>  /help  /exit")
    print("Tip: free text → agent with all tools.\n")

    while True:
        try:
            q = input("💬 Query> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Bye.")
            break

        if not q:
            print("ℹ️  Type /help for commands.")
            continue

        if q.lower().startswith("/exit"):
            print("👋 Bye.")
            break

        if q.lower().startswith("/help"):
            print("Commands: /tools  /health  /toy  /systems [days]  /health [system_id]  /problems  /ids [days]  /manifest  /call <tool> <json>  /exit")
            continue

        if q.lower().startswith("/tools"):
            print("Tools:", [t.name for t in tools])
            continue

        if q.lower() == "/health":
            await _direct_call(tools, "db2.healthcheck", {})
            continue

        # if q.lower().startswith("/toy"):
        #     await _direct_call(tools, "toy.ping", {})
        #     continue

        if q.lower().startswith("/manifest"):
            if manifest:
                print(_summarize_manifest(manifest))
            else:
                print("(no manifest available)")
            continue

        if q.lower().startswith("/systems"):
            parts = q.split()
            days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 7
            await _direct_call(tools, "db2.show_systems", {"days": days})
            continue

        if q.lower().startswith("/health "):
            parts = q.split()
            if len(parts) > 1:
                system_id = parts[1]
                await _direct_call(tools, "db2.system_health", {"system_id": system_id})
            else:
                await _direct_call(tools, "db2.all_systems_health", {})
            continue

        if q.lower().startswith("/problems"):
            await _direct_call(tools, "db2.problem_areas", {})
            continue

        if q.lower().startswith("/ids"):
            parts = q.split()
            days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
            await _direct_call(tools, "db2.discover_context", {"days": days})
            continue

        if q.lower().startswith("/call "):
            try:
                _, name, rest = q.split(" ", 2)
                args = json.loads(rest)
            except ValueError:
                print("Usage: /call <tool> <json-args>")
                continue
            except json.JSONDecodeError as e:
                print(f"Bad JSON: {e}")
                continue
            await _direct_call(tools, name, args)
            continue

        # Agent turn
        msgs = sys_msgs + [HumanMessage(content=q)]
        try:
            result = await agent.ainvoke({"messages": msgs})
            print("\n🤖", strip_think(result["messages"][-1].content), "\n")
        except Exception as e:
            print(f"\n⚠️  Agent error: {e}\n")

async def main():
    print(f"🧩 Multi-MCP REPL • model={OLLAMA_MODEL} • base={OLLAMA_BASE_URL}")

    # stdio launch params for DB2 server
    p_db2 = StdioServerParameters(command="uv", args=["run", DB2_PY], cwd=DB2_DIR, env=os.environ.copy())

    # keep DB2 session open during the whole REPL
    async with stdio_client(p_db2) as (r1, w1):
        async with ClientSession(read_stream=r1, write_stream=w1) as s1:
            await s1.initialize()
            print("✅ DB2 MCP session initialized")

            tools = await load_mcp_tools(s1)  # db2 server tools

            llm = ChatOllama(
                model=OLLAMA_MODEL,
                base_url=OLLAMA_BASE_URL,
                temperature=0.1,
                timeout=60.0,
            ).bind(stop=["</think>"])

            await repl(tools, llm)

if __name__ == "__main__":
    asyncio.run(main())
