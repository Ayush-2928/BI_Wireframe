import io
import os
import re
import uuid
import datetime

import duckdb
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session

from database import get_db, Wireframe, File as FileModel, ChartConfig
from dependencies import get_current_user
from services.sql_service import get_schema, get_schema_multi, run_sql
from services.llm_service import generate_initial_dashboard
from config import settings

router = APIRouter()

ALLOWED_EXTENSIONS = {".csv", ".txt", ".xlsx"}


def _safe_table_name(sheet_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", sheet_name)
    if name[0].isdigit():
        name = "sheet_" + name
    return name.lower()


def _unique_table_name(base: str, seen: set) -> str:
    if base not in seen:
        return base
    i = 2
    while f"{base}_{i}" in seen:
        i += 1
    return f"{base}_{i}"


def _build_duckdb_warehouse(content: bytes, duckdb_path: str, wireframe_name: str) -> list[str]:
    if os.path.exists(duckdb_path):
        os.remove(duckdb_path)

    wf_prefix = _safe_table_name(wireframe_name)
    xl = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    conn = duckdb.connect(duckdb_path)
    table_names = []
    seen = set()

    for sheet_name in xl.sheet_names:
        df = xl.parse(sheet_name)
        base = f"{wf_prefix}_{_safe_table_name(sheet_name)}"
        table = _unique_table_name(base, seen)
        seen.add(table)
        conn.register("_tmp", df)
        conn.execute(f"CREATE TABLE {table} AS SELECT * FROM _tmp")
        table_names.append(table)

    conn.close()
    return table_names


async def process_file(
    file: UploadFile,
    wireframe_id: str,
    wireframe_name: str,
    db: Session,
) -> dict:
    """
    Core file processing logic — shared between POST /upload and
    POST /wireframes/create-with-file. Returns the file data dict.
    """
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only CSV, TXT, and XLSX files are allowed")

    upload_dir = os.path.join(settings.UPLOAD_DIR, wireframe_id)
    os.makedirs(upload_dir, exist_ok=True)

    content = await file.read()
    base_name = os.path.splitext(file.filename)[0]

    # ── XLSX ──────────────────────────────────────────────────────────────────
    if ext == ".xlsx":
        try:
            xl = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
            sheet_count = len(xl.sheet_names)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not read XLSX: {str(e)}")

        if sheet_count == 1:
            csv_path = os.path.join(upload_dir, base_name + ".csv")
            xl.parse(xl.sheet_names[0]).to_csv(csv_path, index=False)
            try:
                schema = get_schema(csv_path)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Could not parse file: {str(e)}")

            record = FileModel(
                id=str(uuid.uuid4()),
                wireframe_id=wireframe_id,
                original_filename=file.filename,
                stored_path=csv_path,
                uploaded_at=datetime.datetime.utcnow(),
            )
            db.add(record)
            db.flush()

            return {
                "file_id": record.id,
                "filename": file.filename,
                "multi_sheet": False,
                "schema": schema,
            }

        else:
            duckdb_path = os.path.join(upload_dir, base_name + ".duckdb")
            try:
                table_names = _build_duckdb_warehouse(content, duckdb_path, wireframe_name)
                schema_by_sheet = get_schema_multi(duckdb_path)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Could not process sheets: {str(e)}")

            record = FileModel(
                id=str(uuid.uuid4()),
                wireframe_id=wireframe_id,
                original_filename=file.filename,
                stored_path=duckdb_path,
                uploaded_at=datetime.datetime.utcnow(),
            )
            db.add(record)
            db.flush()

            return {
                "file_id": record.id,
                "filename": file.filename,
                "multi_sheet": True,
                "tables": table_names,
                "schema": schema_by_sheet,
            }

    # ── CSV / TXT ─────────────────────────────────────────────────────────────
    else:
        filepath = os.path.join(upload_dir, file.filename)
        with open(filepath, "wb") as f:
            f.write(content)

        try:
            schema = get_schema(filepath)
        except Exception as e:
            os.remove(filepath)
            raise HTTPException(status_code=422, detail=f"Could not parse file: {str(e)}")

        record = FileModel(
            id=str(uuid.uuid4()),
            wireframe_id=wireframe_id,
            original_filename=file.filename,
            stored_path=filepath,
            uploaded_at=datetime.datetime.utcnow(),
        )
        db.add(record)
        db.flush()

        return {
            "file_id": record.id,
            "filename": file.filename,
            "multi_sheet": False,
            "schema": schema,
        }


def _build_init_charts(
    file_id: str,
    file_data: dict,
    wireframe_id: str,
    db,
    prompt_suggestion: str | None = None,
) -> dict:
    """
    Generate KPI cards + charts from schema and persist them.
    Returns {"kpis": [...], "charts": [...]}.
    Never raises — always returns empty dict structure on failure.
    """
    import logging
    log = logging.getLogger("bi-agent")

    is_multi = file_data.get("multi_sheet", False)
    schema = file_data.get("schema")

    file_record = db.query(FileModel).filter(FileModel.id == file_id).first()
    if not file_record:
        return {"kpis": [], "charts": []}

    dashboard = generate_initial_dashboard(
        schema,
        is_multi_sheet=is_multi,
        prompt_suggestion=prompt_suggestion,
    )

    # ── Save & execute KPIs ───────────────────────────────────────────────────
    kpi_results = []
    for kpi in dashboard.get("kpis", []):
        try:
            data = run_sql(file_record.stored_path, kpi["sql"])
            record = ChartConfig(
                id=str(uuid.uuid4()),
                wireframe_id=wireframe_id,
                file_id=file_id,
                sql_query=kpi["sql"],
                chart_type="kpi",
                x_axis=None,
                y_axis=kpi["value_key"],
                group_by=None,
                title=kpi["title"],
                created_at=datetime.datetime.utcnow(),
            )
            db.add(record)
            db.commit()
            kpi_results.append({
                "chart_id": record.id,
                "type": "kpi",
                "title": record.title,
                "value_key": kpi["value_key"],
                "sql": record.sql_query,
                "data": data,
            })
        except Exception as e:
            log.warning(f"Init KPI skipped: {e}")

    # ── Save & execute charts ─────────────────────────────────────────────────
    chart_results = []
    for config in dashboard.get("charts", []):
        try:
            data = run_sql(file_record.stored_path, config["sql"])
            record = ChartConfig(
                id=str(uuid.uuid4()),
                wireframe_id=wireframe_id,
                file_id=file_id,
                sql_query=config["sql"],
                chart_type=config["chart_type"],
                x_axis=config["x_axis"],
                y_axis=config["y_axis"],
                group_by=config.get("group_by"),
                title=config["title"],
                created_at=datetime.datetime.utcnow(),
            )
            db.add(record)
            db.commit()
            chart_results.append({
                "chart_id": record.id,
                "type": "chart",
                "chart_type": record.chart_type,
                "x_axis": record.x_axis,
                "y_axis": record.y_axis,
                "group_by": record.group_by,
                "title": record.title,
                "sql": record.sql_query,
                "data": data,
            })
        except Exception as e:
            log.warning(f"Init chart skipped: {e}")

    return {"kpis": kpi_results, "charts": chart_results}


@router.post("")
async def upload_file(
    wireframe_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    wireframe = (
        db.query(Wireframe)
        .filter(Wireframe.id == wireframe_id, Wireframe.username == username)
        .first()
    )
    if not wireframe:
        raise HTTPException(status_code=404, detail="Wireframe not found")

    file_data = await process_file(file, wireframe_id, wireframe.name, db)
    db.commit()

    # Fire init chart generation — never blocks upload on failure
    initial_charts = _build_init_charts(
        file_data["file_id"],
        file_data,
        wireframe_id,
        db,
    )

    return {"success": True, "data": {**file_data, "initial_charts": initial_charts}, "error": None}


@router.get("/{wireframe_id}/files")
def list_files(
    wireframe_id: str,
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    wireframe = (
        db.query(Wireframe)
        .filter(Wireframe.id == wireframe_id, Wireframe.username == username)
        .first()
    )
    if not wireframe:
        raise HTTPException(status_code=404, detail="Wireframe not found")

    files = db.query(FileModel).filter(FileModel.wireframe_id == wireframe_id).all()
    return {
        "success": True,
        "data": [
            {
                "file_id": f.id,
                "filename": f.original_filename,
                "multi_sheet": f.stored_path.endswith(".duckdb"),
                "uploaded_at": f.uploaded_at.isoformat() if f.uploaded_at else None,
            }
            for f in files
        ],
        "error": None,
    }
