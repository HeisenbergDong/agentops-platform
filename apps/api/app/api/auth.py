from fastapi import APIRouter

router = APIRouter()


@router.get("/me")
def me() -> dict:
    return {"id": "dev-user", "name": "Development User"}
