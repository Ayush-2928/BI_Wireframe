import uuid
import datetime
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Any

from database import get_db, Wireframe, File as FileModel, ChartConfig, ChatHistory
from dependencies import get_current_user
from services.llm_service import generate_chart_config
from services.sql_service import run_sql

router = APIRouter()


class GenerateChartRequest(BaseModel):
    wireframe_id: str
    file_id: str
    prompt: str
    assistant_message_id: Optional[str] = None
    selected_suggestion: Optional[dict[str, Any]] = None


class UpdateChartRequest(BaseModel):
    prompt: str
    assistant_message_id: Optional[str] = None
    selected_suggestion: Optional[dict[str, Any]] = None


def _get_file_or_404(file_id: str, wireframe_id: str, db: Session) -> FileModel:
    f = db.query(FileModel).filter(
        FileModel.id == file_id,
        FileModel.wireframe_id == wireframe_id,
    ).first()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return f


def _save_chart(db, wireframe_id, file_id, config: dict) -> ChartConfig:
    record = ChartConfig(
        id=str(uuid.uuid4()),
        wireframe_id=wireframe_id,
        file_id=file_id,
        sql_query=config.get("sql"),
        chart_type=config.get("chart_type"),
        x_axis=config.get("x_axis"),
        y_axis=config.get("y_axis"),
        group_by=config.get("group_by"),
        title=config.get("title"),
        created_at=datetime.datetime.utcnow(),
    )
    db.add(record)
    return record


def _serialize_chart(record: ChartConfig, data: list) -> dict:
    return {
        "chart_id": record.id,
        "chart_type": record.chart_type,
        "x_axis": record.x_axis,
        "y_axis": record.y_axis,
        "group_by": record.group_by,
        "title": record.title,
        "sql": record.sql_query,
        "data": data,
    }


def _log_selected_suggestion(
    db: Session,
    wireframe_id: str,
    assistant_message_id: Optional[str],
    selected_suggestion: Optional[dict[str, Any]],
    action_result: dict[str, Any],
):
    if not assistant_message_id or not selected_suggestion:
        return

    selection_event = ChatHistory(
        id=str(uuid.uuid4()),
        wireframe_id=wireframe_id,
        role="system",
        event_type="suggestion_selected",
        message="Suggestion selected",
        metadata_json=json.dumps(
            {
                "selected_suggestion": selected_suggestion,
                "action_result": action_result,
            }
        ),
        parent_message_id=assistant_message_id,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(selection_event)


# ── GET all charts for a wireframe (re-runs SQL for fresh data) ───────────────

@router.get("/{wireframe_id}")
def get_charts(
    wireframe_id: str,
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    wireframe = db.query(Wireframe).filter(
        Wireframe.id == wireframe_id, Wireframe.username == username
    ).first()
    if not wireframe:
        raise HTTPException(status_code=404, detail="Wireframe not found")

    chart_records = db.query(ChartConfig).filter(
        ChartConfig.wireframe_id == wireframe_id
    ).order_by(ChartConfig.created_at).all()

    kpis, charts = [], []
    for record in chart_records:
        file_record = db.query(FileModel).filter(FileModel.id == record.file_id).first()
        if not file_record:
            continue
        try:
            data = run_sql(file_record.stored_path, record.sql_query)
        except Exception:
            data = []

        if record.chart_type == "kpi":
            kpis.append({
                "chart_id": record.id,
                "type": "kpi",
                "title": record.title,
                "value_key": record.y_axis,
                "sql": record.sql_query,
                "data": data,
            })
        else:
            charts.append(_serialize_chart(record, data))

    return {"success": True, "data": {"kpis": kpis, "charts": charts}, "error": None}


# ── POST generate a new chart from a user prompt ──────────────────────────────

@router.post("/generate")
def generate_chart(
    req: GenerateChartRequest,
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    wireframe = db.query(Wireframe).filter(
        Wireframe.id == req.wireframe_id, Wireframe.username == username
    ).first()
    if not wireframe:
        raise HTTPException(status_code=404, detail="Wireframe not found")

    file_record = _get_file_or_404(req.file_id, req.wireframe_id, db)
    is_multi = file_record.stored_path.endswith(".duckdb")

    from services.sql_service import get_schema, get_schema_multi
    schema = get_schema_multi(file_record.stored_path) if is_multi else get_schema(file_record.stored_path)

    try:
        config = generate_chart_config(req.prompt, schema, is_multi_sheet=is_multi)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Chart generation failed: {str(e)}")

    try:
        data = run_sql(file_record.stored_path, config["sql"])
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"SQL execution failed: {str(e)}")

    record = _save_chart(db, req.wireframe_id, req.file_id, config)
    action_result = _serialize_chart(record, data)
    _log_selected_suggestion(
        db=db,
        wireframe_id=req.wireframe_id,
        assistant_message_id=req.assistant_message_id,
        selected_suggestion=req.selected_suggestion,
        action_result=action_result,
    )
    db.commit()

    return {"success": True, "data": action_result, "error": None}


# ── POST update an existing chart from a user prompt ─────────────────────────

@router.post("/{chart_id}/update")
def update_chart(
    chart_id: str,
    req: UpdateChartRequest,
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    chart_record = db.query(ChartConfig).filter(ChartConfig.id == chart_id).first()
    if not chart_record:
        raise HTTPException(status_code=404, detail="Chart not found")

    # Verify ownership via wireframe
    wireframe = db.query(Wireframe).filter(
        Wireframe.id == chart_record.wireframe_id,
        Wireframe.username == username,
    ).first()
    if not wireframe:
        raise HTTPException(status_code=403, detail="Not authorized")

    file_record = db.query(FileModel).filter(FileModel.id == chart_record.file_id).first()
    is_multi = file_record.stored_path.endswith(".duckdb")

    from services.sql_service import get_schema, get_schema_multi
    schema = get_schema_multi(file_record.stored_path) if is_multi else get_schema(file_record.stored_path)

    existing_config = {
        "chart_type": chart_record.chart_type,
        "x_axis": chart_record.x_axis,
        "y_axis": chart_record.y_axis,
        "title": chart_record.title,
        "sql": chart_record.sql_query,
    }

    try:
        config = generate_chart_config(
            req.prompt, schema,
            is_multi_sheet=is_multi,
            existing_config=existing_config,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Chart update failed: {str(e)}")

    try:
        data = run_sql(file_record.stored_path, config["sql"])
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"SQL execution failed: {str(e)}")

    # Update the existing record in place
    chart_record.sql_query = config["sql"]
    chart_record.chart_type = config["chart_type"]
    chart_record.x_axis = config["x_axis"]
    chart_record.y_axis = config["y_axis"]
    chart_record.group_by = config.get("group_by")
    chart_record.title = config["title"]
    action_result = _serialize_chart(chart_record, data)
    _log_selected_suggestion(
        db=db,
        wireframe_id=chart_record.wireframe_id,
        assistant_message_id=req.assistant_message_id,
        selected_suggestion=req.selected_suggestion,
        action_result=action_result,
    )
    db.commit()

    return {"success": True, "data": action_result, "error": None}
