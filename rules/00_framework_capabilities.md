# 00 Framework Capabilities

This file describes the current AgentOps framework so the Rule Collector can place new requirements into the correct rule files.

## Architecture

- AgentOps Server owns orchestration, rules, roles, persistence, real-time logs, GitHub, Feishu, attachments, and exception handling.
- Windows Worker owns local GUI control, Trae CN screenshots, clipboard trace copying, local project scanning, local build/test execution, and local browser acceptance.
- Trae CN is currently the only required controlled GUI application.
- Future workers may support browsers, IDEs, office apps, or other AI tools through the same worker command/result protocol.

## Core Roles

1. Orchestrator
2. Rule Collector
3. Prompt Writer
4. Product Reviewer
5. Dissatisfaction Writer
6. GitHub Submitter
7. Feishu Writer
8. Windows Worker Controller

## Hard Validation

The platform supports deterministic checks for trace validity, prompt forbidden words, GitHub URL shape, Feishu required fields, dissatisfaction section format, secret leakage, path safety, and worker command allowlists.

## Current Gaps

- LLM role execution is scaffolded but not implemented.
- Rule collection from online documents is scaffolded but not implemented.
- Trae CN real GUI control is scaffolded in Worker but not implemented.
- Database persistence is scaffolded but models and migrations are not complete.
