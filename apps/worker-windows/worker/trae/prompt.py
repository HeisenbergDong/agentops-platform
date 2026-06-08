def send_prompt(prompt: str) -> dict:
    return {
        "status": "pending",
        "chars": len(prompt),
        "message": "Prompt paste/send will be implemented with clipboard and UIA.",
    }
