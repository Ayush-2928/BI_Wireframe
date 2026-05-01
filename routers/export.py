import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db, Wireframe, File as FileModel, ChartConfig
from dependencies import get_current_user
from services.sql_service import run_sql

router = APIRouter()


@router.get("")
def export_chart(
    chart_id: str = Query(...),
    format: str = Query("csv", regex="^(csv|json)$"),
    db: Session = Depends(get_db),
    username: str = Depends(get_current_user),
):
    chart = db.query(ChartConfig).filter(ChartConfig.id == chart_id).first()
    if not chart:
        raise HTTPException(status_code=404, detail="Chart not found")

    # Verify ownership
    wireframe = db.query(Wireframe).filter(
        Wireframe.id == chart.wireframe_id,
        Wireframe.username == username,
    ).first()
    if not wireframe:
        raise HTTPException(status_code=403, detail="Not authorized")

    file_record = db.query(FileModel).filter(FileModel.id == chart.file_id).first()
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        rows = run_sql(file_record.stored_path, chart.sql_query)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"SQL execution failed: {str(e)}")

    safe_title = chart.title.replace(" ", "_").replace("/", "-") if chart.title else "export"

    if format == "csv":
        if not rows:
            csv_content = ""
        else:
            headers = list(rows[0].keys())
            lines = [",".join(headers)]
            for row in rows:
                lines.append(",".join(str(row.get(h, "")) for h in headers))
            csv_content = "\n".join(lines)

        return StreamingResponse(
            io.StringIO(csv_content),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={safe_title}.csv"},
        )

    else:  # json
        return StreamingResponse(
            io.StringIO(json.dumps(rows, default=str, indent=2)),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={safe_title}.json"},
        )
