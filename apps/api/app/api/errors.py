from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import AutomationError, User
from app.db.session import get_db

router = APIRouter()


@router.get("")
def list_errors(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(AutomationError).order_by(desc(AutomationError.created_at)).limit(100)).all()
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "stage": item.stage,
            "message": item.message,
            "resolved": item.resolved,
            "created_at": item.created_at.isoformat(),
        }
        for item in rows
    ]
