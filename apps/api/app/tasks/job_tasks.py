from app.tasks.celery_app import celery_app


@celery_app.task(name="agentops.noop")
def noop() -> str:
    return "ok"
