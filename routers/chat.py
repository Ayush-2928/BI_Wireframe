import uuid
import datetime
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Any

from database import get_db, Wireframe, File as FileModel, ChartConfig, ChatHistory
from dependencies import get_current_user
from services.llm_service import generate_suggestions
from services.sql_service import get_schema, get_schema_multi

router = APIRouter()


class SuggestionsRequest(BaseModel):
    wireframe_id: str
    file_id: str
    message: str


class SuggestionSelectionRequest(BaseModel):
    wireframe_id: str
    assistant_message_id: str
    suggestion: dict[str, Any]
    action_result: Optional[dict[str, Any]] = None


@router.post("")
def get_suggestions(
    req: SuggestionsRequest,
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    # Verify ownership
    wireframe = db.query(Wireframe).filter(
        Wireframe.id == req.wireframe_id,
        Wireframe.username == username,
    ).first()
    if not wireframe:
        raise HTTPException(status_code=404, detail="Wireframe not found")

    # Load last 5 chat messages for context
    history_records = (
        db.query(ChatHistory)
        .filter(
            ChatHistory.wireframe_id == req.wireframe_id,
            ChatHistory.event_type == "message",
        )
        .order_by(ChatHistory.created_at.desc())
        .limit(5)
        .all()
    )
    chat_history = [
        {"role": h.role, "message": h.message}
        for h in reversed(history_records)
    ]

    # Load current charts on the dashboard for context (include axes for richer context)
    chart_records = db.query(ChartConfig).filter(
        ChartConfig.wireframe_id == req.wireframe_id,
        ChartConfig.chart_type != "kpi",
    ).all()
    current_charts = [
        {
            "chart_id": c.id,
            "chart_type": c.chart_type,
            "title": c.title,
            "x_axis": c.x_axis,
            "y_axis": c.y_axis,
        }
        for c in chart_records
    ]

    # Load schema for the file so LLM knows all available columns
    file_record = db.query(FileModel).filter(FileModel.id == req.file_id).first()
    schema_columns = []
    if file_record:
        try:
            is_multi = file_record.stored_path.endswith(".duckdb")
            if is_multi:
                raw_schema = get_schema_multi(file_record.stored_path)
                for table, cols in raw_schema.items():
                    schema_columns += [f"{table}.{c['name']} ({c['dtype']})" for c in cols]
            else:
                raw_schema = get_schema(file_record.stored_path)
                schema_columns = [f"{c['name']} ({c['dtype']})" for c in raw_schema]
        except Exception:
            pass

    # Call CHAT MODE
    result = generate_suggestions(
        user_message=req.message,
        current_charts=current_charts,
        chat_history=chat_history,
        schema_columns=schema_columns,
    )

    # Save user message + assistant response to history
    user_history = ChatHistory(
        id=str(uuid.uuid4()),
        wireframe_id=req.wireframe_id,
        role="user",
        event_type="message",
        message=req.message,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(user_history)

    assistant_history = ChatHistory(
        id=str(uuid.uuid4()),
        wireframe_id=req.wireframe_id,
        role="assistant",
        event_type="message",
        message=result.get("message", ""),
        suggestions_json=json.dumps(result.get("suggestions", [])),
        created_at=datetime.datetime.utcnow(),
    )
    db.add(assistant_history)
    db.commit()

    return {
        "success": True,
        "data": {
            **result,
            "assistant_message_id": assistant_history.id,
        },
        "error": None,
    }


@router.post("/suggestion-selected")
def mark_suggestion_selected(
    req: SuggestionSelectionRequest,
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    wireframe = db.query(Wireframe).filter(
        Wireframe.id == req.wireframe_id,
        Wireframe.username == username,
    ).first()
    if not wireframe:
        raise HTTPException(status_code=404, detail="Wireframe not found")

    assistant_message = db.query(ChatHistory).filter(
        ChatHistory.id == req.assistant_message_id,
        ChatHistory.wireframe_id == req.wireframe_id,
        ChatHistory.role == "assistant",
    ).first()
    if not assistant_message:
        raise HTTPException(status_code=404, detail="Assistant message not found")

    selection_event = ChatHistory(
        id=str(uuid.uuid4()),
        wireframe_id=req.wireframe_id,
        role="system",
        event_type="suggestion_selected",
        message="Suggestion selected",
        metadata_json=json.dumps(
            {
                "selected_suggestion": req.suggestion,
                "action_result": req.action_result,
            }
        ),
        parent_message_id=req.assistant_message_id,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(selection_event)
    db.commit()

    return {"success": True, "data": {"selection_event_id": selection_event.id}, "error": None}


@router.get("/{wireframe_id}/history")
def get_chat_history(
    wireframe_id: str,
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    wireframe = db.query(Wireframe).filter(
        Wireframe.id == wireframe_id,
        Wireframe.username == username,
    ).first()
    if not wireframe:
        raise HTTPException(status_code=404, detail="Wireframe not found")

    history = (
        db.query(ChatHistory)
        .filter(ChatHistory.wireframe_id == wireframe_id)
        .order_by(ChatHistory.created_at)
        .all()
    )
    return {
        "success": True,
        "data": [
            {
                "id": h.id,
                "role": h.role,
                "event_type": h.event_type,
                "message": h.message,
                "suggestions": json.loads(h.suggestions_json) if h.suggestions_json else None,
                "metadata": json.loads(h.metadata_json) if h.metadata_json else None,
                "parent_message_id": h.parent_message_id,
                "created_at": h.created_at.isoformat() if h.created_at else None,
            }
            for h in history
        ],
        "error": None,
    }
