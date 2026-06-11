from datetime import datetime
import os
from pathlib import Path


def capture_screenshot() -> dict:
    import mss
    from PIL import Image

    out_dir = _screenshot_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"worker-screen-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        image = sct.grab(monitor)
        img = Image.frombytes("RGB", image.size, image.rgb)
        img.save(path)
    return {
        "path": str(path),
        "filename": path.name,
        "status": "captured",
        "content_type": "image/png",
        "size_bytes": path.stat().st_size,
    }


def _screenshot_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "AgentOps" / "screenshots"
    return Path.home() / ".agentops" / "screenshots"
