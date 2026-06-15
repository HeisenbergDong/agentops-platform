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
    rows = db.scalars(
        select(AutomationError)
        .where(AutomationError.job_id.is_(None) | AutomationError.job_id.in_(select_job_ids_for_user(user.id)))
        .order_by(desc(AutomationError.created_at))
        .limit(100)
    ).all()
    return [
        {
            "id": item.id,
            "job_id": item.job_id,
            "round_id": item.round_id,
            "kind": item.kind,
            "stage": item.stage,
            "message": item.message,
            "details": item.details,
            "resolved": item.resolved,
            "created_at": item.created_at.isoformat(),
        }
        for item in rows
    ]


def select_job_ids_for_user(user_id: str):
    from app.db.models import Job

    return select(Job.id).where(Job.user_id == user_id)
