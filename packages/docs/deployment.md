# Deployment Notes

Server target: `115.190.113.8`.

Recommended:

- Docker Compose for PostgreSQL, Redis, API, Web, and reverse proxy.
- Bind database and Redis to localhost or private Docker network only.
- Store secrets in environment variables or encrypted database fields.
- Keep Windows Worker outside server deployment.
