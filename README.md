# AgentOps Platform

AgentOps Platform is a multi-role LLM automation platform for dispatching Windows workers that operate Trae CN, collect verifiable execution traces, review generated code, submit GitHub changes, and write Feishu Bitable records.

This repository is built toward the final architecture:

- Server owns orchestration, rules, role runtime, user configuration, data, Feishu, GitHub, logs, and attachments.
- Windows Worker owns Trae CN GUI control, screenshots, clipboard trace copying, local project scanning, and local build/test execution.
- Legacy `D:\adbz` scripts are reference material only. Their behavior should be migrated into first-class Server or Worker modules.

## Layout

```text
apps/api              FastAPI backend
apps/web              React web console
apps/worker-windows   Python Windows worker
rules                 Versioned role and workflow rules
packages/shared-schemas JSON schemas for contracts
packages/docs         Protocol and implementation notes
deploy                Deployment files
storage               Local development attachment storage
legacy-reference      Notes about legacy D:\adbz capabilities
```

## Local Development

```powershell
docker compose up -d postgres redis
cd apps/api
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\uvicorn app.main:app --reload
```

```powershell
cd apps/web
npm install
npm run dev
```

```powershell
cd apps/worker-windows
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m worker.main --once
```

## Server Deployment

The production target is `115.190.113.8`. The server side should be deployed with Docker Compose. PostgreSQL and Redis are expected to be private to the host or Docker network.

## Non-Negotiable Workflow Rules

- No verified full Trae assistant trace, no GitHub submission.
- No verified full Trae assistant trace, no Feishu business write.
- GitHub failure aborts Feishu business write.
- Feishu automation errors never consume business rows.
- Dissatisfied records must contain both product and process dissatisfaction sections.
- Secrets are never written to rule files, logs, prompts, or public attachments.
