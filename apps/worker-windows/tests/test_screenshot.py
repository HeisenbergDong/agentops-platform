from pathlib import Path

from PIL import Image, ImageDraw

from worker.trae.screenshot import assess_screenshot_quality


def test_assess_screenshot_quality_rejects_too_small_image(tmp_path: Path):
    path = tmp_path / "small.png"
    Image.new("RGB", (120, 80), "white").save(path)

    result = assess_screenshot_quality(path, {"target": "trae_window"})

    assert result["ok"] is False
    assert result["reason"] == "image_too_small"


def test_assess_screenshot_quality_rejects_blank_image(tmp_path: Path):
    path = tmp_path / "blank.png"
    Image.new("RGB", (900, 700), "white").save(path)

    result = assess_screenshot_quality(path, {"target": "trae_window", "window_title": "Trae CN", "bounds": {"width": 900, "height": 700}})

    assert result["ok"] is False
    assert result["reason"] == "mostly_blank"


def test_assess_screenshot_quality_accepts_detailed_trae_window(tmp_path: Path):
    path = tmp_path / "trae.png"
    image = Image.new("RGB", (900, 700), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 220, 700), fill=(15, 28, 42))
    for y in range(80, 650, 28):
        draw.rectangle((260, y, 820, y + 10), fill=(80 + (y % 90), 95, 120))
    for x in range(260, 850, 60):
        draw.line((x, 80, x, 650), fill=(20, 20, 20))
    image.save(path)

    result = assess_screenshot_quality(
        path,
        {"target": "trae_window", "window_title": "Trae CN", "bounds": {"width": 900, "height": 700}},
    )

    assert result["ok"] is True
    assert result["reason"] == "ok"
