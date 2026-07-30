from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import unquote
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from PIL import ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    Image as PdfImage,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "index.html"
DEFAULT_OUTPUT = ROOT / "output" / "pdf" / "menu-1719-urban-bistrot.pdf"

BRAND_RED = colors.HexColor("#9D2D2D")
INK = colors.HexColor("#1C1917")
MUTED = colors.HexColor("#5F5752")
LINE = colors.HexColor("#DDD6D1")
PANEL = colors.HexColor("#F3F0ED")


def normalize_text(value: str) -> str:
    value = value.replace("\u2014", "-").replace("\u2013", "-").replace("\u2011", "-")
    return re.sub(r"\s+", " ", value).strip()


def plain_text(fragment: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", fragment)
    return normalize_text(html.unescape(without_tags))


def menu_data_from_index(index_path: Path) -> list[dict]:
    js = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const match = source.match(/const menuData\s*=\s*(\[[\s\S]*?\n    \]);/);
if (!match) throw new Error("menuData non trovato");
const menuData = Function(`"use strict"; return (${match[1]});`)();
process.stdout.write(JSON.stringify(menuData));
"""
    result = subprocess.run(
        ["node", "-e", js, str(index_path)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def aperitivo_from_index(source: str) -> dict:
    match = re.search(r'<section id="aperitivo">([\s\S]*?)</section>', source)
    if not match:
        raise ValueError("Sezione aperitivo non trovata")

    items = []
    for block in re.findall(
        r'<article class="card deal-card">([\s\S]*?)</article>', match.group(1)
    ):
        name_match = re.search(r'<h4 class="deal-title">([\s\S]*?)</h4>', block)
        price_match = re.search(r'<span class="deal-price">([\s\S]*?)</span>', block)
        desc_match = re.search(r'<p class="deal-desc">([\s\S]*?)</p>', block)
        photo_match = re.search(r'<img[^>]+src="([^"]+)"', block)
        if not (name_match and price_match and desc_match and photo_match):
            raise ValueError("Formula aperitivo incompleta")
        items.append(
            {
                "name": plain_text(name_match.group(1)),
                "price": plain_text(price_match.group(1)),
                "desc": plain_text(desc_match.group(1)),
                "photo": html.unescape(photo_match.group(1)),
            }
        )

    return {
        "title": "Formule Aperitivo",
        "groups": [
            {
                "title": "Formule da condividere",
                "items": items,
                "note": "Chiedi al banco le proposte del giorno.",
            }
        ],
    }


def clean_heading(value: str) -> str:
    return normalize_text(re.sub(r"^[^A-Za-zÀ-ÿ0-9]+", "", value))


class ThumbnailStore:
    def __init__(self, directory: Path, size_px: int = 360):
        self.directory = directory
        self.size_px = size_px
        self.created: dict[str, Path] = {}
        self.missing: list[str] = []

    def get(self, reference: str) -> Path | None:
        reference = str(reference or "").strip()
        if not reference:
            return None
        if reference in self.created:
            return self.created[reference]

        relative_path = unquote(reference.split("?", 1)[0].lstrip("/"))
        source_path = ROOT / relative_path
        if not source_path.exists():
            self.missing.append(reference)
            return None

        digest = hashlib.sha1(reference.encode("utf-8")).hexdigest()[:16]
        output_path = self.directory / f"{digest}.jpg"
        with PILImage.open(source_path) as source:
            source = ImageOps.exif_transpose(source)
            square = ImageOps.fit(
                source.convert("RGB"),
                (self.size_px, self.size_px),
                method=PILImage.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            square.save(output_path, "JPEG", quality=84, optimize=True, progressive=True)

        self.created[reference] = output_path
        return output_path


def register_fonts() -> None:
    font_dir = Path("C:/Windows/Fonts")
    fonts = {
        "MenuSans": font_dir / "arial.ttf",
        "MenuSans-Bold": font_dir / "arialbd.ttf",
        "MenuSans-Italic": font_dir / "ariali.ttf",
    }
    for name, path in fonts.items():
        if not path.exists():
            raise FileNotFoundError(f"Font richiesto non trovato: {path}")
        pdfmetrics.registerFont(TTFont(name, str(path)))


def build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "MenuTitle",
            parent=base["Title"],
            fontName="MenuSans-Bold",
            fontSize=24,
            leading=27,
            textColor=INK,
            alignment=TA_LEFT,
            spaceAfter=3 * mm,
        ),
        "subtitle": ParagraphStyle(
            "MenuSubtitle",
            parent=base["BodyText"],
            fontName="MenuSans",
            fontSize=10.5,
            leading=13,
            textColor=MUTED,
            spaceAfter=6 * mm,
        ),
        "category": ParagraphStyle(
            "MenuCategory",
            parent=base["Heading1"],
            fontName="MenuSans-Bold",
            fontSize=16,
            leading=18,
            textColor=BRAND_RED,
            spaceBefore=4 * mm,
            spaceAfter=2.5 * mm,
            keepWithNext=True,
        ),
        "group": ParagraphStyle(
            "MenuGroup",
            parent=base["Heading2"],
            fontName="MenuSans-Bold",
            fontSize=12.5,
            leading=15,
            textColor=INK,
            backColor=PANEL,
            borderPadding=(4, 5, 4, 5),
            spaceBefore=2 * mm,
            spaceAfter=2 * mm,
            keepWithNext=True,
        ),
        "name": ParagraphStyle(
            "ItemName",
            parent=base["BodyText"],
            fontName="MenuSans-Bold",
            fontSize=10.8,
            leading=13,
            textColor=INK,
        ),
        "desc": ParagraphStyle(
            "ItemDescription",
            parent=base["BodyText"],
            fontName="MenuSans",
            fontSize=9.4,
            leading=11.6,
            textColor=MUTED,
            spaceBefore=1,
        ),
        "price": ParagraphStyle(
            "ItemPrice",
            parent=base["BodyText"],
            fontName="MenuSans-Bold",
            fontSize=10.2,
            leading=12,
            textColor=BRAND_RED,
            alignment=TA_RIGHT,
        ),
        "note": ParagraphStyle(
            "MenuNote",
            parent=base["BodyText"],
            fontName="MenuSans-Italic",
            fontSize=9.2,
            leading=11.5,
            textColor=MUTED,
            spaceBefore=2 * mm,
            spaceAfter=2 * mm,
        ),
        "footer": ParagraphStyle(
            "Footer",
            parent=base["BodyText"],
            fontName="MenuSans",
            fontSize=8,
            leading=9,
            textColor=MUTED,
            alignment=TA_CENTER,
        ),
    }


def item_flowable(
    item: dict,
    styles: dict[str, ParagraphStyle],
    frame_width: float,
    thumbnails: ThumbnailStore,
):
    name = normalize_text(str(item.get("name", "")))
    tag = normalize_text(str(item.get("tag", "")))
    if tag:
        name = f"{name} ({tag})"

    raw_desc = normalize_text(str(item.get("desc", "")))
    desc = "" if raw_desc.startswith("§§AUTO§§") else raw_desc
    price = normalize_text(str(item.get("price", "")))
    photo_path = thumbnails.get(str(item.get("photo", "")))

    if photo_path:
        photo_size = 19 * mm
        photo_column = 22 * mm
        price_width = 48
        text_width = frame_width - photo_column - price_width
        text_rows = [[Paragraph(escape(name), styles["name"])]]
        if desc:
            text_rows.append([Paragraph(escape(desc), styles["desc"])])
        text_table = Table(text_rows, colWidths=[text_width], hAlign="LEFT")
        text_table.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        table = Table(
            [
                [
                    PdfImage(str(photo_path), width=photo_size, height=photo_size),
                    text_table,
                    Paragraph(escape(price), styles["price"]),
                ]
            ],
            colWidths=[photo_column, text_width, price_width],
            hAlign="LEFT",
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (0, 0), 5),
                    ("RIGHTPADDING", (1, 0), (1, 0), 4),
                    ("RIGHTPADDING", (2, 0), (2, 0), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.45, LINE),
                ]
            )
        )
        return KeepTogether([table])

    price_width = 88
    name_width = frame_width - price_width
    rows = [[Paragraph(escape(name), styles["name"]), Paragraph(escape(price), styles["price"])]]
    if desc:
        rows.append([Paragraph(escape(desc), styles["desc"]), ""])

    table = Table(rows, colWidths=[name_width, price_width], hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), 4),
        ("RIGHTPADDING", (1, 0), (1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, 0), 2),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2 if not desc else 0.5),
        ("TOPPADDING", (0, 1), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 2.5),
        ("LINEBELOW", (0, -1), (-1, -1), 0.45, LINE),
    ]
    if desc:
        commands.append(("SPAN", (0, 1), (1, 1)))
    table.setStyle(TableStyle(commands))
    return KeepTogether([table])


def page_decor(canvas, doc) -> None:
    canvas.saveState()
    width, _ = A4
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.6)
    canvas.line(14 * mm, 11 * mm, width - 14 * mm, 11 * mm)
    canvas.setFillColor(MUTED)
    canvas.setFont("MenuSans", 8)
    canvas.drawString(14 * mm, 7 * mm, "Via di Santa Costanza 17/19, Roma")
    canvas.drawRightString(width - 14 * mm, 7 * mm, f"Pagina {doc.page}")
    canvas.restoreState()


def build_pdf(output_path: Path) -> tuple[int, int, int]:
    register_fonts()
    styles = build_styles()

    source = INDEX_PATH.read_text(encoding="utf-8")
    categories = [aperitivo_from_index(source), *menu_data_from_index(INDEX_PATH)]
    item_count = sum(
        len(group.get("items", []))
        for category in categories
        for group in category.get("groups", [])
    )
    photo_count = sum(
        1
        for category in categories
        for group in category.get("groups", [])
        for item in group.get("items", [])
        if item.get("photo")
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    page_width, page_height = A4
    left_margin = right_margin = 14 * mm
    top_margin = 18 * mm
    bottom_margin = 15 * mm
    gutter = 7 * mm
    usable_width = page_width - left_margin - right_margin
    frame_width = (usable_width - gutter) / 2
    frame_height = page_height - top_margin - bottom_margin

    frames = [
        Frame(
            left_margin,
            bottom_margin,
            frame_width,
            frame_height,
            id="left",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        ),
        Frame(
            left_margin + frame_width + gutter,
            bottom_margin,
            frame_width,
            frame_height,
            id="right",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        ),
    ]
    doc = BaseDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
        title="Menu completo - 17/19 Urban Bistrot",
        author="17/19 Urban Bistrot",
        subject="Menu completo aggiornato",
    )
    doc.addPageTemplates([PageTemplate(id="menu", frames=frames, onPageEnd=page_decor)])

    today = date.today()
    month_names = [
        "",
        "gennaio",
        "febbraio",
        "marzo",
        "aprile",
        "maggio",
        "giugno",
        "luglio",
        "agosto",
        "settembre",
        "ottobre",
        "novembre",
        "dicembre",
    ]
    updated = f"{today.day} {month_names[today.month]} {today.year}"

    with tempfile.TemporaryDirectory(prefix="1719-menu-thumbs-") as thumbnail_dir:
        thumbnails = ThumbnailStore(Path(thumbnail_dir))
        story = [
            Paragraph("Menu completo", styles["title"]),
            Paragraph(
                f"17/19 Urban Bistrot · Aggiornato il {escape(updated)} · "
                f"{item_count} proposte · {photo_count} foto",
                styles["subtitle"],
            ),
        ]

        for category in categories:
            story.append(CondPageBreak(40 * mm))
            story.append(
                Paragraph(escape(clean_heading(category["title"])), styles["category"])
            )
            for group in category.get("groups", []):
                story.append(
                    Paragraph(escape(clean_heading(group["title"])), styles["group"])
                )
                for item in group.get("items", []):
                    story.append(item_flowable(item, styles, frame_width, thumbnails))
                if group.get("note"):
                    story.append(Paragraph(escape(group["note"]), styles["note"]))
                story.append(Spacer(1, 1 * mm))

        if thumbnails.missing:
            raise FileNotFoundError(
                "Foto menu mancanti: " + ", ".join(sorted(set(thumbnails.missing)))
            )
        doc.build(story)
    return len(categories), item_count, photo_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera il menu PDF dal menu attivo del sito.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    categories, items, photos = build_pdf(args.output.resolve())
    print(
        f"Creato {args.output.resolve()} "
        f"({categories} categorie, {items} voci, {photos} foto)"
    )


if __name__ == "__main__":
    main()
