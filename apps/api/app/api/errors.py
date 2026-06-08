from fastapi import APIRouter

router = APIRouter()


@router.get("")
def list_errors() -> list[dict]:
    return []
