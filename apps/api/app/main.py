from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import admin, attachments, auth, errors, jobs, roles, rules, settings, workers
from app.core.config import settings as app_settings
from app.db.bootstrap import bootstrap_database
from app.db.session import SessionLocal


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
    app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
    app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
    app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
    app.include_router(roles.router, prefix="/api/roles", tags=["roles"])
    app.include_router(rules.router, prefix="/api/rules", tags=["rules"])
    app.include_router(workers.router, prefix="/api/workers", tags=["workers"])
    app.include_router(attachments.router, prefix="/api/attachments", tags=["attachments"])
    app.include_router(errors.router, prefix="/api/errors", tags=["errors"])

    @app.on_event("startup")
    def startup() -> None:
        bootstrap_database()

    @app.get("/api/health")
    def health() -> dict:
        db_ok = False
        try:
            with SessionLocal() as db:
                db.execute(text("select 1"))
            db_ok = True
        except Exception:
            db_ok = False
        return {"status": "ok", "service": "agentops-api", "database": db_ok}

    return app


app = create_app()
