from fastapi import APIRouter

router = APIRouter()


@router.get("")
def list_attachments() -> list[dict]:
    return []
