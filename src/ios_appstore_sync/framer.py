"""Generic brand-framed App Store screenshot compositor using Pillow (Linux-friendly)."""

from __future__ import annotations

import html
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

DEFAULT_CANVAS_WIDTH = 1320
DEFAULT_CANVAS_HEIGHT = 2868

_CJK_FONT_CANDIDATES_BOLD = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

_CJK_FONT_CANDIDATES_REGULAR = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

_LATIN_FONT_CANDIDATES_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
)

_LATIN_FONT_CANDIDATES_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
)


class FramingError(RuntimeError):
    """Raised when screenshot framing configuration or input files are invalid."""


@dataclass(frozen=True)
class ThemeFonts:
    headline: str | None = None
    subheadline: str | None = None
    cjk_headline: str | None = None
    cjk_subheadline: str | None = None


@dataclass(frozen=True)
class ScreenshotTheme:
    background_color: str = "#F7F3EE"
    gradient_bottom_color: str | None = None
    accent_color: str = "#C46D5E"
    headline_color: str = "#1F1D1C"
    subheadline_color: str = "#5A5350"
    bezel_color: str = "#1C1C1E"
    bezel_stroke_color: str = "#3A3A3C"
    corner_radius: int = 120
    bezel_width: int = 28
    shadow_enabled: bool = True
    fonts: ThemeFonts = field(default_factory=ThemeFonts)


@dataclass(frozen=True)
class WebExportConfig:
    enabled: bool = False
    width: int = 660


@dataclass(frozen=True)
class ScreenshotItemConfig:
    raw: str
    output: str
    headline: str
    subheadline: str = ""


@dataclass(frozen=True)
class ScreenshotsConfig:
    schema_version: int
    display_type: str
    canvas_width: int
    canvas_height: int
    theme: ScreenshotTheme
    web_export: WebExportConfig
    locales: dict[str, tuple[ScreenshotItemConfig, ...]]


@dataclass(frozen=True)
class RenderedScreenshot:
    locale: str
    output_path: Path
    web_path: Path | None
    width: int
    height: int
    mode: str
    headline: str
    subheadline: str
    size_bytes: int


@dataclass(frozen=True)
class FrameResult:
    screenshots: tuple[RenderedScreenshot, ...]
    contact_sheet_png: Path
    contact_sheet_html: Path
    web_export_dir: Path | None


def load_screenshots_config(path: str | Path) -> ScreenshotsConfig:
    """Parse and validate a declarative screenshots.json configuration."""
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FramingError(f"Failed to read screenshots config '{config_path}': {exc}") from exc

    if not isinstance(raw, dict):
        raise FramingError("screenshots.json must be a top-level JSON object.")

    schema_version = raw.get("schemaVersion", 1)
    if isinstance(schema_version, bool) or schema_version != 1:
        raise FramingError(f"Unsupported screenshots.json schemaVersion: {schema_version!r} (expected 1).")

    canvas = raw.get("canvas") or {}
    width = int(canvas.get("width", DEFAULT_CANVAS_WIDTH))
    height = int(canvas.get("height", DEFAULT_CANVAS_HEIGHT))
    if width < 320 or height < 320:
        raise FramingError(f"Invalid canvas size {width}x{height}; dimensions must be >= 320px.")

    raw_theme = raw.get("theme") or {}
    raw_fonts = raw_theme.get("fonts") or {}
    fonts = ThemeFonts(
        headline=raw_fonts.get("headline") or None,
        subheadline=raw_fonts.get("subheadline") or None,
        cjk_headline=raw_fonts.get("cjkHeadline") or None,
        cjk_subheadline=raw_fonts.get("cjkSubheadline") or None,
    )
    theme = ScreenshotTheme(
        background_color=str(raw_theme.get("backgroundColor", "#F7F3EE")),
        gradient_bottom_color=raw_theme.get("gradientBottomColor") or None,
        accent_color=str(raw_theme.get("accentColor", "#C46D5E")),
        headline_color=str(raw_theme.get("headlineColor", "#1F1D1C")),
        subheadline_color=str(raw_theme.get("subheadlineColor", "#5A5350")),
        bezel_color=str(raw_theme.get("bezelColor", "#1C1C1E")),
        bezel_stroke_color=str(raw_theme.get("bezelStrokeColor", "#3A3A3C")),
        corner_radius=int(raw_theme.get("cornerRadius", 120)),
        bezel_width=int(raw_theme.get("bezelWidth", 28)),
        shadow_enabled=bool(raw_theme.get("shadowEnabled", True)),
        fonts=fonts,
    )

    raw_web = raw.get("webExport") or {}
    web_export = WebExportConfig(
        enabled=bool(raw_web.get("enabled", False)),
        width=int(raw_web.get("width", 660)),
    )

    raw_locales = raw.get("locales")
    if not isinstance(raw_locales, dict) or not raw_locales:
        raise FramingError("screenshots.json must define a non-empty 'locales' mapping.")

    locales: dict[str, tuple[ScreenshotItemConfig, ...]] = {}
    for locale, entries in raw_locales.items():
        if not isinstance(entries, list) or not entries:
            raise FramingError(f"screenshots.json locale '{locale}' must be a non-empty list.")
        parsed_items: list[ScreenshotItemConfig] = []
        for idx, item in enumerate(entries):
            if not isinstance(item, dict):
                raise FramingError(f"screenshots.json locales.{locale}[{idx}] must be an object.")
            raw_file = str(item.get("raw", "")).strip()
            if not raw_file:
                raise FramingError(f"screenshots.json locales.{locale}[{idx}].raw is required.")
            out_file = str(item.get("output") or Path(raw_file).name).strip()
            headline = str(item.get("headline", "")).strip()
            subheadline = str(item.get("subheadline", "")).strip()
            parsed_items.append(
                ScreenshotItemConfig(
                    raw=raw_file,
                    output=out_file,
                    headline=headline,
                    subheadline=subheadline,
                )
            )
        locales[str(locale)] = tuple(parsed_items)

    return ScreenshotsConfig(
        schema_version=schema_version,
        display_type=str(raw.get("displayType", "APP_IPHONE_69")),
        canvas_width=width,
        canvas_height=height,
        theme=theme,
        web_export=web_export,
        locales=locales,
    )


def _contains_cjk(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if (
            0x3000 <= code <= 0x303F
            or 0x3040 <= code <= 0x30FF
            or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF
            or 0xFF00 <= code <= 0xFFEF
        ):
            return True
    return False


def _is_cjk_locale(locale: str) -> bool:
    lower = locale.lower()
    return lower.startswith(("zh", "ja", "ko"))


def _preferred_ttc_tag(locale: str) -> str:
    lower = locale.lower()
    if "hant" in lower or "tw" in lower:
        return "tc"
    if "hk" in lower:
        return "hk"
    if "hans" in lower or "cn" in lower:
        return "sc"
    if lower.startswith("ko"):
        return "kr"
    if lower.startswith("ja"):
        return "jp"
    return "tc"


def _load_ttc_for_locale(font_path: str, size: int, locale: str) -> ImageFont.FreeTypeFont:
    tag = _preferred_ttc_tag(locale)
    best_font: ImageFont.FreeTypeFont | None = None
    for index in range(8):
        try:
            candidate = ImageFont.truetype(font_path, size=size, index=index)
        except OSError:
            break
        if best_font is None:
            best_font = candidate
        family, _style = candidate.getname()
        family_tokens = family.lower().split()
        if tag in family_tokens and "mono" not in family_tokens:
            return candidate
    if best_font is not None:
        return best_font
    return ImageFont.truetype(font_path, size=size)


def _fc_match_font(pattern: str) -> str | None:
    if not shutil.which("fc-match"):
        return None
    try:
        proc = subprocess.run(
            ["fc-match", "-f", "%{file}", pattern],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except OSError:
        return None
    path = proc.stdout.strip()
    if proc.returncode == 0 and path and Path(path).is_file():
        return path
    return None


def resolve_font(
    *,
    locale: str,
    text: str,
    size: int,
    bold: bool,
    fonts: ThemeFonts,
    config_dir: Path | None = None,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Resolve a crisp TrueType/OpenType font with automatic CJK fallback on Linux."""
    needs_cjk = _contains_cjk(text) or _is_cjk_locale(locale)

    configured: list[str] = []
    if needs_cjk:
        primary = fonts.cjk_headline if bold else fonts.cjk_subheadline
        secondary = fonts.headline if bold else fonts.subheadline
        for item in (primary, secondary):
            if item:
                configured.append(item)
    else:
        primary = fonts.headline if bold else fonts.subheadline
        if primary:
            configured.append(primary)

    candidates: list[str] = []
    for item in configured:
        p = Path(item)
        if not p.is_absolute() and config_dir is not None:
            p = config_dir / p
        candidates.append(str(p))

    if needs_cjk:
        candidates.extend(_CJK_FONT_CANDIDATES_BOLD if bold else _CJK_FONT_CANDIDATES_REGULAR)
        fc_pattern = f":lang={locale}:weight={'bold' if bold else 'regular'}"
        matched = _fc_match_font(fc_pattern)
        if matched:
            candidates.append(matched)
    candidates.extend(_LATIN_FONT_CANDIDATES_BOLD if bold else _LATIN_FONT_CANDIDATES_REGULAR)

    for candidate in candidates:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            if path.suffix.lower() == ".ttc":
                return _load_ttc_for_locale(str(path), size, locale)
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue

    return ImageFont.load_default()


def _safe_child_path(base_dir: Path, relative_path: str) -> Path:
    base_resolved = base_dir.resolve()
    target = (base_resolved / relative_path).resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError as exc:
        raise FramingError(
            f"Path '{relative_path}' escapes base directory '{base_resolved}'."
        ) from exc
    return target


def _make_background(width: int, height: int, theme: ScreenshotTheme) -> Image.Image:
    top_rgb = ImageColor.getrgb(theme.background_color)
    if not theme.gradient_bottom_color:
        return Image.new("RGB", (width, height), top_rgb)

    bottom_rgb = ImageColor.getrgb(theme.gradient_bottom_color)
    strip = Image.new("RGB", (1, height))
    pixels = strip.load()
    denom = max(height - 1, 1)
    for y in range(height):
        t = y / denom
        r = int(round(top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * t))
        g = int(round(top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * t))
        b = int(round(top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * t))
        pixels[0, y] = (r, g, b)
    return strip.resize((width, height), Image.Resampling.BILINEAR)


def _measure_width(draw: ImageDraw.ImageDraw, text: str, font: Any) -> float:
    bbox = draw.textbbox((0, 0), text, font=font)
    return float(bbox[2] - bbox[0])


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3000 <= code <= 0x303F
        or 0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
    )


def _tokenize_mixed_paragraph(para: str) -> list[tuple[str, bool]]:
    """Split text into (token, preceded_by_space) pieces where CJK chars wrap individually."""
    tokens: list[tuple[str, bool]] = []
    buf: list[str] = []
    pending_space = False

    def flush_buf() -> None:
        nonlocal pending_space
        if buf:
            tokens.append(("".join(buf), pending_space))
            buf.clear()
            pending_space = False

    for ch in para:
        if ch.isspace():
            flush_buf()
            pending_space = True
        elif _is_cjk_char(ch):
            flush_buf()
            tokens.append((ch, pending_space))
            pending_space = False
        else:
            buf.append(ch)
    flush_buf()
    return tokens


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int) -> list[str]:
    """Wrap Latin, CJK, and mixed CJK/Latin text cleanly within max_width pixels."""
    lines: list[str] = []
    for paragraph in text.splitlines():
        para = paragraph.strip()
        if not para:
            continue
        if _contains_cjk(para):
            current = ""
            for token, needs_space in _tokenize_mixed_paragraph(para):
                sep = " " if (current and needs_space) else ""
                trial = f"{current}{sep}{token}"
                if current and _measure_width(draw, trial, font) > max_width:
                    lines.append(current)
                    current = token
                else:
                    current = trial
            if current:
                lines.append(current)
        else:
            words = para.split()
            current = ""
            for word in words:
                trial = f"{current} {word}".strip()
                if current and _measure_width(draw, trial, font) > max_width:
                    lines.append(current)
                    current = word
                else:
                    current = trial
            if current:
                lines.append(current)
    return lines


def _fit_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize and center-crop image to cover target dimensions cleanly."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(int(round(src_w * scale)), target_w)
    new_h = max(int(round(src_h * scale)), target_h)
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def render_single_screenshot(
    *,
    raw_image_path: Path,
    locale: str,
    headline: str,
    subheadline: str,
    config: ScreenshotsConfig,
    config_dir: Path | None = None,
) -> Image.Image:
    """Composite a single raw simulator screenshot into an opaque RGB brand-framed canvas."""
    width = config.canvas_width
    height = config.canvas_height
    theme = config.theme

    canvas = _make_background(width, height, theme).convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    margin_x = int(round(width * 0.08))
    max_text_width = width - 2 * margin_x

    # Top decorative accent bar
    accent_w = int(round(width * 0.09))
    accent_h = max(8, int(round(height * 0.004)))
    accent_y = int(round(height * 0.055))
    accent_x0 = (width - accent_w) // 2
    draw.rounded_rectangle(
        [accent_x0, accent_y, accent_x0 + accent_w, accent_y + accent_h],
        radius=accent_h // 2,
        fill=ImageColor.getrgb(theme.accent_color) + (255,),
    )

    cursor_y = accent_y + accent_h + int(round(height * 0.025))

    # Headline typography
    headline_size = max(44, int(round(width * 0.068)))
    headline_font = resolve_font(
        locale=locale,
        text=headline,
        size=headline_size,
        bold=True,
        fonts=theme.fonts,
        config_dir=config_dir,
    )
    headline_fill = ImageColor.getrgb(theme.headline_color) + (255,)
    for line in _wrap_text(draw, headline, headline_font, max_text_width)[:3]:
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        line_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]
        draw.text(((width - line_w) // 2, cursor_y), line, font=headline_font, fill=headline_fill)
        cursor_y += max(line_h, headline_size) + int(round(headline_size * 0.22))

    # Subheadline typography
    if subheadline:
        cursor_y += int(round(height * 0.008))
        sub_size = max(30, int(round(width * 0.036)))
        sub_font = resolve_font(
            locale=locale,
            text=subheadline,
            size=sub_size,
            bold=False,
            fonts=theme.fonts,
            config_dir=config_dir,
        )
        sub_fill = ImageColor.getrgb(theme.subheadline_color) + (255,)
        for line in _wrap_text(draw, subheadline, sub_font, max_text_width)[:3]:
            bbox = draw.textbbox((0, 0), line, font=sub_font)
            line_w = bbox[2] - bbox[0]
            line_h = bbox[3] - bbox[1]
            draw.text(((width - line_w) // 2, cursor_y), line, font=sub_font, fill=sub_fill)
            cursor_y += max(line_h, sub_size) + int(round(sub_size * 0.25))

    # Device frame placement
    with Image.open(raw_image_path) as raw_img:
        src = raw_img.convert("RGB")
        src_aspect = src.width / max(src.height, 1)

        bezel_w = max(8, theme.bezel_width)
        avail_top = max(cursor_y + int(round(height * 0.03)), int(round(height * 0.24)))
        bottom_margin = int(round(height * 0.045))
        max_frame_w = int(round(width * 0.82))
        max_frame_h = max(height - avail_top - bottom_margin, int(round(height * 0.45)))

        inner_w = max_frame_w - 2 * bezel_w
        inner_h = int(round(inner_w / src_aspect))
        if inner_h + 2 * bezel_w > max_frame_h:
            inner_h = max_frame_h - 2 * bezel_w
            inner_w = int(round(inner_h * src_aspect))

        frame_w = inner_w + 2 * bezel_w
        frame_h = inner_h + 2 * bezel_w
        frame_x = (width - frame_w) // 2
        frame_y = avail_top + max(0, (max_frame_h - frame_h) // 2)

        outer_radius = max(theme.corner_radius, bezel_w + 16)
        inner_radius = max(outer_radius - bezel_w, 12)

        # Drop shadow
        if theme.shadow_enabled:
            shadow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            shadow_draw = ImageDraw.Draw(shadow_layer)
            shadow_offset_y = int(round(height * 0.012))
            shadow_draw.rounded_rectangle(
                [
                    frame_x,
                    frame_y + shadow_offset_y,
                    frame_x + frame_w,
                    frame_y + frame_h + shadow_offset_y,
                ],
                radius=outer_radius,
                fill=(0, 0, 0, 75),
            )
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=32))
            canvas = Image.alpha_composite(canvas, shadow_layer)
            draw = ImageDraw.Draw(canvas)

        # Device outer bezel
        draw.rounded_rectangle(
            [frame_x, frame_y, frame_x + frame_w, frame_y + frame_h],
            radius=outer_radius,
            fill=ImageColor.getrgb(theme.bezel_color) + (255,),
            outline=ImageColor.getrgb(theme.bezel_stroke_color) + (255,),
            width=4,
        )

        # Rounded raw screenshot inside bezel
        screen_img = _fit_cover(src, inner_w, inner_h)
        mask = Image.new("L", (inner_w, inner_h), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle(
            [0, 0, inner_w - 1, inner_h - 1],
            radius=inner_radius,
            fill=255,
        )
        canvas.paste(screen_img, (frame_x + bezel_w, frame_y + bezel_w), mask)

    # Guarantee opaque RGB output (zero alpha channel for App Store Connect)
    opaque = Image.new("RGB", (width, height), ImageColor.getrgb(theme.background_color))
    opaque.paste(canvas.convert("RGB"))
    return opaque


def _build_contact_sheet_png(
    rendered: list[RenderedScreenshot],
    output_path: Path,
    fonts: ThemeFonts,
    config_dir: Path | None,
) -> None:
    by_locale: dict[str, list[RenderedScreenshot]] = {}
    for item in rendered:
        by_locale.setdefault(item.locale, []).append(item)

    thumb_w = 240
    thumb_h = 520
    pad = 24
    header_h = 44
    caption_h = 36

    max_cols = max((len(items) for items in by_locale.values()), default=1)
    num_rows = max(len(by_locale), 1)

    sheet_w = pad + max_cols * (thumb_w + pad)
    sheet_h = pad + num_rows * (header_h + thumb_h + caption_h + pad)

    sheet = Image.new("RGB", (sheet_w, sheet_h), (245, 245, 247))
    draw = ImageDraw.Draw(sheet)

    y = pad
    for locale, items in by_locale.items():
        header_font = resolve_font(
            locale=locale,
            text=locale,
            size=24,
            bold=True,
            fonts=fonts,
            config_dir=config_dir,
        )
        caption_font = resolve_font(
            locale="en-US",
            text="01.png",
            size=16,
            bold=False,
            fonts=fonts,
            config_dir=config_dir,
        )
        draw.text((pad, y + 8), f"{locale} ({len(items)} screenshot(s))", font=header_font, fill=(28, 28, 30))
        row_top = y + header_h

        for col_idx, item in enumerate(items):
            x = pad + col_idx * (thumb_w + pad)
            with Image.open(item.output_path) as full_img:
                thumb = full_img.convert("RGB")
                thumb.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                paste_x = x + (thumb_w - thumb.width) // 2
                paste_y = row_top + (thumb_h - thumb.height) // 2
                sheet.paste(thumb, (paste_x, paste_y))
                draw.rectangle(
                    [paste_x, paste_y, paste_x + thumb.width - 1, paste_y + thumb.height - 1],
                    outline=(200, 200, 205),
                    width=1,
                )
            label = f"{item.output_path.name} ({item.width}x{item.height})"
            draw.text((x, row_top + thumb_h + 6), label, font=caption_font, fill=(90, 90, 96))

        y = row_top + thumb_h + caption_h + pad

    sheet.save(output_path, format="PNG", optimize=True)


def _build_contact_sheet_html(
    rendered: list[RenderedScreenshot],
    output_dir: Path,
    html_path: Path,
) -> None:
    by_locale: dict[str, list[RenderedScreenshot]] = {}
    for item in rendered:
        by_locale.setdefault(item.locale, []).append(item)

    sections: list[str] = []
    for locale, items in by_locale.items():
        cards: list[str] = []
        for item in items:
            rel_src = item.output_path.relative_to(output_dir).as_posix()
            kb = item.size_bytes / 1024.0
            cards.append(
                "<figure class='card'>"
                f"<img src='{html.escape(rel_src)}' alt='{html.escape(item.headline)}' loading='lazy'>"
                "<figcaption>"
                f"<div class='file'>{html.escape(item.output_path.name)}</div>"
                f"<div class='meta'>{item.width}×{item.height} · {html.escape(item.mode)} · {kb:.1f} KB</div>"
                f"<div class='headline'>{html.escape(item.headline)}</div>"
                f"<div class='sub'>{html.escape(item.subheadline)}</div>"
                "</figcaption></figure>"
            )
        sections.append(
            f"<section><h2>{html.escape(locale)}</h2><div class='grid'>{''.join(cards)}</div></section>"
        )

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>App Store Screenshot Contact Sheet</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 24px;
    background: #f5f5f7;
    color: #1d1d1f;
  }}
  h1 {{ margin-bottom: 4px; }}
  p.lead {{ color: #6e6e73; margin-top: 0; }}
  h2 {{ margin-top: 32px; border-bottom: 1px solid #d2d2d7; padding-bottom: 6px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 18px; }}
  .card {{ margin: 0; background: #fff; border-radius: 12px; padding: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
  .card img {{ width: 100%; height: auto; border-radius: 8px; display: block; }}
  figcaption {{ margin-top: 10px; font-size: 13px; }}
  .file {{ font-weight: 600; }}
  .meta {{ color: #6e6e73; font-size: 12px; margin-bottom: 6px; }}
  .headline {{ font-weight: 500; }}
  .sub {{ color: #515154; font-size: 12px; margin-top: 2px; }}
</style>
</head>
<body>
  <h1>App Store Screenshot Contact Sheet</h1>
  <p class="lead">
    Review framed opaque RGB outputs before running
    <code>sync --sync-screenshots --dry-run false</code>.
  </p>
  {''.join(sections)}
</body>
</html>
"""
    html_path.write_text(doc, encoding="utf-8")


def frame_screenshots(
    *,
    config_path: str | Path,
    raw_dir: str | Path,
    output_dir: str | Path,
) -> FrameResult:
    """Render all configured screenshots, contact sheets, and optional web exports."""
    cfg_path = Path(config_path).resolve()
    config = load_screenshots_config(cfg_path)
    raw_root = Path(raw_dir).resolve()
    out_root = Path(output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    web_root: Path | None = None
    if config.web_export.enabled:
        web_root = out_root / "web-screenshots"
        web_root.mkdir(parents=True, exist_ok=True)

    rendered: list[RenderedScreenshot] = []

    for locale, items in config.locales.items():
        locale_out_dir = _safe_child_path(out_root, locale)
        locale_out_dir.mkdir(parents=True, exist_ok=True)

        for item in items:
            # Look first in raw_dir/<locale>/<raw>, then fallback to raw_dir/<raw>
            locale_candidate = _safe_child_path(raw_root, f"{locale}/{item.raw}")
            flat_candidate = _safe_child_path(raw_root, item.raw)
            if locale_candidate.is_file():
                raw_path = locale_candidate
            elif flat_candidate.is_file():
                raw_path = flat_candidate
            else:
                raise FramingError(
                    f"Raw screenshot '{item.raw}' for locale '{locale}' not found at "
                    f"'{locale_candidate}' or '{flat_candidate}'."
                )

            out_path = _safe_child_path(locale_out_dir, item.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)

            framed_img = render_single_screenshot(
                raw_image_path=raw_path,
                locale=locale,
                headline=item.headline,
                subheadline=item.subheadline,
                config=config,
                config_dir=cfg_path.parent,
            )
            framed_img.save(out_path, format="PNG", optimize=True)

            web_path: Path | None = None
            if web_root is not None:
                web_locale_dir = _safe_child_path(web_root, locale)
                web_locale_dir.mkdir(parents=True, exist_ok=True)
                web_path = _safe_child_path(web_locale_dir, item.output)
                web_w = max(120, config.web_export.width)
                web_h = int(round(framed_img.height * (web_w / framed_img.width)))
                web_img = framed_img.resize((web_w, web_h), Image.Resampling.LANCZOS).convert("RGB")
                web_img.save(web_path, format="PNG", optimize=True)

            rendered.append(
                RenderedScreenshot(
                    locale=locale,
                    output_path=out_path,
                    web_path=web_path,
                    width=framed_img.width,
                    height=framed_img.height,
                    mode=framed_img.mode,
                    headline=item.headline,
                    subheadline=item.subheadline,
                    size_bytes=out_path.stat().st_size,
                )
            )

    contact_png = out_root / "contact-sheet.png"
    contact_html = out_root / "contact-sheet.html"
    _build_contact_sheet_png(rendered, contact_png, config.theme.fonts, cfg_path.parent)
    _build_contact_sheet_html(rendered, out_root, contact_html)

    return FrameResult(
        screenshots=tuple(rendered),
        contact_sheet_png=contact_png,
        contact_sheet_html=contact_html,
        web_export_dir=web_root,
    )
