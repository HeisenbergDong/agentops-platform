from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import User
from app.db.session import get_db
from app.services.feishu.local_records import list_local_feishu_records

router = APIRouter()


@router.get("")
def list_records(
    limit: int = 200,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    return list_local_feishu_records(db, user, limit=limit)
