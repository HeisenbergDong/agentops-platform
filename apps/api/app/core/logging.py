from datetime import datetime, timezone


def runtime_event(stage: str, message: str, **extra) -> dict:
    return {
        "stage": stage,
        "message": message,
        "time": datetime.now(timezone.utc).isoformat(),
        "extra": extra,
    }
