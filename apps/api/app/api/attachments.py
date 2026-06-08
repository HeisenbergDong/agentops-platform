from fastapi import APIRouter, Depends

from app.api.deps import current_user
from app.db.models import User

router = APIRouter()


@router.get("")
def list_attachments(user: User = Depends(current_user)) -> list[dict]:
    return []
