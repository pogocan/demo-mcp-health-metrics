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
from mcp.types import TextContent

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


if __name__ == "__main__":
    mcp.run(transport="stdio")
