CHART_MODE_SYSTEM = """
You are a SQL and chart configuration generator for a BI tool.
The data is stored in DuckDB.

{schema_description}

Rules:
- Output ONLY valid JSON. No explanation, no markdown, no backticks.
- Use ONLY the column names listed above. Never invent columns.
- Use DuckDB-compatible SQL only.
- Use the exact table names listed above.

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

User: top 5 products by sales
Output: {{"sql": "SELECT product, SUM(sales) AS total_sales FROM df GROUP BY product ORDER BY total_sales DESC LIMIT 5", "chart_type": "bar", "x_axis": "product", "y_axis": "total_sales", "group_by": null, "title": "Top 5 Products by Sales", "error": null}}
"""

CHART_MODE_USER = """
<think>
First identify which columns and tables are relevant. Consider whether aggregation, filtering, or grouping is needed. Decide the best chart type. Then write the SQL.
</think>

User request: {user_prompt}

Output the JSON now.
"""

CHART_MODE_RETRY = """
The SQL you generated caused an error in DuckDB:

SQL: {sql}
Error: {error}

Fix the SQL and return the corrected JSON. Output ONLY valid JSON.
"""


def build_schema_description(schema, is_multi_sheet: bool) -> str:
    """
    Build the schema block injected into the system prompt.
    - Single file: one table named 'df'
    - Multi-sheet: multiple named tables
    """
    if not is_multi_sheet:
        lines = ["Available table: df", "Columns (name | dtype | sample values):"]
        for col in schema:
            samples = ", ".join(col["samples"])
            lines.append(f"  - {col['name']} ({col['dtype']}) — e.g. {samples}")
        return "\n".join(lines)

    else:
        blocks = []
        for table_name, columns in schema.items():
            lines = [f"Table: {table_name}", "Columns (name | dtype | sample values):"]
            for col in columns:
                samples = ", ".join(col["samples"])
                lines.append(f"  - {col['name']} ({col['dtype']}) — e.g. {samples}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
