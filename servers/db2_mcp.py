#!/usr/bin/env python3
"""
IZPCA DB2 Health Metrics MCP Server (minimal+)
- Tools:
    • db2.healthcheck        — connectivity probe (SYSIBM.SYSDUMMY1)
    • db2.schema_manifest    — static JSON with table/column structure (no DB hit)
    • db2.discover_context   — available MVS_SYSTEM_ID/LPAR/PROCESSOR_TYPE (fast)
    • (optional) db2.list_schemas — distinct creators having tables/views
- Uses JayDeBeApi + explicit JVM start for reliability on Windows
- Loads .env from this folder (and parent as fallback)
"""

import os
import sys
import json
import time
import pathlib
import contextlib
from typing import List, Sequence, Tuple, Dict, Any

import jpype
import jaydebeapi
from jpype import JClass
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, Resource

HERE = pathlib.Path(__file__).resolve().parent
load_dotenv(HERE / ".env", override=True)
load_dotenv(HERE.parent / ".env", override=True)

DATA_SCHEMA = os.environ.get("DATA_SCHEMA", "PRL")
METADATA_SCHEMA = os.environ.get("METADATA_SCHEMA", "PRLSYS")

def _log(msg: str) -> None:
    print(f"[db2-mcp] {msg}", file=sys.stderr, flush=True)

# -----------------------------------------------------------------------------
# Classpath + JVM bootstrap
# -----------------------------------------------------------------------------
def _jdbc_classpath() -> str:
    drivers_dir = HERE.parent / "drivers"
    candidates = [
        drivers_dir / "db2jcc4.jar",
        drivers_dir / "db2jcc_license_cu.jar",
        drivers_dir / "db2jcc_license_cisuz.jar",
    ]
    jars = [str(p.resolve()) for p in candidates if p.exists()]
    if not any(pathlib.Path(j).name.startswith("db2jcc4") for j in jars):
        raise RuntimeError("DB2 JDBC driver not found in ./drivers (expected db2jcc4.jar).")
    return os.pathsep.join(jars)

def _jvm_dll_path() -> str:
    dll = os.environ.get("JVM_DLL")
    if not dll:
        java_home = os.environ.get("JAVA_HOME")
        if not java_home:
            raise RuntimeError("JAVA_HOME environment variable not set")
        dll = str(pathlib.Path(java_home) / "bin" / "server" / "jvm.dll")
    if not pathlib.Path(dll).exists():
        raise RuntimeError(f"jvm.dll not found at: {dll}")
    return dll

def _start_jvm_once() -> None:
    if jpype.isJVMStarted():
        return
    cp = _jdbc_classpath()
    jvm = _jvm_dll_path()
    _log(f"Starting JVM: {jvm}")
    _log(f"Classpath: {cp}")
    jpype.startJVM(jvm, f"-Djava.class.path={cp}")
    # Fail fast if login stalls
    try:
        JClass("java.sql.DriverManager").setLoginTimeout(10)  # seconds
    except Exception as e:
        _log(f"Login timeout set failed (non-fatal): {e}")
    _log("JVM started.")

try:
    _log("JVM warmup starting…")
    _start_jvm_once()
    _log("JVM warmup done.")
except Exception as e:
    _log(f"JVM warmup skipped: {e}")

# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------
def _connect():
    drv = os.environ.get("DB2_DRIVER", "com.ibm.db2.jcc.DB2Driver")
    url = os.environ["DB2_JDBC_URL"]
    usr = os.environ["DB2_USER"]
    pwd = os.environ.get("DB2_PASSWORD") or os.environ.get("DB2_PASS")
    if not pwd:
        raise RuntimeError("Missing DB2_PASSWORD (or DB2_PASS) in environment.")
    return jaydebeapi.connect(drv, url, [usr, pwd], jars=None)

@contextlib.contextmanager
def _db2_conn(default_schema: str = DATA_SCHEMA):
    conn = _connect()
    if default_schema:
        conn.jconn.createStatement().execute(f"SET CURRENT SCHEMA {default_schema.upper()}")
    try:
        yield conn
    finally:
        conn.close()

# --- put near other helpers ---
def _py(v):
    """Coerce JDBC/JPype scalars to JSON-safe Python types."""
    if v is None: 
        return None
    try:
        # ints sometimes come back as java.lang.Long
        if isinstance(v, (int, float, bool, str)):
            return v
        # Force anything else to string (java.sql.Date, java.lang.String, etc.)
        return str(v)
    except Exception:
        return str(v)

def _json_text(payload):
    # last-resort default=str so nothing blows up
    return TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))


# -----------------------------------------------------------------------------
# STATIC manifest (no DB hit) — tell the agent what columns exist
# -----------------------------------------------------------------------------
SCHEMA_MANIFEST = {
    "tables": {
        "KPMZ_RULE_VALUES_V": {
            "schema": "PRL",
            "description": "Current rule values and health levels",
            "columns": [
                "RULE_GROUP", "DATE", "RULE_ID", "RULE_METRIC", "RULE_AREA",
                "RULE_VALUE", "RULE_LEVEL", "MVS_SYSTEM_ID", "LPAR_NAME",
                "PROCESSOR_TYPE", "CAPACITY_GRP_NM", "SYSPLEX_NAME"
            ],
            "key_columns": ["MVS_SYSTEM_ID", "RULE_ID", "DATE", "RULE_LEVEL"]
        },
        "KPMZ_RULES": {
            "schema": "PRL",
            "description": "Rule definitions and descriptions",
            "columns": ["START_DATE", "END_DATE", "RULE_ID", "RULE_UOM", "RULE_GROUP", "RULE_DESCRIPTION"],
            "key_columns": ["RULE_ID", "RULE_GROUP", "RULE_DESCRIPTION"]
        },
        "KPMZ_CP_SCWL_HV": {
            "schema": "PRL",
            "description": "LPAR performance metrics (CPU/storage)",
            "columns": [
                "DATE", "TIME", "SYSPLEX_NAME", "MVS_SYSTEM_ID", "PROCESSOR_TYPE",
                "CPU_USED_TOT", "CPU_DISPATCH_SEC", "CSTOR_AVLBL_AVG", "LPAR_NAME"
            ],
            "key_columns": ["MVS_SYSTEM_ID", "LPAR_NAME", "DATE", "TIME"]
        },
        "KPMZ_RULE_LEVELS": {
            "schema": "PRL",
            "description": "Rule threshold definitions",
            "columns": [
                "RULE_ID", "RULE_METRIC", "RULE_LEVEL", "RULE_LEVEL_LOW", "RULE_LEVEL_HIGH", "DESCRIPTION"
            ],
            "key_columns": ["RULE_ID", "RULE_LEVEL"]
        },
        "DRLCOMPONENTS": {
            "schema": "PRLSYS",
            "description": "Component installation status",
            "columns": ["COMPONENT_NAME", "STATUS", "DESCRIPTION"],
            "key_columns": ["COMPONENT_NAME", "STATUS"]
        },
        "DRLCOMP_PARTS": {
            "schema": "PRLSYS",
            "description": "Component parts and health checks",
            "columns": ["COMPONENT_NAME", "PART_NAME", "STATUS", "DESCRIPTION"],
            "key_columns": ["COMPONENT_NAME", "PART_NAME", "STATUS"]
        },
    },
    "hints": {
        "date_column_is_date_type": True,
        "with_ur_safe": True,
        "recent_window_days_default": 30
    }
}

# -----------------------------------------------------------------------------
# MCP server + tools
# -----------------------------------------------------------------------------
mcp = FastMCP("izpca-db2-health-minimal")

@mcp.tool(
    name="db2.management_summary", 
    description="Executive summary for management - translates technical issues into business impact language. Use when user asks for 'summary for boss', 'executive summary', or 'management report'."
)
def management_summary(days: int = 7, system_id: str = None) -> TextContent:
    t0 = time.time()
    try:
        # Get the same data as problem_areas but with business context
        where_clauses = [
            "rv.DATE >= CURRENT DATE - ? DAYS",
            "rv.RULE_LEVEL >= 2"
        ]
        params = [int(days)]
        
        if system_id:
            where_clauses.append("rv.MVS_SYSTEM_ID = ?")
            params.append(system_id.upper())
        
        summary_sql = f"""
            SELECT rv.MVS_SYSTEM_ID,
                   rv.RULE_GROUP,
                   COUNT(CASE WHEN rv.RULE_LEVEL >= 3 THEN 1 END) as CRITICAL_COUNT,
                   COUNT(CASE WHEN rv.RULE_LEVEL = 2 THEN 1 END) as WARNING_COUNT
            FROM {DATA_SCHEMA}.KPMZ_RULE_VALUES_V rv
            WHERE {" AND ".join(where_clauses)}
            GROUP BY rv.MVS_SYSTEM_ID, rv.RULE_GROUP
            ORDER BY CRITICAL_COUNT DESC, WARNING_COUNT DESC
            WITH UR
        """
        
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(summary_sql, params)
            rows = cur.fetchall()
        
        # Business impact mapping
        rule_group_impact = {
            "DB2Z": {
                "name": "Database Performance",
                "business_impact": "Customer transaction slowdowns, potential revenue loss",
                "urgency": "HIGH - affects customer experience directly",
                "typical_resolution": "2-4 hours with DBA intervention"
            },
            "DASD": {
                "name": "Storage Systems", 
                "business_impact": "System slowdowns, potential outages during peak usage",
                "urgency": "HIGH - risk of complete system unavailability",
                "typical_resolution": "4-8 hours, may require hardware intervention"
            },
            "LPAR": {
                "name": "Resource Allocation",
                "business_impact": "Inefficient resource usage, increased operational costs", 
                "urgency": "MEDIUM - optimize during next maintenance window",
                "typical_resolution": "1-2 hours configuration changes"
            },
            "WORK": {
                "name": "Workload Management",
                "business_impact": "Reduced system throughput, longer processing times",
                "urgency": "MEDIUM - monitor and schedule optimization",
                "typical_resolution": "2-4 hours tuning and testing"
            },
            "SYID": {
                "name": "System Configuration",
                "business_impact": "Configuration drift, compliance risks",
                "urgency": "LOW - address in planned maintenance",
                "typical_resolution": "1-2 hours administrative changes"
            }
        }
        
        # Process data for business summary
        business_issues = []
        total_critical = 0
        total_warnings = 0
        systems_affected = set()
        
        for row in rows:
            sys_id = _py(row[0]).strip()
            rule_group = _py(row[1]).strip() 
            critical = int(_py(row[2]) or 0)
            warnings = int(_py(row[3]) or 0)
            
            if critical > 0 or warnings > 0:
                systems_affected.add(sys_id)
                total_critical += critical
                total_warnings += warnings
                
                impact_info = rule_group_impact.get(rule_group, {
                    "name": rule_group,
                    "business_impact": "System performance impact",
                    "urgency": "MEDIUM - requires investigation", 
                    "typical_resolution": "2-6 hours technical analysis"
                })
                
                business_issues.append({
                    "system": sys_id,
                    "area": impact_info["name"],
                    "critical": critical,
                    "warnings": warnings,
                    "impact": impact_info["business_impact"],
                    "urgency": impact_info["urgency"],
                    "resolution_time": impact_info["typical_resolution"]
                })
        
        # Calculate business risk
        risk_level = "LOW"
        
        if total_critical > 100:
            risk_level = "CRITICAL"
        elif total_critical > 50:
            risk_level = "HIGH" 
        elif total_critical > 10:
            risk_level = "MEDIUM"
        
        return _json_text({
            "ok": True,
            "days": int(days),
            "system_filter": system_id.upper() if system_id else "ALL_SYSTEMS",
            "executive_summary": {
                "total_critical_issues": total_critical,
                "total_warning_issues": total_warnings,
                "systems_affected": len(systems_affected),
                "business_risk_level": risk_level,
                "systems_list": list(systems_affected)
            },
            "business_issues": business_issues[:3],  # Top 3 for brevity
            "recommendations": {
                "immediate_action": "Address critical issues on affected systems",
                "timeframe": "Next 4-24 hours",
                "resources_needed": "Technical teams and possible vendor support"
            },
            "ms": int((time.time() - t0) * 1000)
        })
        
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(
    name="db2.installed_components", 
    description="List main components and their installation status. Shows high-level components like DB2, CICS, etc. Use when user asks 'what components are installed'."
)
def installed_components() -> TextContent:
    t0 = time.time()
    try:
        sql = f"""
            SELECT COMPONENT_NAME, STATUS, DESCRIPTION, TIME_INSTALLED, USER_ID
            FROM {METADATA_SCHEMA}.DRLCOMPONENTS
            ORDER BY COMPONENT_NAME
            WITH UR
        """
        
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        
        components = []
        for row in rows:
            components.append({
                "component_name": _py(row[0]).strip() if _py(row[0]) else "",
                "status": _py(row[1]).strip() if _py(row[1]) else "",
                "description": _py(row[2]).strip() if _py(row[2]) else "",
                "time_installed": str(_py(row[3])) if _py(row[3]) else "",
                "user_id": _py(row[4]).strip() if _py(row[4]) else ""
            })
        
        # Categorize by status (I=Installed, empty=Not Installed)
        installed = [c for c in components if c["status"] == "I"]
        not_installed = [c for c in components if c["status"] == ""]
        other_status = [c for c in components if c["status"] not in ["I", ""]]
        
        return _json_text({
            "ok": True,
            "total_components": len(components),
            "installed_count": len(installed),
            "not_installed_count": len(not_installed),
            "other_status_count": len(other_status),
            "installed_components": installed,
            "not_installed_components": not_installed,
            "other_status_components": other_status,
            "ms": int((time.time() - t0) * 1000)
        })
        
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(
    name="db2.find_components",
    description="Search for all components related to a technology (e.g., 'DB2' finds DB2, ADB2, KPM_DB2, CP_DB2, etc.). Use when user asks 'what DB2 components' or 'CICS related components'."
)
def find_components(search_pattern: str) -> TextContent:
    t0 = time.time()
    try:
        sql = f"""
            SELECT COMPONENT_NAME, STATUS, DESCRIPTION, TIME_INSTALLED, USER_ID
            FROM {METADATA_SCHEMA}.DRLCOMPONENTS
            WHERE UPPER(COMPONENT_NAME) LIKE ? OR UPPER(DESCRIPTION) LIKE ?
            ORDER BY COMPONENT_NAME
            WITH UR
        """
        
        pattern = f"%{search_pattern.upper()}%"
        
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, [pattern, pattern])
            rows = cur.fetchall()
        
        components = []
        for row in rows:
            components.append({
                "component_name": _py(row[0]).strip() if _py(row[0]) else "",
                "status": _py(row[1]).strip() if _py(row[1]) else "",
                "description": _py(row[2]).strip() if _py(row[2]) else "",
                "time_installed": str(_py(row[3])) if _py(row[3]) else "",
                "user_id": _py(row[4]).strip() if _py(row[4]) else ""
            })
        
        # Categorize by status and group by aspect
        installed = [c for c in components if c["status"] == "I"]
        not_installed = [c for c in components if c["status"] == ""]
        other_status = [c for c in components if c["status"] not in ["I", ""]]
        
        # Group components by their aspect/purpose
        def categorize_component(comp_name, description):
            name_upper = comp_name.upper()
            desc_upper = description.upper()
            
            if any(x in name_upper for x in ["AKD", "KPM"]) or "KEY PERFORMANCE" in desc_upper:
                return "Performance Monitoring"
            elif any(x in name_upper for x in ["ADB2", "AZPM"]) or "ANALYTICS" in desc_upper:
                return "Analytics"
            elif any(x in name_upper for x in ["CP_"]) or "CAPACITY PLANNING" in desc_upper:
                return "Capacity Planning"
            elif "MON" in name_upper or "MONITORING" in desc_upper:
                return "Monitoring"
            elif name_upper == search_pattern.upper():
                return "Core Component"
            else:
                return "Other/Extension"
        
        # Group installed components by aspect
        installed_by_aspect = {}
        for comp in installed:
            aspect = categorize_component(comp["component_name"], comp["description"])
            if aspect not in installed_by_aspect:
                installed_by_aspect[aspect] = []
            installed_by_aspect[aspect].append(comp)
        
        # Group not installed components by aspect  
        not_installed_by_aspect = {}
        for comp in not_installed:
            aspect = categorize_component(comp["component_name"], comp["description"])
            if aspect not in not_installed_by_aspect:
                not_installed_by_aspect[aspect] = []
            not_installed_by_aspect[aspect].append(comp)
        
        # Add priority analysis for found components
        def get_component_priority(comp_name, search_term):
            # Technology mapping
            tech_map = {
                "DB2": "DB2", "CICS": "CICS", "MVS": "z/OS_SYSTEM", "IMS": "IMS", 
                "NETWORK": "NETWORK", "NW": "NETWORK"
            }
            
            # Determine technology from search pattern or component name
            technology = None
            for key, tech in tech_map.items():
                if key.upper() in search_term.upper() or key.upper() in comp_name.upper():
                    technology = tech
                    break
            
            if not technology:
                return "UNKNOWN"
            
            # Priority mapping (from resources - this is static for now but could be dynamic)
            priorities = {
                "DB2": {
                    "ESSENTIAL": ["DB2"],
                    "IMPORTANT": ["KPMDB2", "AKD"], 
                    "USEFUL": ["CP_DB2", "ADB2"],
                    "OPTIONAL": ["CSWKDPS"]
                },
                "CICS": {
                    "ESSENTIAL": ["CICSMON"],
                    "IMPORTANT": ["CICSUOW", "AKC"],
                    "USEFUL": ["CP_CICS", "OMEG_CICSMON"]
                },
                "z/OS_SYSTEM": {
                    "ESSENTIAL": ["MVS", "KPMZOS"],
                    "IMPORTANT": ["MVSPERF", "DFSMS"],
                    "USEFUL": ["CP", "MVSAC"]
                }
            }
            
            tech_priorities = priorities.get(technology, {})
            for priority, components in tech_priorities.items():
                if comp_name in components:
                    return priority
            return "USEFUL"  # Default priority
        
        # Enhance components with priority information
        for comp in installed + not_installed:
            comp["priority"] = get_component_priority(comp["component_name"], search_pattern)
        
        # Create priority-based recommendations
        recommendations = []
        if not_installed:
            # Find missing essential/important components
            missing_essential = [c for c in not_installed if c["priority"] == "ESSENTIAL"]
            missing_important = [c for c in not_installed if c["priority"] == "IMPORTANT"]
            
            if missing_essential:
                recommendations.append({
                    "type": "CRITICAL_MISSING",
                    "message": f"Missing ESSENTIAL components: {[c['component_name'] for c in missing_essential]}",
                    "action": "Install immediately for basic monitoring"
                })
            
            if missing_important:
                recommendations.append({
                    "type": "RECOMMENDED",
                    "message": f"Missing IMPORTANT KPM components: {[c['component_name'] for c in missing_important]}",
                    "action": "Highly recommended for performance monitoring"
                })
        
        return _json_text({
            "ok": True,
            "search_pattern": search_pattern,
            "total_found": len(components),
            "installed_count": len(installed),
            "not_installed_count": len(not_installed),
            "other_status_count": len(other_status),
            "installed_by_aspect": installed_by_aspect,
            "not_installed_by_aspect": not_installed_by_aspect,
            "other_status_components": other_status,
            "recommendations": recommendations,
            "ms": int((time.time() - t0) * 1000)
        })
        
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(
    name="db2.component_recommendations",
    description="Get intelligent component installation recommendations based on usage patterns and priorities. Use when user asks 'what should I install' or 'component recommendations'."
)
def component_recommendations(focus_area: str = "performance") -> TextContent:
    t0 = time.time()
    try:
        # Get all installed components first
        sql = f"""
            SELECT COMPONENT_NAME, STATUS, DESCRIPTION, TIME_INSTALLED, USER_ID
            FROM {METADATA_SCHEMA}.DRLCOMPONENTS
            ORDER BY COMPONENT_NAME
            WITH UR
        """
        
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        
        components = []
        for row in rows:
            components.append({
                "component_name": _py(row[0]).strip() if _py(row[0]) else "",
                "status": _py(row[1]).strip() if _py(row[1]) else "",
                "description": _py(row[2]).strip() if _py(row[2]) else ""
            })
        
        installed = [c["component_name"] for c in components if c["status"] == "I"]
        
        # Component priority matrix based on focus area
        focus_recommendations = {
            "performance": {
                "essential": ["KPMZOS", "MVS"],  # KPM z/OS + basic system monitoring
                "high_priority": ["KPMDB2", "KPMCIC", "DFSMS", "DB2", "CICSMON"],  # Technology-specific KPM + basic monitoring
                "useful": ["MVSPERF", "CICSUOW", "AKD", "AKC"],  # Advanced performance tools
                "description": "Performance monitoring and management focus"
            },
            "capacity": {
                "essential": ["CP", "MVS"],  # Core capacity planning + basic system
                "high_priority": ["CP_DB2", "CP_CICS", "DFSMS"],  # Technology-specific capacity planning
                "useful": ["CP_IMS", "MVSAC", "KPMZOS"],  # Additional capacity tools + system KPM
                "description": "Capacity planning and resource optimization focus"
            },
            "database": {
                "essential": ["KPMDB2", "DB2"],  # DB2 KPM is critical
                "high_priority": ["KPMZOS", "MVS", "AKD"],  # System KPM + alternatives
                "useful": ["CP_DB2", "ADB2", "DFSMS", "CSWKDPS"],  # Capacity + analytics
                "description": "Database focus - KPMDB2 is essential"
            },
            "transactions": {
                "essential": ["KPMCIC", "CICSMON"],  # CICS KPM + basic monitoring
                "high_priority": ["KPMZOS", "AKC", "CICSUOW"],  # System KPM + transaction analysis
                "useful": ["CP_CICS", "OMEG_CICSMON", "MVS"],  # Capacity + enhanced monitoring
                "description": "Transaction focus - CICS KPM first"
            },
            "basic": {
                "essential": ["KPMZOS"],  # Minimal: just system KPM
                "high_priority": ["MVS", "DFSMS"],  # Basic system monitoring
                "useful": ["KPMDB2", "DB2", "CICSMON"],  # Add technology-specific as needed
                "description": "Minimal monitoring - KPM z/OS only"
            }
        }
        
        recommendations = focus_recommendations.get(focus_area.lower(), focus_recommendations["performance"])
        
        # Analyze what's missing
        missing_essential = [c for c in recommendations["essential"] if c not in installed]
        missing_high_priority = [c for c in recommendations["high_priority"] if c not in installed]
        missing_useful = [c for c in recommendations["useful"] if c not in installed]
        
        # Create installation plan
        installation_plan = []
        if missing_essential:
            installation_plan.append({
                "phase": "Phase 1 - Critical (Install First)",
                "components": missing_essential,
                "reason": "Essential for basic monitoring in your focus area"
            })
        
        if missing_high_priority:
            installation_plan.append({
                "phase": "Phase 2 - High Priority (Install Next)",
                "components": missing_high_priority,
                "reason": "Key performance metrics and advanced monitoring"
            })
        
        if missing_useful:
            installation_plan.append({
                "phase": "Phase 3 - Enhancement (Install Later)",
                "components": missing_useful[:5],  # Limit to top 5
                "reason": "Additional capabilities and specialized analysis"
            })
        
        # Calculate coverage
        total_recommended = len(recommendations["essential"]) + len(recommendations["high_priority"])
        installed_recommended = len([c for c in recommendations["essential"] + recommendations["high_priority"] if c in installed])
        coverage_percentage = (installed_recommended / total_recommended * 100) if total_recommended > 0 else 0
        
        return _json_text({
            "ok": True,
            "focus_area": focus_area,
            "focus_description": recommendations["description"],
            "coverage_percentage": round(coverage_percentage, 1),
            "installed_count": len(installed),
            "total_components": len(components),
            "installation_plan": installation_plan,
            "next_action": installation_plan[0] if installation_plan else {"message": "All recommended components installed!"},
            "ms": int((time.time() - t0) * 1000)
        })
        
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(
    name="db2.kmp_assessment",
    description="Assess KPM (Key Performance Metrics) component coverage and provide KPM-first installation guidance. Use when user asks about monitoring foundation or what to install first."
)
def kmp_assessment() -> TextContent:
    t0 = time.time()
    try:
        # Get installed components
        sql = f"""
            SELECT COMPONENT_NAME, STATUS, DESCRIPTION
            FROM {METADATA_SCHEMA}.DRLCOMPONENTS
            WHERE COMPONENT_NAME LIKE '%KPM%' 
               OR COMPONENT_NAME IN ('KPMZOS', 'KPMDB2', 'KPMCIC', 'AKD', 'AKC')
               OR DESCRIPTION LIKE '%Key Performance%'
            ORDER BY COMPONENT_NAME
            WITH UR
        """
        
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        
        kmp_components = []
        for row in rows:
            kmp_components.append({
                "component_name": _py(row[0]).strip() if _py(row[0]) else "",
                "status": _py(row[1]).strip() if _py(row[1]) else "",
                "description": _py(row[2]).strip() if _py(row[2]) else ""
            })
        
        # Define core KPM components by technology
        core_kmp = {
            "z/OS System": {"component": "KPMZOS", "critical": True, "description": "Foundation - required for all other monitoring"},
            "DB2": {"component": "KPMDB2", "critical": True, "description": "Essential for DB2 performance monitoring"},
            "CICS": {"component": "KPMCIC", "critical": True, "description": "Essential for CICS transaction monitoring"},
            "IMS": {"component": "KPM_IMS", "critical": False, "description": "IMS performance monitoring"},
            "MQ": {"component": "KPMMQ", "critical": False, "description": "MQ performance monitoring"}
        }
        
        # Check installation status
        installed_kmp = [c["component_name"] for c in kmp_components if c["status"] == "I"]
        
        assessment = {}
        critical_missing = []
        
        for tech, info in core_kmp.items():
            comp_name = info["component"]
            is_installed = comp_name in installed_kmp
            assessment[tech] = {
                "component": comp_name,
                "installed": is_installed,
                "critical": info["critical"],
                "description": info["description"]
            }
            
            if info["critical"] and not is_installed:
                critical_missing.append({"tech": tech, "component": comp_name})
        
        # Calculate KPM coverage
        critical_total = sum(1 for info in core_kmp.values() if info["critical"])
        critical_installed = sum(1 for tech, status in assessment.items() if status["installed"] and status["critical"])
        kmp_coverage = (critical_installed / critical_total * 100) if critical_total > 0 else 0
        
        # Generate recommendations
        recommendations = []
        
        if not assessment["z/OS System"]["installed"]:
            recommendations.append({
                "priority": "URGENT",
                "action": "Install KPMZOS immediately",
                "reason": "Foundation component - required for all mainframe monitoring"
            })
        
        for missing in critical_missing:
            if missing["component"] != "KPMZOS":  # Already handled above
                recommendations.append({
                    "priority": "HIGH",
                    "action": f"Install {missing['component']} for {missing['tech']}",
                    "reason": f"Essential for {missing['tech']} performance monitoring"
                })
        
        if kmp_coverage >= 100:
            recommendations.append({
                "priority": "INFO", 
                "action": "KPM foundation complete - excellent performance monitoring coverage",
                "reason": "All critical KPM components installed for performance management"
            })
        
        return _json_text({
            "ok": True,
            "kmp_coverage_percentage": round(kmp_coverage, 1),
            "critical_missing_count": len(critical_missing),
            "assessment_by_technology": assessment,
            "installed_kmp_components": installed_kmp,
            "recommendations": recommendations,
            "next_steps": "Install missing KPM components for complete performance monitoring coverage" if critical_missing else "KPM foundation complete - performance monitoring ready",
            "ms": int((time.time() - t0) * 1000)
        })
        
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(
    name="db2.component_parts",
    description="Get sub-components/parts within a main component (e.g., DB2 has parts like 'DB2 Buffer Pool', 'DB2 Address Space'). Use when user asks about parts of a specific component."
)
def component_parts(component_name: str = None) -> TextContent:
    t0 = time.time()
    try:
        where_clause = ""
        params = []
        
        if component_name:
            where_clause = "WHERE COMPONENT_NAME = ?"
            params.append(component_name.upper())
        
        sql = f"""
            SELECT COMPONENT_NAME, PART_NAME, STATUS, DESCRIPTION, TIME_INSTALLED, USER_ID
            FROM {METADATA_SCHEMA}.DRLCOMP_PARTS
            {where_clause}
            ORDER BY COMPONENT_NAME, PART_NAME
            WITH UR
        """
        
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        
        parts = []
        for row in rows:
            parts.append({
                "component_name": _py(row[0]).strip() if _py(row[0]) else "",
                "part_name": _py(row[1]).strip() if _py(row[1]) else "",
                "status": _py(row[2]).strip() if _py(row[2]) else "",
                "description": _py(row[3]).strip() if _py(row[3]) else "",
                "time_installed": str(_py(row[4])) if _py(row[4]) else "",
                "user_id": _py(row[5]).strip() if _py(row[5]) else ""
            })
        
        # Group by component
        components_parts = {}
        for part in parts:
            comp_name = part["component_name"]
            if comp_name not in components_parts:
                components_parts[comp_name] = []
            components_parts[comp_name].append({
                "part_name": part["part_name"],
                "status": part["status"],
                "description": part["description"],
                "time_installed": part["time_installed"],
                "user_id": part["user_id"]
            })
        
        return _json_text({
            "ok": True,
            "component_filter": component_name.upper() if component_name else "ALL_COMPONENTS",
            "total_parts": len(parts),
            "components_count": len(components_parts),
            "components_parts": components_parts,
            "ms": int((time.time() - t0) * 1000)
        })
        
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(
    name="db2.component_objects",
    description="Get detailed objects (tables, views, procedures) within a component part from DRLCOMP_OBJECTS table. Use when user needs specific database objects details."
)
def component_objects(component_name: str = None, part_name: str = None) -> TextContent:
    t0 = time.time()
    try:
        where_clauses = []
        params = []
        
        if component_name:
            where_clauses.append("COMPONENT_NAME = ?")
            params.append(component_name.upper())
        
        if part_name:
            where_clauses.append("PART_NAME = ?")
            params.append(part_name.upper())
        
        where_clause = ""
        if where_clauses:
            where_clause = "WHERE " + " AND ".join(where_clauses)
        
        sql = f"""
            SELECT COMPONENT_NAME, OBJECT_NAME, OBJECT_TYPE, MEMBER_NAME, PART_NAME, EXCLUDE_FLAG
            FROM {METADATA_SCHEMA}.DRLCOMP_OBJECTS
            {where_clause}
            ORDER BY COMPONENT_NAME, PART_NAME, OBJECT_TYPE, OBJECT_NAME
            FETCH FIRST 100 ROWS ONLY
            WITH UR
        """
        
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        
        objects = []
        for row in rows:
            objects.append({
                "component_name": _py(row[0]).strip() if _py(row[0]) else "",
                "object_name": _py(row[1]).strip() if _py(row[1]) else "",
                "object_type": _py(row[2]).strip() if _py(row[2]) else "",
                "member_name": _py(row[3]).strip() if _py(row[3]) else "",
                "part_name": _py(row[4]).strip() if _py(row[4]) else "",
                "exclude_flag": _py(row[5]).strip() if _py(row[5]) else ""
            })
        
        # Group by component and part
        hierarchy = {}
        for obj in objects:
            comp_name = obj["component_name"]
            part_name = obj["part_name"] or "NO_PART"
            
            if comp_name not in hierarchy:
                hierarchy[comp_name] = {}
            if part_name not in hierarchy[comp_name]:
                hierarchy[comp_name][part_name] = []
            
            hierarchy[comp_name][part_name].append({
                "object_name": obj["object_name"],
                "object_type": obj["object_type"],
                "member_name": obj["member_name"],
                "exclude_flag": obj["exclude_flag"]
            })
        
        return _json_text({
            "ok": True,
            "component_filter": component_name.upper() if component_name else "ALL_COMPONENTS",
            "part_filter": part_name.upper() if part_name else "ALL_PARTS",
            "total_objects": len(objects),
            "component_hierarchy": hierarchy,
            "ms": int((time.time() - t0) * 1000)
        })
        
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(name="db2.explain_health_levels", description="Explain what health levels 0-4 mean in IZPCA monitoring")
def explain_health_levels() -> TextContent:
    explanation = {
        "ok": True,
        "health_levels": {
            "0": {"name": "Not Applicable", "description": "Rule does not apply to this system/component"},
            "1": {"name": "Good", "description": "Healthy - no issues detected"},
            "2": {"name": "Warning", "description": "Needs monitoring - potential issue"},
            "3": {"name": "Critical", "description": "Requires immediate attention"},
            "4": {"name": "Severe Critical", "description": "Urgent - system may be at risk"}
        },
        "summary": "Levels 0-1 are normal, level 2 needs monitoring, levels 3+ require action",
        "usage": "Use db2.problem_areas to see only levels 2+ that need attention"
    }
    return _json_text(explanation)

@mcp.tool(name="db2.healthcheck", description="Connectivity probe via SYSIBM.SYSDUMMY1")
def healthcheck() -> TextContent:
    t0 = time.time()
    try:
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT CURRENT SERVER AS SERVER, CURRENT DATE AS CD "
                "FROM SYSIBM.SYSDUMMY1 WITH UR"
            )
            row = cur.fetchall()[0]
        payload = {
            "ok": True,
            "server": str(row[0]).strip(),
            "date": str(row[1]),
            "ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        payload = {"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)}
    return _json_text(payload)

@mcp.tool(name="db2.schema_manifest", description="Static table/column manifest as JSON (no DB access).")
def schema_manifest() -> TextContent:
    # allow overriding schemas via env without editing the dict
    manifest = dict(SCHEMA_MANIFEST)
    for tbl, meta in manifest["tables"].items():
        if tbl in ("DRLCOMPONENTS", "DRLCOMP_PARTS"):
            meta["schema"] = os.environ.get("METADATA_SCHEMA", meta["schema"])
        else:
            meta["schema"] = os.environ.get("DATA_SCHEMA", meta["schema"])
    return _json_text({"ok": True, "manifest": manifest})



@mcp.tool(
    name="db2.discover_context",
    description="List distinct systems/LPARs/processor types (recent window) to help pick params."
)
def discover_context(days: int = 30, max_rows: int = 200) -> TextContent:
    t0 = time.time()
    try:
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(f"""
                SELECT MVS_SYSTEM_ID, LPAR_NAME, PROCESSOR_TYPE, COUNT(*) AS CNT
                FROM {DATA_SCHEMA}.KPMZ_RULE_VALUES_V
                WHERE DATE >= CURRENT DATE - {int(days)} DAYS
                GROUP BY MVS_SYSTEM_ID, LPAR_NAME, PROCESSOR_TYPE
                ORDER BY MVS_SYSTEM_ID, LPAR_NAME, PROCESSOR_TYPE
                FETCH FIRST {int(max_rows)} ROWS ONLY
                WITH UR
            """)
            rows = cur.fetchall()

        data = [{
            "MVS_SYSTEM_ID": _py(r[0]).strip() if _py(r[0]) else "",
            "LPAR_NAME":     _py(r[1]).strip() if _py(r[1]) else "",
            "PROCESSOR_TYPE":_py(r[2]).strip() if _py(r[2]) else "",
            "count":         int(_py(r[3]) or 0),
        } for r in rows]

        return _json_text({
            "ok": True,
            "days": int(days),
            "rows": data,
            "ms": int((time.time() - t0) * 1000)
        })
    except Exception as e:
        return _json_text({"ok": False, "error": str(e)})



@mcp.tool(
    name="db2.show_systems",
    description="List all available mainframe systems with recent activity. Call this first if user asks about systems without specifying names."
)
def show_systems(days: int = 7) -> TextContent:
    t0 = time.time()
    try:
        sql = f"""
            SELECT MVS_SYSTEM_ID, 
                   COUNT(*) as TOTAL_RECORDS,
                   COUNT(CASE WHEN RULE_LEVEL >= 3 THEN 1 END) as CRITICAL_ISSUES,
                   COUNT(CASE WHEN RULE_LEVEL = 2 THEN 1 END) as WARNINGS
            FROM {DATA_SCHEMA}.KPMZ_RULE_VALUES_V
            WHERE DATE >= CURRENT DATE - {int(days)} DAYS
            GROUP BY MVS_SYSTEM_ID
            ORDER BY CRITICAL_ISSUES DESC, MVS_SYSTEM_ID
            WITH UR
        """
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        
        systems = [{
            "system_id": _py(r[0]).strip(),
            "total_records": int(_py(r[1]) or 0),
            "critical_issues": int(_py(r[2]) or 0),
            "warnings": int(_py(r[3]) or 0)
        } for r in rows]
        
        return _json_text({
            "ok": True,
            "days": int(days),
            "systems": systems,
            "total_systems": len(systems),
            "ms": int((time.time() - t0) * 1000)
        })
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(
    name="db2.system_health",
    description="Get detailed health status for a specific system by rule group. Use when user asks about a particular system's health."
)
def system_health(system_id: str, days: int = 7) -> TextContent:
    t0 = time.time()
    try:
        sql = f"""
            SELECT rv.RULE_GROUP,
                   COUNT(*) as TOTAL,
                   COUNT(CASE WHEN rv.RULE_LEVEL >= 3 THEN 1 END) as CRITICAL,
                   COUNT(CASE WHEN rv.RULE_LEVEL = 2 THEN 1 END) as WARNING,
                   COUNT(CASE WHEN rv.RULE_LEVEL = 1 THEN 1 END) as GOOD,
                   COUNT(CASE WHEN rv.RULE_LEVEL = 0 THEN 1 END) as NOT_APPLICABLE,
                   DECIMAL(AVG(CASE WHEN rv.RULE_LEVEL > 0 THEN FLOAT(rv.RULE_LEVEL) END), 5, 2) as AVG_SEVERITY
            FROM {DATA_SCHEMA}.KPMZ_RULE_VALUES_V rv
            WHERE rv.MVS_SYSTEM_ID = ? AND rv.DATE >= CURRENT DATE - {int(days)} DAYS
            GROUP BY rv.RULE_GROUP
            ORDER BY CRITICAL DESC, WARNING DESC, rv.RULE_GROUP
            WITH UR
        """
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, [system_id.upper()])
            rows = cur.fetchall()
        
        rule_groups = [{
            "rule_group": _py(r[0]).strip(),
            "total": int(_py(r[1]) or 0),
            "critical": int(_py(r[2]) or 0),
            "warning": int(_py(r[3]) or 0),
            "good": int(_py(r[4]) or 0),
            "not_applicable": int(_py(r[5]) or 0),
            "avg_severity": float(_py(r[6]) or 0) if _py(r[6]) else 0
        } for r in rows]
        
        totals = {
            "total": sum(g["total"] for g in rule_groups),
            "critical": sum(g["critical"] for g in rule_groups),
            "warning": sum(g["warning"] for g in rule_groups),
            "good": sum(g["good"] for g in rule_groups)
        }
        
        return _json_text({
            "ok": True,
            "system_id": system_id.upper(),
            "days": int(days),
            "summary": totals,
            "rule_groups": rule_groups,
            "ms": int((time.time() - t0) * 1000)
        })
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(
    name="db2.all_systems_health",
    description="Get health overview across all systems and rule groups. Use for estate-wide health dashboard or when user asks about overall health."
)
def all_systems_health(days: int = 7, max_rows: int = 100) -> TextContent:
    t0 = time.time()
    try:
        sql = f"""
            SELECT rv.MVS_SYSTEM_ID,
                   rv.RULE_GROUP,
                   COUNT(*) as TOTAL,
                   COUNT(CASE WHEN rv.RULE_LEVEL >= 3 THEN 1 END) as CRITICAL,
                   COUNT(CASE WHEN rv.RULE_LEVEL = 2 THEN 1 END) as WARNING,
                   COUNT(CASE WHEN rv.RULE_LEVEL = 1 THEN 1 END) as GOOD,
                   DECIMAL(AVG(CASE WHEN rv.RULE_LEVEL > 0 THEN FLOAT(rv.RULE_LEVEL) END), 5, 2) as AVG_SEVERITY
            FROM {DATA_SCHEMA}.KPMZ_RULE_VALUES_V rv
            WHERE rv.DATE >= CURRENT DATE - {int(days)} DAYS
            GROUP BY rv.MVS_SYSTEM_ID, rv.RULE_GROUP
            ORDER BY CRITICAL DESC, WARNING DESC, rv.MVS_SYSTEM_ID, rv.RULE_GROUP
            FETCH FIRST {int(max_rows)} ROWS ONLY
            WITH UR
        """
        with _db2_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        
        health_data = [{
            "system_id": _py(r[0]).strip(),
            "rule_group": _py(r[1]).strip(),
            "total": int(_py(r[2]) or 0),
            "critical": int(_py(r[3]) or 0),
            "warning": int(_py(r[4]) or 0),
            "good": int(_py(r[5]) or 0),
            "avg_severity": float(_py(r[6]) or 0) if _py(r[6]) else 0
        } for r in rows]
        
        systems_summary = {}
        for item in health_data:
            sys_id = item["system_id"]
            if sys_id not in systems_summary:
                systems_summary[sys_id] = {"critical": 0, "warning": 0, "total": 0}
            systems_summary[sys_id]["critical"] += item["critical"]
            systems_summary[sys_id]["warning"] += item["warning"]
            systems_summary[sys_id]["total"] += item["total"]
        
        return _json_text({
            "ok": True,
            "days": int(days),
            "systems_summary": systems_summary,
            "detailed_data": health_data,
            "total_entries": len(health_data),
            "ms": int((time.time() - t0) * 1000)
        })
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

@mcp.tool(
    name="db2.problem_areas",
    description="Show executive summary of critical and warning issues that need attention. Use when user asks 'what needs attention' or 'what are the problems'."
)
def problem_areas(days: int = 7, max_rows: int = 10) -> TextContent:
    t0 = time.time()
    try:
        # Get summary by system and rule group first
        summary_sql = f"""
            SELECT rv.MVS_SYSTEM_ID,
                   rv.RULE_GROUP,
                   COUNT(CASE WHEN rv.RULE_LEVEL >= 3 THEN 1 END) as CRITICAL_COUNT,
                   COUNT(CASE WHEN rv.RULE_LEVEL = 2 THEN 1 END) as WARNING_COUNT
            FROM {DATA_SCHEMA}.KPMZ_RULE_VALUES_V rv
            WHERE rv.DATE >= CURRENT DATE - {int(days)} DAYS
              AND rv.RULE_LEVEL >= 2
            GROUP BY rv.MVS_SYSTEM_ID, rv.RULE_GROUP
            HAVING COUNT(*) > 0
            ORDER BY CRITICAL_COUNT DESC, WARNING_COUNT DESC
            WITH UR
        """
        
        # Get top specific issues
        detail_sql = f"""
            SELECT rv.MVS_SYSTEM_ID,
                   rv.RULE_GROUP,
                   rv.RULE_ID,
                   r.RULE_DESCRIPTION,
                   rv.RULE_LEVEL,
                   rv.DATE
            FROM {DATA_SCHEMA}.KPMZ_RULE_VALUES_V rv
            JOIN {DATA_SCHEMA}.KPMZ_RULES r ON rv.RULE_ID = r.RULE_ID
            WHERE rv.DATE >= CURRENT DATE - {int(days)} DAYS
              AND rv.RULE_LEVEL >= 3
            ORDER BY rv.RULE_LEVEL DESC, rv.DATE DESC
            FETCH FIRST {int(max_rows)} ROWS ONLY
            WITH UR
        """
        
        with _db2_conn() as conn, conn.cursor() as cur:
            # Get summary by system/rule group
            cur.execute(summary_sql)
            summary_rows = cur.fetchall()
            
            # Get top critical issues
            cur.execute(detail_sql)
            detail_rows = cur.fetchall()
        
        # Process summary
        system_summary = [{
            "system_id": _py(r[0]).strip(),
            "rule_group": _py(r[1]).strip(),
            "critical": int(_py(r[2]) or 0),
            "warnings": int(_py(r[3]) or 0)
        } for r in summary_rows]
        
        # Process top critical issues
        top_issues = [{
            "system_id": _py(r[0]).strip(),
            "rule_group": _py(r[1]).strip(),
            "rule_id": _py(r[2]).strip(),
            "description": _py(r[3]).strip() if _py(r[3]) else "",
            "level": int(_py(r[4]) or 0),
            "date": str(_py(r[5])) if _py(r[5]) else ""
        } for r in detail_rows]
        
        # Calculate totals
        total_critical = sum(s["critical"] for s in system_summary)
        total_warnings = sum(s["warnings"] for s in system_summary)
        
        return _json_text({
            "ok": True,
            "days": int(days),
            "executive_summary": {
                "total_critical": total_critical,
                "total_warnings": total_warnings,
                "systems_affected": len(set(s["system_id"] for s in system_summary)),
                "priority_systems": [s for s in system_summary if s["critical"] > 0][:3]
            },
            "system_breakdown": system_summary[:10],
            "top_critical_issues": top_issues,
            "ms": int((time.time() - t0) * 1000)
        })
    except Exception as e:
        return _json_text({"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})


# -----------------------------------------------------------------------------
# MCP Resources - Structured data the agent can reference
# -----------------------------------------------------------------------------
@mcp.resource("db2://health-levels")
async def health_levels_resource() -> str:
    """Health level reference guide for IZPCA monitoring"""
    return json.dumps({
        "health_levels": {
            "0": {"name": "Not Applicable", "description": "Rule does not apply to this system/component", "action": "No action needed"},
            "1": {"name": "Good", "description": "Healthy - no issues detected", "action": "Continue monitoring"},
            "2": {"name": "Warning", "description": "Needs monitoring - potential issue", "action": "Investigate and monitor closely"},
            "3": {"name": "Critical", "description": "Requires immediate attention", "action": "Take corrective action within 24 hours"},
            "4": {"name": "Severe Critical", "description": "Urgent - system may be at risk", "action": "Take immediate action - escalate if needed"}
        },
        "rule_groups": {
            "DB2Z": "Database Performance - affects customer transactions",
            "DASD": "Storage Systems - affects system availability", 
            "LPAR": "Resource Allocation - affects operational efficiency",
            "WORK": "Workload Management - affects system throughput",
            "SYID": "System Configuration - affects compliance",
            "MMRY": "Memory Management - affects performance",
            "PATH": "Network Paths - affects connectivity",
            "PLEX": "Sysplex Configuration - affects availability"
        }
    })

@mcp.resource("db2://component-hierarchy")
async def component_hierarchy_resource() -> str:
    """Component structure reference for understanding the 3-level hierarchy"""
    return json.dumps({
        "hierarchy_levels": {
            "1": {
                "name": "Main Components",
                "description": "High-level technology components (DB2, CICS, etc.)",
                "table": "DRLCOMPONENTS",
                "examples": ["DB2", "CICS", "CP_DB2", "KPMDB2"]
            },
            "2": {
                "name": "Component Parts", 
                "description": "Sub-components within main components",
                "table": "DRLCOMP_PARTS",
                "examples": ["DB2 Buffer Pool", "DB2 Address Space", "CICS Monitoring"]
            },
            "3": {
                "name": "Component Objects",
                "description": "Database objects (tables, views, procedures)",
                "table": "DRLCOMP_OBJECTS", 
                "examples": ["A_DB2_BP_I", "LOOKUP", "TABLE", "UPDATE"]
            }
        },
        "component_aspects": {
            "Core": "Main technology component",
            "Analytics": "ADB2, AZPM - data analysis and reporting", 
            "Performance_Monitoring": "KPM*, AKD - key performance metrics",
            "Capacity_Planning": "CP_* - capacity and resource planning",
            "Monitoring": "*MON - general monitoring and alerting"
        }
    })

@mcp.resource("db2://schema-summary") 
async def schema_summary_resource() -> str:
    """Complete database schema reference - all tables and columns"""
    return json.dumps({
        "schemas": {
            "PRL": "Main data schema - health rules and metrics",
            "PRLSYS": "Metadata schema - component definitions"
        },
        "tables": {
            "KPMZ_RULE_VALUES_V": {
                "schema": "PRL",
                "description": "Current rule values and health levels",
                "columns": [
                    "RULE_GROUP", "DATE", "RULE_ID", "RULE_METRIC", "RULE_AREA",
                    "RULE_VALUE", "RULE_LEVEL", "MVS_SYSTEM_ID", "LPAR_NAME",
                    "PROCESSOR_TYPE", "CAPACITY_GRP_NM", "SYSPLEX_NAME"
                ],
                "key_columns": ["MVS_SYSTEM_ID", "RULE_ID", "DATE", "RULE_LEVEL"],
                "purpose": "Primary source for system health metrics - shows current rule violations",
                "usage": "Filter by RULE_LEVEL >= 2 for problems, join with KPMZ_RULES for descriptions"
            },
            "KPMZ_RULES": {
                "schema": "PRL",
                "description": "Rule definitions and descriptions",
                "columns": ["START_DATE", "END_DATE", "RULE_ID", "RULE_UOM", "RULE_GROUP", "RULE_DESCRIPTION"],
                "key_columns": ["RULE_ID", "RULE_GROUP", "RULE_DESCRIPTION"],
                "purpose": "Explains what each rule monitors - join with RULE_VALUES_V",
                "usage": "Use to translate RULE_ID to human-readable descriptions"
            },
            "KPMZ_CP_SCWL_HV": {
                "schema": "PRL", 
                "description": "LPAR performance metrics (CPU/storage)",
                "columns": [
                    "DATE", "TIME", "SYSPLEX_NAME", "MVS_SYSTEM_ID", "PROCESSOR_TYPE",
                    "CPU_USED_TOT", "CPU_DISPATCH_SEC", "CSTOR_AVLBL_AVG", "LPAR_NAME"
                ],
                "key_columns": ["MVS_SYSTEM_ID", "LPAR_NAME", "DATE", "TIME"],
                "purpose": "Raw performance data - CPU usage and storage metrics",
                "usage": "Use for performance analysis and capacity planning"
            },
            "KPMZ_RULE_LEVELS": {
                "schema": "PRL",
                "description": "Rule threshold definitions",
                "columns": [
                    "RULE_ID", "RULE_METRIC", "RULE_LEVEL", "RULE_LEVEL_LOW", "RULE_LEVEL_HIGH", "DESCRIPTION"
                ],
                "key_columns": ["RULE_ID", "RULE_LEVEL"],
                "purpose": "Defines thresholds for when rules trigger at different levels",
                "usage": "Shows what values cause level 1, 2, 3, 4 health alerts"
            },
            "DRLCOMPONENTS": {
                "schema": "PRLSYS",
                "description": "Component installation status",
                "columns": ["COMPONENT_NAME", "STATUS", "DESCRIPTION", "TIME_INSTALLED", "USER_ID"],
                "key_columns": ["COMPONENT_NAME", "STATUS"],
                "purpose": "Lists all available components and their installation status",
                "usage": "STATUS='I' = Installed, empty = Not Installed"
            },
            "DRLCOMP_PARTS": {
                "schema": "PRLSYS",
                "description": "Component parts and health checks",
                "columns": ["COMPONENT_NAME", "PART_NAME", "STATUS", "DESCRIPTION", "TIME_INSTALLED", "USER_ID"],
                "key_columns": ["COMPONENT_NAME", "PART_NAME", "STATUS"],
                "purpose": "Sub-components within main components (e.g., DB2 Buffer Pool, Address Space)",
                "usage": "Shows detailed parts of each installed component"
            },
            "DRLCOMP_OBJECTS": {
                "schema": "PRLSYS", 
                "description": "Component objects (tables, views, procedures)",
                "columns": ["COMPONENT_NAME", "OBJECT_NAME", "OBJECT_TYPE", "MEMBER_NAME", "PART_NAME", "EXCLUDE_FLAG"],
                "key_columns": ["COMPONENT_NAME", "PART_NAME", "OBJECT_TYPE", "OBJECT_NAME"],
                "purpose": "Database objects within component parts - tables, views, lookup tables, etc.",
                "usage": "Shows actual database objects that each component part manages"
            }
        },
        "common_queries": {
            "health_overview": "SELECT MVS_SYSTEM_ID, COUNT(*) as issues FROM KPMZ_RULE_VALUES_V WHERE RULE_LEVEL >= 2 GROUP BY MVS_SYSTEM_ID",
            "rule_descriptions": "SELECT r.RULE_ID, r.RULE_DESCRIPTION, rv.RULE_LEVEL FROM KPMZ_RULES r JOIN KPMZ_RULE_VALUES_V rv ON r.RULE_ID = rv.RULE_ID",
            "installed_components": "SELECT COMPONENT_NAME, DESCRIPTION, TIME_INSTALLED FROM DRLCOMPONENTS WHERE STATUS = 'I'",
            "system_performance": "SELECT MVS_SYSTEM_ID, AVG(CPU_USED_TOT), AVG(CSTOR_AVLBL_AVG) FROM KPMZ_CP_SCWL_HV GROUP BY MVS_SYSTEM_ID"
        }
    })

@mcp.resource("db2://component-priorities")
async def component_priorities_resource() -> str:
    """Component importance and installation priorities by technology area"""
    return json.dumps({
        "priority_levels": {
            "ESSENTIAL": "Core monitoring - install first",
            "IMPORTANT": "Key performance metrics - highly recommended", 
            "USEFUL": "Additional analysis capabilities",
            "OPTIONAL": "Specialized use cases"
        },
        "technology_priorities": {
            "DB2": {
                "ESSENTIAL": [
                    {"component": "DB2", "purpose": "Core DB2 monitoring", "why_essential": "Basic DB2 health and performance tracking"}
                ],
                "IMPORTANT": [
                    {"component": "KPMDB2", "purpose": "Key Performance Metrics for DB2", "why_important": "Critical performance indicators, bottleneck identification"},
                    {"component": "AKD", "purpose": "DB2 Analytics (Alternative KPM)", "why_important": "Performance monitoring if KPMDB2 not available"}
                ],
                "USEFUL": [
                    {"component": "CP_DB2", "purpose": "Capacity Planning for DB2", "why_useful": "Future growth planning, resource optimization"},
                    {"component": "ADB2", "purpose": "DB2 Analytics", "why_useful": "Advanced reporting and historical analysis"}
                ],
                "OPTIONAL": [
                    {"component": "CSWKDPS", "purpose": "DB2 Workload Statistics", "why_optional": "Detailed workload analysis"}
                ]
            },
            "CICS": {
                "ESSENTIAL": [
                    {"component": "CICSMON", "purpose": "Core CICS Monitoring", "why_essential": "Basic CICS transaction monitoring"}
                ],
                "IMPORTANT": [
                    {"component": "CICSUOW", "purpose": "CICS Transaction Analysis", "why_important": "Transaction performance and unit-of-work tracking"},
                    {"component": "AKC", "purpose": "CICS Key Performance Metrics", "why_important": "Critical CICS performance indicators"}
                ],
                "USEFUL": [
                    {"component": "CP_CICS", "purpose": "CICS Capacity Planning", "why_useful": "Transaction volume planning"},
                    {"component": "OMEG_CICSMON", "purpose": "OMEGAMON CICS Monitoring", "why_useful": "Enhanced CICS monitoring"}
                ]
            },
            "z/OS_SYSTEM": {
                "ESSENTIAL": [
                    {"component": "MVS", "purpose": "z/OS System Monitoring", "why_essential": "Core system health and performance"},
                    {"component": "KPMZOS", "purpose": "z/OS Key Performance Metrics", "why_essential": "Critical system performance indicators"}
                ],
                "IMPORTANT": [
                    {"component": "MVSPERF", "purpose": "z/OS Performance Management", "why_important": "System performance analysis and tuning"},
                    {"component": "DFSMS", "purpose": "Storage Management", "why_important": "Storage allocation and performance"}
                ],
                "USEFUL": [
                    {"component": "CP", "purpose": "z/OS Capacity Planning", "why_useful": "System capacity and growth planning"},
                    {"component": "MVSAC", "purpose": "z/OS Job/Step Accounting", "why_useful": "Resource usage tracking"}
                ]
            },
            "IMS": {
                "IMPORTANT": [
                    {"component": "CSQVE10C", "purpose": "IMS 14.1 Data Collection", "why_important": "Current IMS version monitoring"},
                    {"component": "KPM_IMS", "purpose": "IMS Key Performance Metrics", "why_important": "IMS transaction performance"}
                ],
                "USEFUL": [
                    {"component": "CP_IMS", "purpose": "IMS Capacity Planning", "why_useful": "IMS growth planning"}
                ]
            },
            "NETWORK": {
                "IMPORTANT": [
                    {"component": "NWAVAIL", "purpose": "Network Availability", "why_important": "Network uptime monitoring"},
                    {"component": "NWSF", "purpose": "Network Session Failure", "why_important": "Connection failure tracking"}
                ],
                "USEFUL": [
                    {"component": "TCPIP", "purpose": "TCP/IP Monitoring", "why_useful": "Network protocol analysis"}
                ]
            }
        },
        "installation_sequence": {
            "new_environment": [
                "1. For PERFORMANCE focus: Start with KPM components (KPMZOS foundation, then KPMDB2, KPMCIC)",
                "2. For CAPACITY focus: Start with CP components (CP core, then CP_DB2, CP_CICS)",
                "3. Install basic monitoring for your technologies (DB2, CICSMON, MVS)",
                "4. Add specialized analytics and advanced monitoring as needed",
                "5. Areas are independent - choose based on your primary need"
            ],
            "existing_environment": [
                "1. Identify your primary focus: Performance management OR Capacity planning",
                "2. For performance issues: prioritize KPM components",
                "3. For capacity concerns: prioritize CP components", 
                "4. Both areas provide value independently"
            ]
        },
        "focus_area_guidance": {
            "performance_management": "Use KPM components (KPMZOS, KPMDB2, KPMCIC) for real-time performance monitoring and problem diagnosis",
            "capacity_planning": "Use CP components (CP, CP_DB2, CP_CICS) for resource planning and growth forecasting",
            "note": "These are separate functional areas - install based on your specific business needs"
        },
        "technology_dependencies": {
            "DB2": ["MVS (z/OS system monitoring required)", "DFSMS (storage management)"],
            "CICS": ["MVS (z/OS system monitoring required)"],
            "IMS": ["MVS (z/OS system monitoring required)", "DFSMS (storage management)"]
        },
        "component_usage_guidance": {
            "performance_issues": ["Start with KPM components (KPMDB2, KPMZOS)", "Add specific monitoring (CICSMON for CICS issues)"],
            "capacity_planning": ["Use CP_* components", "Requires historical data from KPM components"],
            "problem_diagnosis": ["Essential + Important components needed", "Analytics components helpful for trends"],
            "executive_reporting": ["KPM components provide business metrics", "Management_summary tool translates technical data"]
        }
    })

if __name__ == "__main__":
    mcp.run(transport="stdio")
