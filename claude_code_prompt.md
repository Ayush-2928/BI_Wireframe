# Claude Code — Master Project Prompt
# BI Wireframe Agent (AI-Powered Business Intelligence Dashboard)

---

## What you are building

An AI-powered BI tool that lets users upload structured data (CSV/TXT), generate charts via natural language prompts, and iteratively refine them through a conversational chatbot. The system feels like ChatGPT but works like Power BI.

The core pipeline is:
```
User Prompt → LLM → Structured JSON (SQL + Chart Config) → DuckDB → Aggregated Data → Frontend Chart
```

The LLM never touches raw data. It only generates structured SQL + chart config as JSON. The backend controls all execution.

---

## Tech Stack

### Backend
- **FastAPI** — main API server with streaming support (`StreamingResponse`)
- **DuckDB** — analytics engine, reads uploaded CSVs via `read_csv_auto`, executes LLM-generated SQL
- **PostgreSQL + SQLAlchemy** — persistent app storage (users, projects, files, chart configs, chat history)
- **Python internal cache** — `functools.lru_cache` + module-level dict for schema caching, keyed by `hashlib.md5(filepath)`. NO Redis.
- **Ollama** (dev) / **OpenRouter** (demo) — LLM serving
- **Primary model**: `qwen2.5-coder:7b` (not base qwen — coder variant is significantly better at SQL)
- **Fallback model**: `gemma2` or `gemma3`

### Frontend (handled by separate frontend developer — your job is to expose clean APIs)
- React + TypeScript
- ECharts for chart rendering
- Chat interface (ChatGPT-style)
- MCQ-style suggestion buttons

### Dev Sharing
- **ngrok** free tier for exposing the FastAPI server to the frontend developer during development
- Start ngrok with: `ngrok http 8000`
- Share the generated URL with the frontend dev each session

---

## Project File Structure to Create

```
bi-agent/
├── main.py                  # FastAPI app entry point
├── config.py                # env vars, model names, DB URLs
├── database.py              # SQLAlchemy setup, table models
├── models.py                # Pydantic request/response schemas
├── cache.py                 # Python schema cache (lru_cache + dict)
├── routers/
│   ├── auth.py              # POST /login, POST /register
│   ├── projects.py          # GET/POST /projects
│   ├── upload.py            # POST /upload
│   ├── charts.py            # POST /generate-chart, POST /update-chart
│   ├── chat.py              # POST /suggestions
│   └── export.py            # GET /export
├── services/
│   ├── llm_service.py       # LLM call logic, mode switching, retry
│   ├── sql_service.py       # DuckDB execution, schema extraction
│   ├── validation_service.py# JSON validation, schema validation, SQL sanitization
│   └── export_service.py    # CSV/JSON export logic
└── prompts/
    ├── chart_mode.py        # CHART MODE system prompt template
    └── chat_mode.py         # CHAT MODE system prompt template
```

---

## Database Tables (PostgreSQL via SQLAlchemy)

```python
# Users
id, email, hashed_password, created_at

# Projects
id, user_id (FK), name, description, created_at, updated_at

# Files
id, project_id (FK), original_filename, stored_path, uploaded_at

# ChartConfigs
id, project_id (FK), file_id (FK), sql_query, chart_type,
x_axis, y_axis, group_by, title, created_at

# ChatHistory
id, project_id (FK), role (user|assistant), message, created_at
```

---

## API Endpoints — Full Spec

### POST /upload
- Accept: `multipart/form-data` with `file` (CSV or TXT) and `project_id`
- Save file to disk at `./uploads/{project_id}/{filename}`
- Extract schema (column names, dtypes, 3 sample values) immediately on upload
- Store schema in Python cache: `schema_cache[md5(filepath)] = schema`
- Store file path in PostgreSQL Files table
- Return: `{ file_id, filename, schema: [{col, dtype, samples}] }`

### POST /generate-chart
- Body: `{ project_id, file_id, prompt: str }`
- Fetch schema from cache (or re-parse if cache miss)
- Build CHART MODE prompt (see LLM section below)
- Call LLM, parse JSON response
- Validate JSON structure and column names against schema
- Execute SQL in DuckDB
- On DuckDB error: pass error string back to LLM (Stage 5 retry), max 2 retries
- Save ChartConfig to PostgreSQL
- Return: `{ chart_config, data: [{x, y}], chart_id }`

### POST /update-chart
- Body: `{ chart_id, prompt: str }`  
- Load existing chart config from PostgreSQL
- Treat as a refinement — pass existing config + new prompt to CHART MODE
- Same validation + execution flow as /generate-chart
- Return: `{ chart_config, data: [{x, y}], chart_id }`

### POST /suggestions
- Body: `{ project_id, file_id, current_chart_config: obj, message: str }`
- Load last 5 messages from ChatHistory for this project
- Build CHAT MODE prompt (see LLM section)
- Call LLM, parse suggestions JSON
- Save user message + assistant response to ChatHistory
- Return: `{ message: str, suggestions: [{label, action}] }`

### GET /export
- Query params: `chart_id`, `format` (csv | json)
- Re-execute the saved SQL for that chart_id in DuckDB
- Stream back the result as CSV or JSON file download

---

## LLM Service — Two Modes (CRITICAL)

### CHART MODE system prompt

```python
CHART_MODE_SYSTEM = """
You are a SQL and chart configuration generator for a BI tool.
The data is stored in a DuckDB table called `df`.

Available columns (name | dtype | sample values):
{schema_description}

Rules:
- Output ONLY valid JSON. No explanation, no markdown, no backticks.
- Use only the column names listed above. Never invent columns.
- Use DuckDB-compatible SQL only.
- Table name is always: df

Output format:
{{
  "sql": "SELECT ...",
  "chart_type": "bar | line | pie | scatter",
  "x_axis": "column_name",
  "y_axis": "column_name",
  "group_by": "column_name or null",
  "title": "short chart title",
  "error": null
}}

Few-shot examples:
User: show total revenue by region
Output: {{"sql": "SELECT region, SUM(revenue) AS total_revenue FROM df GROUP BY region ORDER BY total_revenue DESC", "chart_type": "bar", "x_axis": "region", "y_axis": "total_revenue", "group_by": null, "title": "Revenue by Region", "error": null}}

User: top 5 products by sales this year
Output: {{"sql": "SELECT product, SUM(sales) AS total_sales FROM df GROUP BY product ORDER BY total_sales DESC LIMIT 5", "chart_type": "bar", "x_axis": "product", "y_axis": "total_sales", "group_by": null, "title": "Top 5 Products by Sales", "error": null}}
"""

CHART_MODE_USER = """
<think>
First, identify which columns are relevant. Consider whether aggregation, filtering, or grouping is needed. Decide the best chart type. Then write the SQL.
</think>

User request: {user_prompt}

Output the JSON now.
"""
```

> Backend must strip everything between `<think>` and `</think>` before parsing JSON.

---

### CHAT MODE system prompt

```python
CHAT_MODE_SYSTEM = """
You are a helpful BI assistant improving a data dashboard.
The user has an existing chart. Suggest concrete improvements as selectable options.

Current chart config:
{current_chart_config}

Conversation history (last 5 messages):
{chat_history}

Output ONLY valid JSON:
{{
  "message": "Brief explanation of suggestions",
  "suggestions": [
    {{"label": "Short button label", "action": "Detailed instruction for chart update"}},
    {{"label": "Short button label", "action": "Detailed instruction for chart update"}},
    {{"label": "Short button label", "action": "Detailed instruction for chart update"}}
  ]
}}
"""
```

---

## SQL Generation Pipeline — 5 Stages

Implement these in `services/sql_service.py` and `services/llm_service.py`:

**Stage 1 — Schema cache**
```python
import hashlib
from functools import lru_cache

schema_cache: dict = {}

def get_schema(filepath: str) -> list[dict]:
    key = hashlib.md5(open(filepath, 'rb').read()).hexdigest()
    if key not in schema_cache:
        schema_cache[key] = _parse_schema(filepath)
    return schema_cache[key]

def _parse_schema(filepath: str) -> list[dict]:
    import duckdb
    conn = duckdb.connect()
    result = conn.execute(f"DESCRIBE SELECT * FROM read_csv_auto('{filepath}')").fetchall()
    sample = conn.execute(f"SELECT * FROM read_csv_auto('{filepath}') LIMIT 3").fetchall()
    # Build [{col, dtype, samples}] structure
    ...
```

**Stage 2 — Prompt construction**
- Inject schema into `CHART_MODE_SYSTEM` template
- Include 2 hardcoded few-shot examples (as shown above)

**Stage 3 — Chain-of-thought**
- Include `<think>` tag instruction in user message
- Strip `<think>...</think>` block from response before JSON parsing:
```python
import re
def strip_thinking(text: str) -> str:
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
```

**Stage 4 — Validation before DuckDB**
```python
def validate_sql_columns(sql: str, schema: list[dict]) -> bool:
    known_cols = {col['name'].lower() for col in schema}
    # Extract column references from SQL and check against known_cols
    ...
```

**Stage 5 — Error-as-context retry**
```python
async def generate_with_retry(prompt, schema, max_retries=2):
    for attempt in range(max_retries + 1):
        result = await call_llm(prompt)
        sql = result.get('sql')
        try:
            data = execute_duckdb(sql)
            return result, data
        except Exception as e:
            if attempt < max_retries:
                prompt = build_retry_prompt(sql, str(e), schema)
            else:
                # Switch to fallback model
                result = await call_llm(prompt, model=FALLBACK_MODEL)
                ...
```

---

## Validation Layer — `services/validation_service.py`

Implement these checks in order:

1. **JSON structure check** — required keys: `sql`, `chart_type`, `x_axis`, `y_axis`
2. **chart_type check** — must be one of: `bar`, `line`, `pie`, `scatter`
3. **column existence check** — all columns referenced in SQL must exist in schema
4. **SQL sanitization** — reject any SQL containing: `DROP`, `DELETE`, `INSERT`, `UPDATE`, `CREATE`, `ALTER`, `EXEC`, `--`
5. **DuckDB execution** — wrap in try/except, return typed error on failure

---

## CORS Configuration (Important for ngrok + frontend dev)

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

This is required or the frontend dev will get CORS errors when hitting the ngrok URL.

---

## Environment Config — `config.py`

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://user:pass@localhost/bi_agent"
    UPLOAD_DIR: str = "./uploads"
    LLM_BASE_URL: str = "http://localhost:11434"  # Ollama
    PRIMARY_MODEL: str = "qwen2.5-coder:7b"
    FALLBACK_MODEL: str = "gemma2:9b"
    OPENROUTER_API_KEY: str = ""  # for demo/production
    SECRET_KEY: str = "change-this-in-production"
    
    class Config:
        env_file = ".env"

settings = Settings()
```

---

## Frontend API Contract (share this with your frontend developer)

All responses follow this envelope:
```json
{
  "success": true,
  "data": { ... },
  "error": null
}
```

Chart data returned by `/generate-chart` and `/update-chart`:
```json
{
  "chart_id": "uuid",
  "chart_config": {
    "chart_type": "bar",
    "x_axis": "region",
    "y_axis": "total_revenue",
    "title": "Revenue by Region"
  },
  "data": [
    { "region": "North", "total_revenue": 42000 },
    { "region": "South", "total_revenue": 38000 }
  ]
}
```

Suggestions returned by `/suggestions`:
```json
{
  "message": "Here are some ways to improve this chart:",
  "suggestions": [
    { "label": "Group by month", "action": "Break down the revenue by month instead of region" },
    { "label": "Switch to line chart", "action": "Show revenue trend over time as a line chart" },
    { "label": "Add top 10 filter", "action": "Limit to top 10 regions by revenue" }
  ]
}
```

---

## What to build first — recommended order

1. `database.py` — get PostgreSQL tables created
2. `cache.py` + `services/sql_service.py` — schema parsing and DuckDB execution
3. `routers/upload.py` — file upload + schema extraction
4. `services/llm_service.py` — CHART MODE with full 5-stage pipeline
5. `routers/charts.py` — /generate-chart endpoint end-to-end
6. `routers/chat.py` — CHAT MODE + /suggestions
7. `routers/auth.py` — basic JWT auth
8. `routers/export.py` — CSV/JSON export
9. Add CORS middleware and test with ngrok

---

## Key constraints — do not violate these

- LLM only ever outputs JSON. It never runs code or directly touches data.
- DuckDB is never exposed to the frontend. Only aggregated result rows are returned.
- The table name in ALL SQL queries is always `df`, regardless of the actual filename.
- Schema cache is always consulted before re-parsing a file.
- SQL is always validated against the schema before DuckDB execution.
- Chat history sent to CHAT MODE is always capped at the last 5 messages.
- Retry logic is always error-as-context, never a blank retry.
