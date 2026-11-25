"""Unit tests for brand-framed screenshot compositor (framer.py)."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
import pytest

from ios_appstore_sync.cli import main as cli_main
from ios_appstore_sync.framer import (
    DEFAULT_CANVAS_HEIGHT,
    DEFAULT_CANVAS_WIDTH,
    FramingError,
    frame_screenshots,
)


def _create_synthetic_raw_png(path: Path, color: tuple[int, int, int, int]) -> None:
    """Create an RGBA simulator screenshot to verify alpha stripping -> opaque RGB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (1320, 2868), color)
    img.save(path, format="PNG")


def _write_screenshots_config(path: Path, *, web_export: bool = True) -> None:
    cfg = {
        "schemaVersion": 1,
        "displayType": "APP_IPHONE_69",
        "canvas": {"width": DEFAULT_CANVAS_WIDTH, "height": DEFAULT_CANVAS_HEIGHT},
        "theme": {
            "backgroundColor": "#F7F3EE",
            "gradientBottomColor": "#EBE0D2",
            "accentColor": "#C46D5E",
            "headlineColor": "#231F20",
            "subheadlineColor": "#5E5654",
            "bezelColor": "#1C1C1E",
            "bezelStrokeColor": "#3A3A3C",
            "cornerRadius": 120,
            "bezelWidth": 28,
            "shadowEnabled": True,
        },
        "webExport": {"enabled": web_export, "width": 660},
        "locales": {
            "en-US": [
                {
                    "raw": "01-home.png",
                    "output": "01-home.png",
                    "headline": "Your day, in calm focus",
                    "subheadline": "Private notes and daily rhythm, entirely on device.",
                },
                {
                    "raw": "02-capture.png",
                    "output": "02-capture.png",
                    "headline": "Capture ideas fast",
                    "subheadline": "Zero accounts, zero trackers.",
                },
            ],
            "zh-Hant": [
                {
                    "raw": "01-home.png",
                    "output": "01-home.png",
                    "headline": "專注每一天的節奏",
                    "subheadline": "完全離線保存，安心記錄你的日常靈感。",
                },
                {
                    "raw": "02-capture.png",
                    "output": "02-capture.png",
                    "headline": "數秒內捕捉想法",
                    "subheadline": "免帳號、零追蹤，隨時快速整理筆記。",
                },
            ],
        },
    }
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def test_frame_generates_opaque_rgb_1320x2868_for_en_and_zh_hant(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    cfg_path = tmp_path / "screenshots.json"

    _create_synthetic_raw_png(raw_dir / "01-home.png", (40, 120, 180, 200))
    _create_synthetic_raw_png(raw_dir / "02-capture.png", (180, 90, 60, 255))
    _write_screenshots_config(cfg_path, web_export=True)

    result = frame_screenshots(config_path=cfg_path, raw_dir=raw_dir, output_dir=out_dir)

    assert len(result.screenshots) == 4
    for shot in result.screenshots:
        assert shot.output_path.is_file()
        assert shot.width == 1320
        assert shot.height == 2868
        assert shot.mode == "RGB"
        with Image.open(shot.output_path) as img:
            assert img.size == (1320, 2868)
            assert img.mode == "RGB"
            assert "A" not in img.getbands()

        # Verify web export
        assert shot.web_path is not None and shot.web_path.is_file()
        with Image.open(shot.web_path) as web_img:
            assert web_img.width == 660
            assert web_img.mode == "RGB"

    # Verify contact sheets
    assert result.contact_sheet_png.is_file()
    with Image.open(result.contact_sheet_png) as sheet_img:
        assert sheet_img.mode == "RGB"
        assert sheet_img.width > 400
        assert sheet_img.height > 400

    assert result.contact_sheet_html.is_file()
    html_text = result.contact_sheet_html.read_text(encoding="utf-8")
    assert "en-US" in html_text
    assert "zh-Hant" in html_text
    assert "專注每一天的節奏" in html_text
    assert "en-US/01-home.png" in html_text
    assert "zh-Hant/01-home.png" in html_text


def test_frame_cli_subcommand_and_path_traversal_guard(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    cfg_path = tmp_path / "screenshots.json"
    _create_synthetic_raw_png(raw_dir / "en-US" / "01-home.png", (60, 80, 100, 255))
    _create_synthetic_raw_png(raw_dir / "en-US" / "02-capture.png", (90, 110, 130, 255))
    _create_synthetic_raw_png(raw_dir / "zh-Hant" / "01-home.png", (70, 90, 110, 255))
    _create_synthetic_raw_png(raw_dir / "zh-Hant" / "02-capture.png", (100, 120, 140, 255))
    _write_screenshots_config(cfg_path, web_export=False)

    rc = cli_main(
        [
            "frame",
            "--config",
            str(cfg_path),
            "--raw-dir",
            str(raw_dir),
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    assert (out_dir / "en-US" / "01-home.png").is_file()
    assert (out_dir / "zh-Hant" / "01-home.png").is_file()
    assert not (out_dir / "web-screenshots").exists()

    # Path traversal in output filename must be rejected
    bad_cfg = tmp_path / "bad.json"
    bad_cfg.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "locales": {
                    "en-US": [{"raw": "01-home.png", "output": "../../escape.png", "headline": "Hi"}]
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(FramingError):
        frame_screenshots(config_path=bad_cfg, raw_dir=raw_dir, output_dir=out_dir)
