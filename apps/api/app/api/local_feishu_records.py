from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import User
from app.db.session import get_db
from app.services.feishu.local_records import (
    EXCEL_MIME_TYPE,
    export_local_feishu_records_xlsx,
    list_local_feishu_records,
)

router = APIRouter()


@router.get("")
def list_records(
    limit: int = 200,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    return list_local_feishu_records(db, user, limit=limit)


@router.get("/export.xlsx")
def export_records(
    limit: int = 1000,
    keyword: str = "",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    records = list_local_feishu_records(db, user, limit=limit)
    content = export_local_feishu_records_xlsx(records, keyword=keyword)
    filename = f"agentops-local-records-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
    return Response(
        content=content,
        media_type=EXCEL_MIME_TYPE,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
        },
    )
