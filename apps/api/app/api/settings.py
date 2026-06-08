from fastapi import APIRouter

router = APIRouter()


@router.get("")
def get_settings() -> dict:
    return {
        "model_configs": [],
        "github": {"configured": False},
        "feishu": {"configured": False},
        "worker": {"configured": False},
    }
