import uuid
import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File as UploadFileType, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database import get_db, Wireframe, File, ChartConfig, ChatHistory
from dependencies import get_current_user
from routers.upload import process_file, _build_init_charts

router = APIRouter()


class CreateWireframeRequest(BaseModel):
    name: str
    description: Optional[str] = None


class UpdateWireframeRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


def _serialize(w: Wireframe) -> dict:
    return {
        "id": w.id,
        "name": w.name,
        "description": w.description,
        "created_at": w.created_at.isoformat() if w.created_at else None,
        "updated_at": w.updated_at.isoformat() if w.updated_at else None,
    }


@router.get("")
def list_wireframes(
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    wireframes = (
        db.query(Wireframe)
        .filter(Wireframe.username == username)
        .order_by(Wireframe.updated_at.desc())
        .all()
    )
    return {
        "success": True,
        "data": [_serialize(w) for w in wireframes],
        "error": None,
    }


@router.post("")
def create_wireframe(
    req: CreateWireframeRequest,
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    wireframe = Wireframe(
        id=str(uuid.uuid4()),
        username=username,
        name=req.name,
        description=req.description,
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
    )
    db.add(wireframe)
    db.commit()
    db.refresh(wireframe)
    return {"success": True, "data": _serialize(wireframe), "error": None}


@router.get("/{wireframe_id}")
def get_wireframe(
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
    return {"success": True, "data": _serialize(wireframe), "error": None}


@router.put("/{wireframe_id}")
def update_wireframe(
    wireframe_id: str,
    req: UpdateWireframeRequest,
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

    if req.name is not None:
        wireframe.name = req.name
    if req.description is not None:
        wireframe.description = req.description
    wireframe.updated_at = datetime.datetime.utcnow()

    db.commit()
    db.refresh(wireframe)
    return {"success": True, "data": _serialize(wireframe), "error": None}


@router.delete("/{wireframe_id}")
def delete_wireframe(
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

    # Delete child records in correct order before deleting wireframe
    file_records = db.query(File).filter(File.wireframe_id == wireframe_id).all()

    db.query(ChartConfig).filter(ChartConfig.wireframe_id == wireframe_id).delete()
    db.query(ChatHistory).filter(ChatHistory.wireframe_id == wireframe_id).delete()

    for f in file_records:
        db.delete(f)

    db.delete(wireframe)
    db.commit()
    return {"success": True, "data": {"deleted_id": wireframe_id}, "error": None}


@router.post("/create-with-file")
async def create_wireframe_with_file(
    name: str = Form(...),
    description: Optional[str] = Form(None),
    prompt_suggestion: Optional[str] = Form(None),
    file: UploadFile = UploadFileType(...),
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    # Step 1 — create the wireframe
    wireframe = Wireframe(
        id=str(uuid.uuid4()),
        username=username,
        name=name,
        description=description,
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
    )
    db.add(wireframe)
    db.flush()

    # Step 2 — process and attach the file
    file_data = await process_file(file, wireframe.id, wireframe.name, db)
    db.commit()

    # Step 3 — generate initial charts
    initial_charts = _build_init_charts(
        file_data["file_id"],
        file_data,
        wireframe.id,
        db,
        prompt_suggestion=prompt_suggestion,
    )

    return {
        "success": True,
        "data": {
            "wireframe": _serialize(wireframe),
            "file": {**file_data, "initial_charts": initial_charts},
        },
        "error": None,
    }
