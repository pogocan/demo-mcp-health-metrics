#!/usr/bin/env python3
"""
MCP DB2 Health Monitoring REPL
- Spawns DB2 MCP server via STDIO
- LangChain integration with Ollama for AI-powered analysis
- Commands:
    /tools                 -> list all tool names
    /health                -> call db2.healthcheck (if present)
    /systems [days]        -> call db2.show_systems 
    /health [system_id]    -> call db2.system_health or db2.all_systems_health
    /problems              -> call db2.problem_areas
    /call <tool> <json>    -> call a tool directly with JSON args
    /manifest              -> summarize db2 schema manifest if available
    /ids [days]            -> call db2.discover_context
    /components            -> call db2.installed_components  
    /resources             -> view available MCP resources
    /help  /exit
"""

import os, re, json, asyncio, threading, time
from typing import List, Optional

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_mcp_adapters.resources import load_mcp_resources
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from langchain_ollama import ChatOllama

load_dotenv()

# --- paths (adjust if you moved them) ---
DB2_DIR = os.getenv("MCP_SERVER_DIR", r"C:\Users\danje\Projects\mcp-experiment")
DB2_PY  = os.getenv("MCP_SERVER_PATH", rf"{DB2_DIR}\servers\db2_mcp.py")


# --- ollama ---
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen3:14b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:28114")

SYSTEM_HINT = (
    "You have mainframe health monitoring tools available.\n\n"
    "IMPORTANT NAMING CLARIFICATION:\n"
    "- Tools named 'db2.*' = Database query tools (for accessing DB2 database)\n"
    "- 'DB2 component' = Mainframe monitoring component (different from db2.* tools)\n"
    "- 'KPMDB2 component' = Performance monitoring component for DB2 technology\n\n"
    "CRITICAL: You have MCP_RESOURCES with immediate answers - DO NOT call tools for explanations:\n"
    "- Health levels (0-4) with actions are in MCP_RESOURCES - answer directly\n"
    "- Component differences (DB2 vs KPMDB2) are in MCP_RESOURCES component_aspects\n"
    "- Schema info (tables, columns) is in MCP_RESOURCES - answer directly\n"
    "- Component priorities are in MCP_RESOURCES - use for recommendations\n"
    "- ONLY call db2.* tools for live database queries (current metrics, status)\n\n"
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
    "- Prefer concise answers and do not reveal <think>\n\n"
    "MANAGEMENT REPORTING:\n"
    "- For management queries ('summary for boss', 'executive summary', 'management report', 'what should I tell my manager'), use `db2.management_summary` tool\n"
    "- Management summaries should be CONCISE - focus on top 3 issues, business impact, and action items\n"
    "- Translate technical terms: DB2Z = Database Performance, DASD = Storage Systems, LPAR = Resource Allocation\n"
    "- Include risk levels and resolution timeframes, but keep responses brief and actionable\n"
)

def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

class ProgressSpinner:
    def __init__(self, message="Thinking"):
        self.message = message
        self.spinner_chars = "|/-\\"
        self.running = False
        self.thread = None
        
    def _spin(self):
        i = 0
        while self.running:
            print(f"\r{self.message}... {self.spinner_chars[i % len(self.spinner_chars)]}", end="", flush=True)
            time.sleep(0.2)
            i += 1
            
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._spin)
        self.thread.daemon = True
        self.thread.start()
        
    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        print("\r" + " " * (len(self.message) + 10) + "\r", end="", flush=True)

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
            print(f"Error: {data.get('error', 'Unknown error')}")
            return
            
        systems = data.get("systems", [])
        days = data.get("days", 7)
        
        print(f"Mainframe Systems (last {days} days)")
        print(f"Total systems: {len(systems)}\n")
        
        for i, sys in enumerate(systems, 1):
            critical = sys['critical_issues']
            warnings = sys['warnings']
            total = sys['total_records']
            status = "[CRIT]" if critical > 0 else "[WARN]" if warnings > 0 else "[OK]"
            
            print(f"{i}. {sys['system_id']} {status}")
            print(f"   - Records: {total:,}")
            print(f"   - Critical: {critical}")
            print(f"   - Warnings: {warnings}\n")
            
    except Exception as e:
        print(f"Format error: {e}")
        print(json_text)

def _format_system_health_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"Error: {data.get('error', 'Unknown error')}")
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
        
        print(f"System {system_id} Health (last {days} days)")
        print(f"Overall: {total:,} rules checked")
        print(f"- Critical: {critical} ({crit_pct:.1f}%)")
        print(f"- Warnings: {warning} ({warn_pct:.1f}%)")
        print(f"- Good: {good}\n")
        
        if rule_groups:
            print("By Rule Group:")
            for rg in rule_groups:
                if rg['critical'] > 0 or rg['warning'] > 0:
                    status = "[CRIT]" if rg['critical'] > 0 else "[WARN]"
                    print(f"- {rg['rule_group']} {status}: {rg['critical']} critical, {rg['warning']} warnings")
                    
    except Exception as e:
        print(f"Format error: {e}")
        print(json_text)

def _format_all_systems_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"Error: {data.get('error', 'Unknown error')}")
            return
            
        systems_summary = data.get("systems_summary", {})
        days = data.get("days", 7)
        
        print(f"Estate-Wide Health Overview (last {days} days)")
        print(f"Systems: {len(systems_summary)}\n")
        
        for sys_id, summary in systems_summary.items():
            critical = summary.get("critical", 0)
            warning = summary.get("warning", 0)
            total = summary.get("total", 0)
            status = "[CRIT]" if critical > 0 else "[WARN]" if warning > 0 else "[OK]"
            
            print(f"{sys_id} {status}: {critical} critical, {warning} warnings ({total:,} total)")
            
    except Exception as e:
        print(f"Format error: {e}")
        print(json_text)

def _format_problems_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"Error: {data.get('error', 'Unknown error')}")
            return
            
        exec_summary = data.get("executive_summary", {})
        system_breakdown = data.get("system_breakdown", [])
        top_issues = data.get("top_critical_issues", [])
        days = data.get("days", 7)
        
        total_critical = exec_summary.get("total_critical", 0)
        total_warnings = exec_summary.get("total_warnings", 0)
        systems_affected = exec_summary.get("systems_affected", 0)
        priority_systems = exec_summary.get("priority_systems", [])
        
        print(f"IMMEDIATE ATTENTION NEEDED (last {days} days)")
        print(f"Executive Summary:")
        print(f"- Critical Issues: {total_critical}")
        print(f"- Warning Issues: {total_warnings}")
        print(f"- Systems Affected: {systems_affected}\n")
        
        if priority_systems:
            print("Priority Systems (Most Critical):")
            for i, sys in enumerate(priority_systems, 1):
                print(f"{i}. {sys['system_id']} {sys['rule_group']}: {sys['critical']} critical, {sys['warnings']} warnings")
            print()
        
        if top_issues:
            print("Top Critical Issues:")
            for i, issue in enumerate(top_issues[:5], 1):
                print(f"{i}. {issue['system_id']} [CRIT] {issue['rule_group']}: {issue['description'][:50]}...")
                
            if len(top_issues) > 5:
                print(f"\n... and {len(top_issues)-5} more critical issues")
            print("\nAsk 'What specific issues need attention?' for detailed analysis")
                
    except Exception as e:
        print(f"Format error: {e}")
        print(json_text)

def _format_discover_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"Error: {data.get('error', 'Unknown error')}")
            return
            
        rows = data.get("rows", [])
        days = data.get("days", 30)
        
        print(f"System Discovery (last {days} days)")
        print(f"Found {len(rows)} system/LPAR/processor combinations\n")
        
        # Group by system
        systems = {}
        for row in rows:
            sys_id = row.get("MVS_SYSTEM_ID", "").strip()
            lpar = row.get("LPAR_NAME", "").strip() or "[No LPAR]"
            proc_type = row.get("PROCESSOR_TYPE", "").strip() or "[No Type]"
            count = row.get("count", 0)
            
            if sys_id not in systems:
                systems[sys_id] = []
            systems[sys_id].append({"lpar": lpar, "proc_type": proc_type, "count": count})
        
        # Display by system
        for sys_id, configs in systems.items():
            print(f"System: {sys_id}")
            
            # Sort by count descending to show most active first
            configs.sort(key=lambda x: x["count"], reverse=True)
            
            for config in configs[:10]:  # Show top 10 per system
                print(f"  - LPAR: {config['lpar']}, Type: {config['proc_type']} ({config['count']} records)")
                
            if len(configs) > 10:
                print(f"  ... and {len(configs)-10} more configurations")
            print()
                
    except Exception as e:
        print(f"Format error: {e}")
        print(json_text)

def _format_components_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"Error: {data.get('error', 'Unknown error')}")
            return
            
        total = data.get("total_components", 0)
        installed = data.get("installed_components", [])
        not_installed = data.get("not_installed_components", [])
        other = data.get("other_status_components", [])
        
        print(f"Component Status Overview")
        print(f"Total components: {total}")
        print(f"- Installed: {len(installed)}")
        print(f"- Not installed: {len(not_installed)}")
        if other:
            print(f"- Other status: {len(other)}")
        print()
        
        if installed:
            print("Installed Components:")
            # Limit to first 20 to prevent overwhelming output
            for i, comp in enumerate(installed[:20], 1):
                desc = comp.get("description", "")[:60] + "..." if len(comp.get("description", "")) > 60 else comp.get("description", "")
                time_installed = comp.get("time_installed", "")[:10] if comp.get("time_installed") else ""  # Just date part
                user_id = comp.get("user_id", "")
                
                print(f"{i}. {comp['component_name']} [INSTALLED]")
                if desc:
                    print(f"   {desc}")
                if time_installed or user_id:
                    install_info = []
                    if time_installed:
                        install_info.append(f"Installed: {time_installed}")
                    if user_id:
                        install_info.append(f"By: {user_id}")
                    print(f"   {' | '.join(install_info)}")
            
            if len(installed) > 20:
                print(f"   ... and {len(installed) - 20} more installed components")
            print()
        
        if not_installed:
            print("Not Installed Components:")
            # Limit to first 15 to prevent overwhelming output
            for i, comp in enumerate(not_installed[:15], 1):
                print(f"{i}. {comp['component_name']}")
            
            if len(not_installed) > 15:
                print(f"   ... and {len(not_installed) - 15} more not installed components")
            print()
            
        if other:
            print("Other Status Components:")
            for i, comp in enumerate(other, 1):
                print(f"{i}. {comp['component_name']} [{comp['status']}]")
                
    except Exception as e:
        print(f"Format error: {e}")
        print(json_text)

def _format_find_components_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"Error: {data.get('error', 'Unknown error')}")
            return
            
        search_pattern = data.get("search_pattern", "")
        total_found = data.get("total_found", 0)
        installed_by_aspect = data.get("installed_by_aspect", {})
        not_installed_by_aspect = data.get("not_installed_by_aspect", {})
        
        print(f"{search_pattern.upper()}-Related Components")
        print(f"Total found: {total_found}")
        print()
        
        if installed_by_aspect:
            print("INSTALLED Components by Aspect:")
            for aspect, components in installed_by_aspect.items():
                print(f"\\n📊 {aspect}:")
                for comp in components:
                    time_installed = comp.get("time_installed", "")[:10] if comp.get("time_installed") else ""
                    user_id = comp.get("user_id", "")
                    install_info = f" (Installed: {time_installed} by {user_id})" if time_installed else ""
                    print(f"   ✅ {comp['component_name']}: {comp['description'][:50]}...{install_info}")
        
        if not_installed_by_aspect:
            print("\\nNOT INSTALLED Components by Aspect:")
            for aspect, components in not_installed_by_aspect.items():
                print(f"\\n📋 {aspect}:")
                for comp in components[:5]:  # Limit to 5 per aspect
                    print(f"   ❌ {comp['component_name']}: {comp['description'][:50]}...")
                if len(components) > 5:
                    print(f"   ... and {len(components) - 5} more in this aspect")
                    
    except Exception as e:
        print(f"Format error: {e}")
        print(json_text)

def _format_recommendations_output(json_text: str):
    try:
        data = json.loads(json_text)
        if not data.get("ok"):
            print(f"Error: {data.get('error', 'Unknown error')}")
            return
            
        focus_area = data.get("focus_area", "")
        coverage = data.get("coverage_percentage", 0)
        installation_plan = data.get("installation_plan", [])
        next_action = data.get("next_action", {})
        
        print(f"Component Recommendations - {focus_area.upper()} Focus")
        print(f"Coverage: {coverage}% of recommended components installed")
        print()
        
        if installation_plan:
            print("📋 Installation Plan:")
            for phase in installation_plan:
                print(f"\\n{phase['phase']}:")
                for comp in phase['components']:
                    print(f"   • {comp}")
                print(f"   Reason: {phase['reason']}")
            
            print(f"\\n🎯 Next Action:")
            if "components" in next_action:
                print(f"   {next_action['phase']}")
                print(f"   Components: {', '.join(next_action['components'])}")
            else:
                print(f"   {next_action.get('message', 'No action needed')}")
        else:
            print("✅ All recommended components are installed!")
            print("Your monitoring coverage is complete for this focus area.")
            
    except Exception as e:
        print(f"Format error: {e}")
        print(json_text)

async def _direct_call(tools, name: str, args: dict):
    t = await _get_tool_by_name(tools, name)
    if not t:
        print(f"Error: Tool not found: {name}")
        return
    
    # Show spinner for potentially slow queries
    spinner_message = "Querying DB2"
    if "systems" in name:
        spinner_message = "Loading systems"
    elif "health" in name:
        spinner_message = "Analyzing health"
    elif "problems" in name:
        spinner_message = "Finding problems"
    
    spinner = ProgressSpinner(spinner_message)
    spinner.start()
    
    try:
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
        elif name == "db2.discover_context":
            _format_discover_output(text)
        elif name == "db2.installed_components":
            _format_components_output(text)
        elif name == "db2.find_components":
            _format_find_components_output(text)
        elif name == "db2.component_recommendations":
            _format_recommendations_output(text)
        else:
            print(text)
    finally:
        spinner.stop()

async def _load_mcp_resources(session):
    """Load MCP resources and return as context strings"""
    try:
        resources = await load_mcp_resources(session)
        resource_context = []
        
        for resource in resources:
            # Convert LangChain Blob to text
            if hasattr(resource, 'as_string'):
                content = resource.as_string()
            else:
                content = str(resource)
            resource_context.append(f"RESOURCE: {content}")
        
        return "\n".join(resource_context) if resource_context else ""
    except Exception as e:
        print(f"Warning: Could not load MCP resources: {e}")
        return ""

async def repl(tools, llm, resource_context=""):
    agent = create_react_agent(llm, tools)

    manifest = await _preload_manifest(tools)
    sys_msgs = [SystemMessage(content=SYSTEM_HINT)]
    if manifest:
        sys_msgs.append(SystemMessage(content=f"DB2_SCHEMA_MANIFEST:\n{json.dumps(manifest, separators=(',',':'))}"))
    
    # Add MCP resources as context
    if resource_context:
        # Make resources more prominent and specific
        resource_instruction = (
            "MANDATORY: Use the following MCP_RESOURCES for instant answers - DO NOT call tools for these:\n\n"
            "FOR HEALTH LEVEL QUESTIONS: Level 3 = Critical (requires immediate attention, take corrective action within 24 hours)\n"
            "FOR COMPONENT DIFFERENCES: DB2 = Core component, KPMDB2 = Performance_Monitoring (key performance metrics)\n"
            "FOR SCHEMA QUESTIONS: All table/column info is below\n\n"
            "If user asks about health levels, component differences, or schema - answer from below data ONLY:\n\n"
            f"{resource_context}\n\n"
            "ONLY call db2.* tools for live database queries (current metrics, system status, installation checks)."
        )
        sys_msgs.append(SystemMessage(content=resource_instruction))
        print("Loaded MCP resources: health-levels, component-hierarchy, schema-summary")
        # Debug: Show first 200 chars of resources
        print(f"Debug - Resource content preview: {resource_context[:200]}...")
    else:
        print("Warning: No MCP resources loaded")

    print("\nCommands: /tools  /health  /systems [days]  /health [system_id]  /problems  /ids [days]  /components  /resources  /manifest  /call <tool> <json>  /help  /exit")
    print("Tip: free text -> agent with all tools.\n")

    while True:
        try:
            q = input("Query> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not q:
            print("Type /help for commands.")
            continue

        if q.lower().startswith("/exit"):
            print("Bye.")
            break

        if q.lower().startswith("/help"):
            print("Commands: /tools  /health  /systems [days]  /health [system_id]  /problems  /ids [days]  /components  /resources  /manifest  /call <tool> <json>  /exit")
            continue

        if q.lower().startswith("/tools"):
            print("Tools:", [t.name for t in tools])
            continue

        if q.lower() == "/health":
            await _direct_call(tools, "db2.healthcheck", {})
            continue


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

        if q.lower().startswith("/components"):
            await _direct_call(tools, "db2.installed_components", {})
            continue

        if q.lower().startswith("/resources"):
            if resource_context:
                print("Available MCP Resources:")
                print(resource_context[:1000] + "..." if len(resource_context) > 1000 else resource_context)
            else:
                print("No MCP resources loaded")
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
        
        spinner = ProgressSpinner("Thinking")
        spinner.start()
        
        try:
            result = await agent.ainvoke({"messages": msgs})
            
            # Show which tools were used (for debugging)
            tool_calls = []
            for msg in result["messages"]:
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    for tool_call in msg.tool_calls:
                        tool_calls.append(tool_call['name'])
            
            if tool_calls:
                print(f"\n[DEBUG] Tools used: {', '.join(tool_calls)}")
            
            print("\nAI:", strip_think(result["messages"][-1].content), "\n")
        except Exception as e:
            print(f"\nAgent error: {e}\n")
        finally:
            spinner.stop()

async def main():
    print(f"MCP DB2 Health REPL - model={OLLAMA_MODEL} - base={OLLAMA_BASE_URL}")

    # stdio launch params for DB2 server
    p_db2 = StdioServerParameters(command="uv", args=["run", DB2_PY], cwd=DB2_DIR, env=os.environ.copy())

    # keep DB2 session open during the whole REPL
    async with stdio_client(p_db2) as (r1, w1):
        async with ClientSession(read_stream=r1, write_stream=w1) as s1:
            await s1.initialize()
            print("DB2 MCP session initialized")

            tools = await load_mcp_tools(s1)  # db2 server tools
            
            # Load MCP resources and add to system context
            resource_context = await _load_mcp_resources(s1)

            llm = ChatOllama(
                model=OLLAMA_MODEL,
                base_url=OLLAMA_BASE_URL,
                temperature=0.1,
                timeout=60.0,
            ).bind(stop=["</think>"])

            await repl(tools, llm, resource_context)

if __name__ == "__main__":
    asyncio.run(main())
