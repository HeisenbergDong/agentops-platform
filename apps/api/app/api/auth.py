from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.bootstrap import ensure_dev_user
from app.db.session import get_db

router = APIRouter()


@router.get("/me")
def me(db: Session = Depends(get_db)) -> dict:
    user = ensure_dev_user(db)
    return {"id": user.id, "email": user.email, "name": user.display_name}
