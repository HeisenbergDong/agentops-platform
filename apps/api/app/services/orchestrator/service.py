from app.core.logging import runtime_event
from app.services.orchestrator.states import JobState


class OrchestratorService:
    """Server-side owner of job state transitions.

    This first skeleton intentionally records the contract before implementing
    queue execution. Later iterations will persist every transition in PostgreSQL.
    """

    def start_job(self, directions: list[str]) -> dict:
        return runtime_event(
            JobState.JOB_STARTING,
            "Job start requested; runtime cleanup and rule loading will run next.",
            directions=directions,
        )

    def continue_job(self, job_id: str | None = None) -> dict:
        return runtime_event(
            JobState.LOADING_RULES,
            "Continue requested; existing state will be preserved.",
            job_id=job_id,
        )

    def stop_job(self, job_id: str | None = None) -> dict:
        return runtime_event(
            JobState.PAUSED,
            "Pause requested; scheduler and worker should stop current activity.",
            job_id=job_id,
        )
