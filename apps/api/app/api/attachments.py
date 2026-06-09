from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.models import Attachment, User
from app.db.session import get_db

router = APIRouter()


@router.get("")
def list_attachments(
    job_id: str | None = None,
    round_id: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(Attachment).order_by(Attachment.created_at.desc())
    if user.role != "admin":
        query = query.where(Attachment.user_id == user.id)
    if job_id:
        query = query.where(Attachment.job_id == job_id)
    if round_id:
        query = query.where(Attachment.round_id == round_id)
    return [serialize_attachment(item) for item in db.scalars(query).all()]


def serialize_attachment(item: Attachment) -> dict:
    return {
        "id": item.id,
        "user_id": item.user_id,
        "job_id": item.job_id,
        "round_id": item.round_id,
        "kind": item.kind,
        "filename": item.filename,
        "path": item.path,
        "content_type": item.content_type,
        "size_bytes": item.size_bytes,
        "created_at": item.created_at.isoformat(),
    }
