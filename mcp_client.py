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
    "You have DB2 mainframe health monitoring tools available.\n\n"
    "HEALTH LEVELS (always explain when first mentioned):\n"
    "- Level 0: Not Applicable\n"
    "- Level 1: Good (healthy)\n"
    "- Level 2: Warning (needs monitoring)\n"
    "- Level 3+: Critical (requires immediate attention)\n\n"
    "USAGE TIPS:\n"
    "- If a query needs system/LPAR/processor IDs and they are not provided, call `db2.discover_context(days=30)` first\n"
    "- When user asks about 'rules' or 'what rules exist', use the schema manifest tool\n"
    "- Always provide executive summaries before detailed analysis\n"
    "- Suggest next steps when appropriate\n"
    "- Prefer concise answers and do not reveal <think>\n"
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

def _format_systems_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"❌ Error: {data.get('error', 'Unknown error')}")
            return
            
        systems = data.get("systems", [])
        days = data.get("days", 7)
        
        print(f"📊 **Mainframe Systems** (last {days} days)")
        print(f"**Total systems:** {len(systems)}\n")
        
        for i, sys in enumerate(systems, 1):
            critical = sys['critical_issues']
            warnings = sys['warnings']
            total = sys['total_records']
            status = "🔴" if critical > 0 else "🟡" if warnings > 0 else "🟢"
            
            print(f"{i}. **{sys['system_id']}** {status}")
            print(f"   - Records: {total:,}")
            print(f"   - Critical: {critical}")
            print(f"   - Warnings: {warnings}\n")
            
    except Exception as e:
        print(f"❌ Format error: {e}")
        print(json_text)

def _format_system_health_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"❌ Error: {data.get('error', 'Unknown error')}")
            return
            
        system_id = data.get("system_id")
        summary = data.get("summary", {})
        rule_groups = data.get("rule_groups", [])
        days = data.get("days", 7)
        
        total = summary.get("total", 0)
        critical = summary.get("critical", 0)
        warning = summary.get("warning", 0)
        good = summary.get("good", 0)
        
        crit_pct = (critical / total * 100) if total > 0 else 0
        warn_pct = (warning / total * 100) if total > 0 else 0
        
        print(f"🏥 **System {system_id} Health** (last {days} days)")
        print(f"**Overall:** {total:,} rules checked")
        print(f"- 🔴 Critical: {critical} ({crit_pct:.1f}%)")
        print(f"- 🟡 Warnings: {warning} ({warn_pct:.1f}%)")
        print(f"- 🟢 Good: {good}\n")
        
        if rule_groups:
            print("**By Rule Group:**")
            for rg in rule_groups:
                if rg['critical'] > 0 or rg['warning'] > 0:
                    status = "🔴" if rg['critical'] > 0 else "🟡"
                    print(f"- **{rg['rule_group']}** {status}: {rg['critical']} critical, {rg['warning']} warnings")
                    
    except Exception as e:
        print(f"❌ Format error: {e}")
        print(json_text)

def _format_all_systems_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"❌ Error: {data.get('error', 'Unknown error')}")
            return
            
        systems_summary = data.get("systems_summary", {})
        days = data.get("days", 7)
        
        print(f"🌍 **Estate-Wide Health Overview** (last {days} days)")
        print(f"**Systems:** {len(systems_summary)}\n")
        
        for sys_id, summary in systems_summary.items():
            critical = summary.get("critical", 0)
            warning = summary.get("warning", 0)
            total = summary.get("total", 0)
            status = "🔴" if critical > 0 else "🟡" if warning > 0 else "🟢"
            
            print(f"**{sys_id}** {status}: {critical} critical, {warning} warnings ({total:,} total)")
            
    except Exception as e:
        print(f"❌ Format error: {e}")
        print(json_text)

def _format_problems_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"❌ Error: {data.get('error', 'Unknown error')}")
            return
            
        exec_summary = data.get("executive_summary", {})
        system_breakdown = data.get("system_breakdown", [])
        top_issues = data.get("top_critical_issues", [])
        days = data.get("days", 7)
        
        total_critical = exec_summary.get("total_critical", 0)
        total_warnings = exec_summary.get("total_warnings", 0)
        systems_affected = exec_summary.get("systems_affected", 0)
        priority_systems = exec_summary.get("priority_systems", [])
        
        print(f"⚠️  **IMMEDIATE ATTENTION NEEDED** (last {days} days)")
        print(f"**Executive Summary:**")
        print(f"- 🔴 Critical Issues: {total_critical}")
        print(f"- 🟡 Warning Issues: {total_warnings}")
        print(f"- 🖥️ Systems Affected: {systems_affected}\n")
        
        if priority_systems:
            print("**Priority Systems (Most Critical):**")
            for i, sys in enumerate(priority_systems, 1):
                print(f"{i}. **{sys['system_id']}** {sys['rule_group']}: {sys['critical']} critical, {sys['warnings']} warnings")
            print()
        
        if top_issues:
            print("**Top Critical Issues:**")
            for i, issue in enumerate(top_issues[:5], 1):
                print(f"{i}. **{issue['system_id']}** 🔴 {issue['rule_group']}: {issue['description'][:50]}...")
                
            if len(top_issues) > 5:
                print(f"\n*... and {len(top_issues)-5} more critical issues*")
            print("\n*Ask 'What specific issues need attention?' for detailed analysis*")
                
    except Exception as e:
        print(f"❌ Format error: {e}")
        print(json_text)

async def _direct_call(tools, name: str, args: dict):
    t = await _get_tool_by_name(tools, name)
    if not t:
        print(f"❌ Tool not found: {name}")
        return
    res = await t.ainvoke(args)
    text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
    
    # Format specific tool outputs for better UX
    if name == "db2.show_systems":
        _format_systems_output(text)
    elif name == "db2.system_health":
        _format_system_health_output(text)
    elif name == "db2.all_systems_health":
        _format_all_systems_output(text)
    elif name == "db2.problem_areas":
        _format_problems_output(text)
    else:
        print(text)

async def repl(tools, llm):
    agent = create_react_agent(llm, tools)

    manifest = await _preload_manifest(tools)
    sys_msgs = [SystemMessage(content=SYSTEM_HINT)]
    if manifest:
        sys_msgs.append(SystemMessage(content=f"DB2_SCHEMA_MANIFEST:\n{json.dumps(manifest, separators=(',',':'))}"))

    print("\nCommands: /tools  /health  /systems [days]  /health [system_id]  /problems  /ids [days]  /manifest  /call <tool> <json>  /help  /exit")
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
            print("Commands: /tools  /health  /systems [days]  /health [system_id]  /problems  /ids [days]  /manifest  /call <tool> <json>  /exit")
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
