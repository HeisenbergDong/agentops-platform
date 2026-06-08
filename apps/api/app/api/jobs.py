from fastapi import APIRouter
from pydantic import BaseModel

from app.services.orchestrator.service import OrchestratorService

router = APIRouter()
service = OrchestratorService()


class StartJobRequest(BaseModel):
    directions: list[str]


@router.post("/start")
def start_job(payload: StartJobRequest) -> dict:
    return service.start_job(payload.directions)


@router.post("/continue")
def continue_job(job_id: str | None = None) -> dict:
    return service.continue_job(job_id)


@router.post("/stop")
def stop_job(job_id: str | None = None) -> dict:
    return service.stop_job(job_id)


@router.get("/current")
def current_job() -> dict:
    return {"status": "idle", "job": None}
