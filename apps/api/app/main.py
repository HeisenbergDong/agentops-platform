from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import attachments, auth, errors, jobs, roles, rules, settings, workers
from app.core.config import settings as app_settings


def create_app() -> FastAPI:
    app = FastAPI(title="AgentOps API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
    app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
    app.include_router(roles.router, prefix="/api/roles", tags=["roles"])
    app.include_router(rules.router, prefix="/api/rules", tags=["rules"])
    app.include_router(workers.router, prefix="/api/workers", tags=["workers"])
    app.include_router(attachments.router, prefix="/api/attachments", tags=["attachments"])
    app.include_router(errors.router, prefix="/api/errors", tags=["errors"])

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "service": "agentops-api"}

    return app


app = create_app()
