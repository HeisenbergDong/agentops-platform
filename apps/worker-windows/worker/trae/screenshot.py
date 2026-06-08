from datetime import datetime
from pathlib import Path


def capture_screenshot() -> dict:
    try:
        import mss
        from PIL import Image

        out_dir = Path("screenshots")
        out_dir.mkdir(exist_ok=True)
        path = out_dir / f"worker-screen-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            image = sct.grab(monitor)
            img = Image.frombytes("RGB", image.size, image.rgb)
            img.save(path)
        return {"path": str(path), "status": "captured"}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}
