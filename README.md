# MCP DB2 Health Monitoring Demo

A Model Context Protocol (MCP) server for mainframe DB2 health monitoring with AI-powered analysis.

## Features

- **MCP Server** (`servers/db2_mcp.py`): FastMCP-based server with DB2 health monitoring tools and resources
- **Python Client** (`mcp_client.py`): Interactive REPL with LangChain MCP adapters and Ollama integration
- **Health Monitoring**: System health analysis, problem detection, and component management
- **Executive Reporting**: Management-friendly summaries with business impact analysis
- **MCP Resources**: Static reference data for health levels, component hierarchy, and schema information

## Quick Start

### Prerequisites

- Python 3.8+ with `uv` package manager
- Java 17+ (for DB2 JDBC drivers)
- Ollama running locally
- DB2 database access

### Setup

1. **Clone and install dependencies:**
   ```bash
   git clone <repository>
   cd demo-mcp-health-metrics
   uv sync
   ```

2. **Configure environment:**
   Copy `.env.example` to `.env` and update with your settings:
   ```bash
   cp .env.example .env
   ```

3. **Add DB2 JDBC drivers:**
   Place `db2jcc4.jar` and `db2jcc_license_cu.jar` in the `drivers/` folder.

4. **Run the client:**
   ```bash
   uv run mcp_client.py
   ```

## Usage

### Interactive Commands

- `/systems [days]` - List mainframe systems with health status
- `/health [system_id]` - System health analysis
- `/problems` - Critical issues requiring attention
- `/components` - Component installation status
- `/resources` - View available MCP resources
- `/manifest` - Database schema summary

### Natural Language Queries

Ask questions like:
- "What systems need attention?"
- "How is ZT01 performing?"
- "Show me critical issues"
- "What components are installed?"
- "Give me an executive summary"

## Architecture

### MCP Server (`servers/db2_mcp.py`)
- **Tools**: Dynamic database queries (system health, problem analysis, component status)
- **Resources**: Static reference data (health levels, component hierarchy, schema info)
- **FastMCP Framework**: Type-safe MCP implementation with automatic tool generation

### Client (`mcp_client.py`)
- **LangChain Integration**: MCP tools and resources as LangChain components
- **Ollama LLM**: Local language model for AI analysis
- **Formatted Output**: Executive summaries and management reporting

### Key Components

- **Health Monitoring**: Rule-based system health analysis with 5-level severity scale
- **Component Management**: Aspect-based grouping (Core, Analytics, Performance, Capacity, Monitoring)
- **Executive Reporting**: Business-friendly summaries with technical translation
- **Problem Detection**: Automated critical issue identification and prioritization

## MCP Resources

The server provides static reference data via MCP resources:

- `db2://health-levels` - Health level definitions (0-4 scale)
- `db2://component-hierarchy` - Component structure and relationships
- `db2://schema-summary` - Database schema and table information
- `db2://component-priorities` - Component importance by technology

## Development

### Project Structure
```
├── servers/
│   └── db2_mcp.py          # MCP server implementation
├── drivers/                # DB2 JDBC drivers (gitignored)
├── mcp_client.py          # Interactive client
├── pyproject.toml         # Dependencies
└── README.md
```

### Environment Variables
All configuration via `.env` file (gitignored). Copy `.env.example` to get started with the required variables.

## License

MIT License - see repository for details.