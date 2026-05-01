import hashlib
import duckdb

schema_cache: dict = {}


def _cache_key(filepath: str) -> str:
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


# ── Single-file (CSV / TXT / single-sheet XLSX converted to CSV) ─────────────

def get_schema(filepath: str) -> list[dict]:
    """Returns schema for a single CSV file as [{name, dtype, samples}]."""
    key = _cache_key(filepath)
    if key not in schema_cache:
        schema_cache[key] = _parse_csv_schema(filepath)
    return schema_cache[key]


def _parse_csv_schema(filepath: str) -> list[dict]:
    safe = filepath.replace("\\", "/")
    conn = duckdb.connect()
    desc = conn.execute(f"DESCRIBE SELECT * FROM read_csv_auto('{safe}')").fetchall()
    sample_df = conn.execute(f"SELECT * FROM read_csv_auto('{safe}') LIMIT 3").fetchdf()
    conn.close()

    schema = []
    for row in desc:
        col, dtype = row[0], row[1]
        samples = sample_df[col].astype(str).tolist() if col in sample_df.columns else []
        schema.append({"name": col, "dtype": dtype, "samples": samples})
    return schema


def execute_sql(filepath: str, sql: str) -> list[dict]:
    """Execute SQL against a single CSV file. Table name inside SQL must be 'df'."""
    safe = filepath.replace("\\", "/")
    conn = duckdb.connect()
    conn.execute(f"CREATE VIEW df AS SELECT * FROM read_csv_auto('{safe}')")
    result = conn.execute(sql).fetchdf()
    conn.close()
    return result.to_dict(orient="records")


# ── Multi-sheet XLSX (stored as .duckdb warehouse file) ──────────────────────

def get_schema_multi(duckdb_path: str) -> dict[str, list[dict]]:
    """Returns schema for all tables in a multi-sheet DuckDB file.
    Returns {table_name: [{name, dtype, samples}]}
    """
    key = _cache_key(duckdb_path)
    if key not in schema_cache:
        schema_cache[key] = _parse_duckdb_schema(duckdb_path)
    return schema_cache[key]


def _parse_duckdb_schema(duckdb_path: str) -> dict[str, list[dict]]:
    conn = duckdb.connect(duckdb_path, read_only=True)
    tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]

    result = {}
    for table in tables:
        desc = conn.execute(f"DESCRIBE SELECT * FROM {table}").fetchall()
        sample_df = conn.execute(f"SELECT * FROM {table} LIMIT 3").fetchdf()
        schema = []
        for row in desc:
            col, dtype = row[0], row[1]
            samples = sample_df[col].astype(str).tolist() if col in sample_df.columns else []
            schema.append({"name": col, "dtype": dtype, "samples": samples})
        result[table] = schema

    conn.close()
    return result


def execute_sql_multi(duckdb_path: str, sql: str) -> list[dict]:
    """Execute SQL against a multi-sheet DuckDB file. Table names = sheet names."""
    conn = duckdb.connect(duckdb_path, read_only=True)
    result = conn.execute(sql).fetchdf()
    conn.close()
    return result.to_dict(orient="records")


def run_sql(stored_path: str, sql: str) -> list[dict]:
    """Unified SQL runner — detects CSV vs DuckDB warehouse automatically."""
    if stored_path.endswith(".duckdb"):
        return execute_sql_multi(stored_path, sql)
    return execute_sql(stored_path, sql)
