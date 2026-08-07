from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont


# =========================
# Настройки
# =========================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CALL_TO_ACTION = (
    os.getenv("CALL_TO_ACTION", "Пиши в Direct / WhatsApp").strip()
    or "Пиши в Direct / WhatsApp"
)
STORE_LINK = os.getenv(
    "STORE_LINK",
    "https://l.bezlimit.ru/store/659787",
).strip()
OWNER_ID_ENV = os.getenv("OWNER_ID", "").strip()

AUTOPILOT_HOUR = int(os.getenv("AUTOPILOT_HOUR", "9"))
AUTOPILOT_MINUTE = int(os.getenv("AUTOPILOT_MINUTE", "0"))
TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Moscow"))

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

STATE_FILE = DATA_DIR / "autopilot_state.json"
OWNER_FILE = DATA_DIR / "owner_id.txt"

router = Router()

BUILD_VERSION = "v9-instagram-layout"

TARIFF_RE = re.compile(
    r"тариф\s*:\s*([\d\s]+)\s*(?:руб(?:\.|лей)?|₽)?\s*(?:/|в)?\s*мес",
    re.IGNORECASE,
)


# =========================
# Данные
# =========================

@dataclass(frozen=True)
class NumberItem:
    phone: str
    price: int
    beauty: int


DEFAULT_STATE = {
    "catalog": [],
    "used": [],
    "autopilot": True,
    "draft": [],
    "draft_mode": None,
    "draft_title": None,
    "draft_created_at": None,
    "last_auto_date": None,
}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        result = dict(DEFAULT_STATE)
        result.update(data)
        return result
    except Exception:
        logging.exception("Ошибка чтения state")
        return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_catalog(rows: list[tuple[str, int]]) -> None:
    state = load_state()
    state["catalog"] = [{"phone": p, "price": price} for p, price in rows]
    state["used"] = []
    state["draft"] = []
    state["draft_mode"] = None
    state["draft_title"] = None
    save_state(state)


def get_catalog(state: dict) -> list[tuple[str, int]]:
    return [
        (str(x["phone"]), int(x["price"]))
        for x in state.get("catalog", [])
    ]


# =========================
# Парсер
# =========================

def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = digits[1:]
    if len(digits) != 10 or not digits.startswith("9"):
        return None
    return digits


def format_phone(number: str) -> str:
    return f"+7 ({number[:3]}) {number[3:6]}-{number[6:8]}-{number[8:]}"


def parse_catalog(text: str) -> list[tuple[str, int]]:
    current_price: int | None = None
    rows: list[tuple[str, int]] = []

    for original in text.splitlines():
        line = original.strip()
        if not line:
            continue

        m = TARIFF_RE.search(line)
        if m:
            current_price = int(re.sub(r"\D", "", m.group(1)))
            continue

        phone = normalize_phone(line)
        if phone and current_price is not None:
            row = (phone, current_price)
            if row not in rows:
                rows.append(row)

    if not rows:
        raise ValueError(
            "Не удалось найти номера. Формат: «Тариф: 950 руб/мес», "
            "ниже номера по одному в строке."
        )

    return rows


# =========================
# Красота номера
# =========================

def beauty_score(phone: str) -> int:
    s = phone[3:]
    score = 0

    runs = re.findall(r"((\d)\2+)", s)
    for run, _ in runs:
        n = len(run)
        score += (n - 1) * 14 + max(0, n - 2) * 12

    counts = Counter(s)
    for count in counts.values():
        if count >= 3:
            score += (count - 2) * 8

    last4 = s[-4:]
    last6 = s[-6:]

    if len(set(last4)) == 1:
        score += 70
    if last4[:2] == last4[2:]:
        score += 50
    if s[-2:] == s[-4:-2]:
        score += 38
    if last6[:3] == last6[3:]:
        score += 55
    if last4 == last4[::-1]:
        score += 30
    if s[-5:] == s[-5:][::-1]:
        score += 35

    if s.endswith("0000"):
        score += 75
    elif s.endswith("000"):
        score += 38
    elif s.endswith("00"):
        score += 15

    if s == s[::-1]:
        score += 85

    return score


def sales_rank(phone: str, price: int) -> int:
    # Небольшой бонус недорогим тарифам
    return beauty_score(phone) + max(0, 12 - price // 500)


def build_items(catalog: list[tuple[str, int]]) -> list[NumberItem]:
    return [
        NumberItem(phone=p, price=price, beauty=sales_rank(p, price))
        for p, price in catalog
    ]


def select_mode(
    catalog: list[tuple[str, int]],
    mode: str,
    used: set[str],
    limit: int = 5,
) -> list[NumberItem]:
    items = build_items(catalog)

    if mode == "budget":
        items = [x for x in items if x.price <= 1000]
    elif mode == "premium":
        items = [x for x in items if x.price >= 2000]
    elif mode == "day":
        limit = 1

    available = [x for x in items if x.phone not in used]
    if not available:
        available = items

    available.sort(key=lambda x: (x.beauty, -x.price), reverse=True)

    if mode == "day":
        return available[:1]

    picked: list[NumberItem] = []
    per_price: Counter[int] = Counter()

    for item in available:
        if per_price[item.price] >= 2:
            continue
        picked.append(item)
        per_price[item.price] += 1
        if len(picked) >= limit:
            break

    if len(picked) < limit:
        have = {x.phone for x in picked}
        for item in available:
            if item.phone in have:
                continue
            picked.append(item)
            if len(picked) >= limit:
                break

    return picked


# =========================
# Дизайн — явный “первый” стиль
# =========================

FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)
FONT_REGULAR_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
FONT_MONO_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
)


def _find_font(candidates: tuple[str, ...]) -> str:
    for p in candidates:
        if Path(p).exists():
            return p
    raise FileNotFoundError("DejaVu Sans не найден")


FONT_BOLD = _find_font(FONT_BOLD_CANDIDATES)
FONT_REGULAR = _find_font(FONT_REGULAR_CANDIDATES)
FONT_MONO = _find_font(FONT_MONO_CANDIDATES)


def font(path: str, size: int):
    return ImageFont.truetype(path, size=size)


def fit_font(draw, text, path, max_size, min_size, max_width):
    for size in range(max_size, min_size - 1, -2):
        f = font(path, size)
        b = draw.textbbox((0, 0), text, font=f)
        if b[2] - b[0] <= max_width:
            return f
    return font(path, min_size)



def background(width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height), (3, 10, 28, 255))
    d = ImageDraw.Draw(img)

    for y in range(height):
        t = y / max(1, height - 1)
        d.line(
            (0, y, width, y),
            fill=(
                int(3 + 3 * (1 - t)),
                int(10 + 12 * (1 - t)),
                int(28 + 28 * (1 - t)),
                255,
            ),
        )

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        (width * 0.46, -height * 0.26, width * 1.18, height * 0.28),
        fill=(0, 125, 255, 130),
    )
    gd.pieslice(
        (-220, height - 340, 420, height + 260),
        start=180, end=300,
        fill=(21, 100, 255, 120),
    )
    gd.pieslice(
        (width - 420, height - 300, width + 250, height + 250),
        start=240, end=360,
        fill=(0, 119, 255, 90),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(int(width * 0.12)))
    img.alpha_composite(glow)

    d = ImageDraw.Draw(img)
    margin = 26 if width == height else 34
    d.rounded_rectangle(
        (margin, margin, width - margin, height - margin),
        radius=38 if width == height else 46,
        outline=(55, 160, 255, 115),
        width=2,
    )
    return img


def neon_box(
    img: Image.Image,
    box: tuple[int, int, int, int],
    radius: int = 34,
    fill=(6, 20, 50, 234),
):
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(
        box,
        radius=radius,
        outline=(0, 150, 255, 180),
        width=12,
    )
    glow = glow.filter(ImageFilter.GaussianBlur(16))
    img.alpha_composite(glow)

    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        box,
        radius=radius,
        fill=fill,
        outline=(86, 190, 255, 245),
        width=2,
    )


def draw_brand(draw, width: int, y: int, story: bool):
    f1 = font(FONT_BOLD, 74 if not story else 94)
    f2 = font(FONT_BOLD, 82 if not story else 102)
    left, right = "Номера", "96"
    b1 = draw.textbbox((0, 0), left, font=f1)
    b2 = draw.textbbox((0, 0), right, font=f2)
    total = (b1[2] - b1[0]) + 8 + (b2[2] - b2[0])
    x = (width - total) // 2
    draw.text((x, y), left, font=f1, fill=(247, 250, 255))
    draw.text(
        (x + (b1[2] - b1[0]) + 8, y - 4),
        right,
        font=f2,
        fill=(34, 135, 255),
    )


def draw_subbrand(draw, width: int, y: int, text: str, story: bool = False):
    tf = font(FONT_BOLD, 16 if not story else 22)
    tb = draw.textbbox((0, 0), text, font=tf)
    line_w = 105 if not story else 130
    gap = 18
    text_w = tb[2] - tb[0]
    x = (width - text_w) // 2
    draw.line((x - gap - line_w, y + 10, x - gap, y + 10), fill=(73, 152, 230), width=2)
    draw.line((x + text_w + gap, y + 10, x + text_w + gap + line_w, y + 10), fill=(73, 152, 230), width=2)
    draw.text((x, y), text, font=tf, fill=(229, 236, 246))


def draw_cta(draw, width, height, story):
    text = CALL_TO_ACTION.upper()
    f = fit_font(
        draw, text, FONT_BOLD,
        38 if story else 31,
        24 if story else 20,
        width - 220,
    )
    b = draw.textbbox((0, 0), text, font=f)
    text_w = b[2] - b[0]

    y = height - (150 if story else 104)

    # Иконка-плашка Telegram слева
    icon_r = 34 if not story else 42
    left_icon_x = 115 if not story else 100
    cy = y + icon_r
    draw.ellipse(
        (left_icon_x, cy - icon_r, left_icon_x + icon_r * 2, cy + icon_r),
        fill=(17, 111, 242),
        outline=(122, 218, 255),
        width=2,
    )
    # Простая "стрелка" телеграма
    ax = left_icon_x + icon_r
    draw.polygon(
        [
            (ax - 18, cy + 2),
            (ax + 22, cy - 17),
            (ax + 8, cy + 22),
            (ax - 2, cy + 8),
        ],
        fill=(255, 255, 255),
    )

    # WhatsApp-подобный зелёный круг справа
    right_icon_x = width - (183 if not story else 195)
    draw.ellipse(
        (right_icon_x, cy - icon_r, right_icon_x + icon_r * 2, cy + icon_r),
        fill=(45, 190, 88),
        outline=(169, 255, 190),
        width=2,
    )
    # Упрощённый телефонный символ
    wf = font(FONT_BOLD, 30 if not story else 38)
    wb = draw.textbbox((0, 0), "☎", font=wf)
    draw.text(
        (
            right_icon_x + icon_r - (wb[2] - wb[0]) // 2,
            cy - (wb[3] - wb[1]) // 2 - 2,
        ),
        "☎",
        font=wf,
        fill=(255, 255, 255),
    )

    # CTA текст по центру
    tx = (width - text_w) // 2
    draw.text((tx, y + 6), text, font=f, fill=(255, 255, 255))

    # Подпись
    sub = "ОФОРМЛЕНИЕ ОНЛАЙН"
    sf = font(FONT_BOLD, 17 if not story else 22)
    sb = draw.textbbox((0, 0), sub, font=sf)
    sx = (width - (sb[2] - sb[0])) // 2
    sy = y + (52 if not story else 62)
    draw.line((sx - 105, sy + 10, sx - 18, sy + 10), fill=(69, 151, 229), width=2)
    draw.line((sx + (sb[2] - sb[0]) + 18, sy + 10, sx + (sb[2] - sb[0]) + 105, sy + 10), fill=(69, 151, 229), width=2)
    draw.text((sx, sy), sub, font=sf, fill=(123, 190, 244))


def draw_price_ribbon(
draw, x: int, y: int, w: int, h: int, price: int, story: bool = False):
    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=20 if not story else 24,
        fill=(17, 103, 240),
        outline=(146, 222, 255),
        width=2,
    )
    pf = font(FONT_BOLD, 30 if not story else 34)
    per = font(FONT_BOLD, 22 if not story else 25)
    txt = f"{price:,}".replace(",", " ")
    tb = draw.textbbox((0, 0), txt, font=pf)
    draw.text((x + 28, y + 10), txt, font=pf, fill=(255, 255, 255))
    draw.text((x + 28 + (tb[2] - tb[0]) + 10, y + 18), "₽/мес", font=per, fill=(235, 244, 255))


def render_number_day(
    item: NumberItem,
    target: Path,
    story: bool = False,
) -> None:
    width, height = (1080, 1920) if story else (1080, 1080)
    img = background(width, height)
    d = ImageDraw.Draw(img)

    draw_brand(d, width, 38 if not story else 66, story)

    head1 = "КРАСИВЫЙ НОМЕР"
    hf1 = fit_font(
        d, head1, FONT_BOLD,
        56 if not story else 74,
        30,
        width - 120,
    )
    hb1 = d.textbbox((0, 0), head1, font=hf1)
    y1 = 150 if not story else 215
    d.text(
        ((width - (hb1[2] - hb1[0])) // 2, y1),
        head1,
        font=hf1,
        fill=(249, 251, 255),
    )

    head2 = "НОМЕР ДНЯ"
    hf2 = fit_font(
        d, head2, FONT_BOLD,
        74 if not story else 94,
        34,
        width - 100,
    )
    hb2 = d.textbbox((0, 0), head2, font=hf2)
    y2 = y1 + (64 if not story else 86)
    d.text(
        ((width - (hb2[2] - hb2[0])) // 2, y2),
        head2,
        font=hf2,
        fill=(52, 146, 255),
    )

    draw_subbrand(
        d,
        width,
        y2 + (92 if not story else 122),
        "ПЛАТИТЕ ТОЛЬКО ЗА ТАРИФ",
        story,
    )

    panel = (
        (125, 390, width - 125, 760)
        if not story
        else (92, 560, width - 92, 1230)
    )
    neon_box(img, panel, 36 if not story else 44)
    d = ImageDraw.Draw(img)

    ribbon_w = int((panel[2] - panel[0]) * 0.66)
    draw_price_ribbon(
        d,
        panel[0] + 18,
        panel[1] + 16,
        ribbon_w,
        72 if not story else 88,
        item.price,
        story,
    )

    phone = format_phone(item.phone)
    pf = fit_font(
        d,
        phone,
        FONT_MONO,
        74 if not story else 92,
        44,
        width - 250,
    )
    pb = d.textbbox((0, 0), phone, font=pf)
    phone_y = panel[1] + (170 if not story else 270)
    d.text(
        ((width - (pb[2] - pb[0])) // 2, phone_y),
        phone,
        font=pf,
        fill=(255, 255, 255),
    )

    note = "НОМЕР БЕСПЛАТНО"
    nf = font(FONT_BOLD, 24 if not story else 31)
    nb = d.textbbox((0, 0), note, font=nf)
    d.text(
        ((width - (nb[2] - nb[0])) // 2, panel[3] - (72 if not story else 100)),
        note,
        font=nf,
        fill=(128, 201, 250),
    )

    draw_cta(d, width, height, story)
    img.convert("RGB").save(target, "PNG", optimize=True)



def render_tariff_group(
    img: Image.Image,
    box: tuple[int, int, int, int],
    price: int,
    items: list[NumberItem],
    story: bool = False,
) -> None:
    x1, y1, x2, y2 = box
    neon_box(img, box, 24 if not story else 30)
    d = ImageDraw.Draw(img)

    # Полноширинная синяя шапка тарифа.
    header_h = 72 if not story else 88
    draw.rounded_rectangle(
        (x1 + 2, y1 + 2, x2 - 2, y1 + header_h),
        radius=20,
        fill=(10, 76, 205),
        outline=(53, 164, 255),
        width=2,
    )

    label = "ТАРИФ"
    lf = font(FONT_BOLD, 15 if not story else 18)
    price_text = f"{price:,}".replace(",", " ")
    pf = fit_font(
        d, price_text, FONT_BOLD,
        36 if not story else 44,
        24 if not story else 30,
        (x2 - x1) - 95,
    )
    mf = font(FONT_BOLD, 14 if not story else 17)

    lb = d.textbbox((0, 0), label, font=lf)
    pb = d.textbbox((0, 0), price_text, font=pf)
    mb = d.textbbox((0, 0), "₽/мес", font=mf)
    total = (lb[2]-lb[0]) + 10 + (pb[2]-pb[0]) + 6 + (mb[2]-mb[0])
    xx = (x1 + x2 - total) // 2
    yy = y1 + 19 if not story else y1 + 24
    d.text((xx, yy + 8), label, font=lf, fill=(230, 242, 255))
    xx += (lb[2]-lb[0]) + 10
    d.text((xx, yy), price_text, font=pf, fill=(255,255,255))
    xx += (pb[2]-pb[0]) + 6
    d.text((xx, yy + 13), "₽/мес", font=mf, fill=(230,242,255))

    area_top = y1 + header_h + (25 if not story else 32)
    area_bottom = y2 - 18
    count = max(1, len(items))
    row_h = max(35 if not story else 45, (area_bottom - area_top) // count)

    for i, item in enumerate(items):
        yy = area_top + i * row_h
        phone = format_phone(item.phone).replace("+7 (", "").replace(") ", " ").strip()
        pf2 = fit_font(
            d, phone, FONT_MONO,
            29 if not story else 36,
            18 if not story else 23,
            (x2-x1)-24,
        )
        bb = d.textbbox((0,0), phone, font=pf2)
        px = (x1+x2-(bb[2]-bb[0]))//2
        d.text((px, yy), phone, font=pf2, fill=(255,255,255))


def render_card_list(
    items: list[NumberItem],
    target: Path,
    title: str,
    subtitle: str,
    story: bool = False,
) -> None:
    # Фиксируем именно квадратный Instagram-стиль Номера96.
    width, height = (1080, 1920) if story else (1080, 1080)
    img = background(width, height)
    d = ImageDraw.Draw(img)

    # Верхняя фирменная зона.
    brand_y = 55 if not story else 105
    draw_brand(d, width, brand_y, story)

    head = "КРАСИВЫЕ НОМЕРА"
    hf = fit_font(d, head, FONT_BOLD, 76 if not story else 88, 42, width - 100)
    hb = d.textbbox((0, 0), head, font=hf)
    hy = 155 if not story else 245
    d.text(((width - (hb[2] - hb[0])) // 2, hy), head, font=hf, fill=(255, 255, 255))

    sy = hy + (94 if not story else 112)
    draw_subbrand(d, width, sy, "ПЛАТИТЕ ТОЛЬКО ЗА ТАРИФ", story)

    # Группировка по тарифам.
    grouped: dict[int, list[NumberItem]] = {}
    for item in items:
        grouped.setdefault(item.price, []).append(item)
    groups = sorted(grouped.items(), key=lambda x: x[0], reverse=True)

    # Ограничиваем количество групп на одном кадре, чтобы не мельчить текст.
    max_groups = 4 if not story else 3
    groups = groups[:max_groups]

    top = 365 if not story else 570
    bottom = height - (185 if not story else 300)
    left, right = 58, width - 58
    gap = 20 if not story else 26
    n = max(1, len(groups))

    boxes = []
    if n == 1:
        boxes = [(135, top, width - 135, bottom)]
    elif n == 2:
        col = (right - left - gap) // 2
        boxes = [
            (left, top, left + col, bottom),
            (left + col + gap, top, right, bottom),
        ]
    elif n == 3:
        col = (right - left - gap * 2) // 3
        boxes = [
            (left, top, left + col, bottom),
            (left + col + gap, top, left + col * 2 + gap, bottom),
            (left + col * 2 + gap * 2, top, right, bottom),
        ]
    else:
        col = (right - left - gap * 3) // 4
        boxes = [
            (left + i * (col + gap), top, left + i * (col + gap) + col, bottom)
            for i in range(4)
        ]

    # Карточки строго вертикальные, как на референсе-карусели.
    for (price, group_items), box in zip(groups, boxes):
        # Не пытаемся впихнуть десятки номеров: берём столько,
        # сколько остаётся читаемым в Instagram.
        capacity = 13 if n <= 3 else 8
        render_tariff_group(img, box, price, group_items[:capacity], story)

    # CTA снизу.
    draw_cta(ImageDraw.Draw(img), width, height, story)
    img.convert("RGB").save(target, "PNG", optimize=True)


def render_selection(

    items: list[NumberItem],
    target: Path,
    title: str,
    subtitle: str,
    story: bool = False,
) -> None:
    if len(items) == 1:
        render_number_day(items[0], target, story)
    else:
        render_card_list(items, target, title, subtitle, story)


def render_tariff_page(
    price: int,
    phones: list[str],
    target: Path,
    page_no: int,
    page_total: int,
    story: bool = False,
) -> None:
    width, height = (1080, 1920) if story else (1080, 1080)
    img = background(width, height)
    d = ImageDraw.Draw(img)

    draw_brand(d, width, 44 if not story else 70, story)
    head = "КРАСИВЫЕ НОМЕРА"
    hf = fit_font(d, head, FONT_BOLD, 42 if not story else 56, 24, width - 80)
    hb = d.textbbox((0, 0), head, font=hf)
    y1 = 135 if not story else 190
    d.text(((width - (hb[2] - hb[0])) // 2, y1), head, font=hf, fill=(248, 251, 255))

    head2 = "В НАЛИЧИИ"
    h2f = fit_font(d, head2, FONT_BOLD, 64 if not story else 78, 30, width - 80)
    h2b = d.textbbox((0, 0), head2, font=h2f)
    y2 = y1 + (56 if not story else 78)
    d.text(((width - (h2b[2] - h2b[0])) // 2, y2), head2, font=h2f, fill=(53, 145, 255))

    draw_subbrand(d, width, y2 + (82 if not story else 104), "ПЛАТИТЕ ТОЛЬКО ЗА ТАРИФ", story)

    panel = (82, 320, width - 82, height - (170 if not story else 270))
    neon_box(img, panel, 34 if not story else 40)
    d = ImageDraw.Draw(img)

    ribbon_w = int((panel[2] - panel[0]) * 0.48)
    draw_price_ribbon(d, panel[0] + 16, panel[1] + 16, ribbon_w, 60 if not story else 72, price, story)

    inner_top = panel[1] + (110 if not story else 134)
    inner_bottom = panel[3] - 30
    row_h = max(62 if not story else 80, (inner_bottom - inner_top) // max(1, len(phones)))

    pf = fit_font(d, format_phone(phones[0]), FONT_MONO, 40 if not story else 50, 24, (panel[2] - panel[0]) - 70)
    for idx, phone in enumerate(phones):
        y = inner_top + idx * row_h
        if idx > 0:
            d.line((panel[0] + 26, y - 12, panel[2] - 26, y - 12), fill=(62, 122, 186), width=1)
        txt = format_phone(phone)
        tb = d.textbbox((0, 0), txt, font=pf)
        d.text(((width - (tb[2] - tb[0])) // 2, y), txt, font=pf, fill=(255, 255, 255))

    footer = f"{page_no}/{page_total}"
    ff = font(FONT_BOLD, 18 if not story else 24)
    fb = d.textbbox((0, 0), footer, font=ff)
    d.text((width - 82 - (fb[2] - fb[0]), panel[3] + 16), footer, font=ff, fill=(123, 194, 246))

    draw_cta(d, width, height, story)
    img.convert("RGB").save(target, "PNG", optimize=True)


def caption_for(
items: list[NumberItem], title: str) -> str:
    lines = [f"🔥 {title}", ""]
    for x in items:
        lines.append(
            f"📱 {format_phone(x.phone)} — "
            f"{x.price:,}".replace(",", " ") + " ₽/мес"
        )
    lines += [
        "",
        "✅ Номер бесплатно",
        "💳 Оплата только за тариф",
        "✅ Оформление онлайн",
        "",
        "Понравился номер? Пиши «НОМЕР» в Direct 📩",
        "",
        "#номера96 #красивыеномера #красивыйномер #тарифы #симкарта",
    ]
    return "\n".join(lines)


def output_folder(prefix: str) -> Path:
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    folder = OUTPUT_DIR / f"{prefix}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def create_selection_bundle(
    items: list[NumberItem],
    title: str,
    subtitle: str,
    prefix: str,
) -> tuple[Path, Path, str]:
    folder = output_folder(prefix)
    post = folder / "post.png"
    story = folder / "story.png"
    render_selection(items, post, title, subtitle, False)
    render_selection(items, story, title, subtitle, True)
    return post, story, caption_for(items, title)


def create_full_catalog(catalog: list[tuple[str, int]]) -> tuple[Path, int]:
    folder = output_folder("catalog")
    posts = folder / "posts"
    stories = folder / "stories"
    posts.mkdir()
    stories.mkdir()

    by_price: dict[int, list[str]] = {}
    for phone, price in catalog:
        by_price.setdefault(price, []).append(phone)

    jobs: list[tuple[int, list[str]]] = []
    for price in sorted(by_price, reverse=True):
        phones = by_price[price]
        chunk_size = 6
        for i in range(0, len(phones), chunk_size):
            jobs.append((price, phones[i:i + chunk_size]))

    total = len(jobs)

    for idx, (price, phones) in enumerate(jobs, start=1):
        render_tariff_page(
            price, phones,
            posts / f"post_{idx:02d}_{price}.png",
            idx, total, False,
        )
        render_tariff_page(
            price, phones,
            stories / f"story_{idx:02d}_{price}.png",
            idx, total, True,
        )

    desc = (
        "Красивые номера в наличии 🔥\n\n"
        "✅ Номер бесплатно\n"
        "💳 Оплата только за выбранный тариф\n"
        "✅ Оформление онлайн\n\n"
        "Пиши «НОМЕР» в Direct — подберём подходящий вариант 📩\n\n"
        "#номера96 #красивыеномера #тарифы #симкарта"
    )
    (folder / "description.txt").write_text(desc, encoding="utf-8")

    archive = folder / "nomera96_catalog.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in posts.glob("*.png"):
            z.write(p, p.relative_to(folder))
        for p in stories.glob("*.png"):
            z.write(p, p.relative_to(folder))
        z.write(folder / "description.txt", "description.txt")

    return archive, total



# =========================
# Reels
# =========================

REELS_META = {
    "choice": ("Какой номер выберешь?", "top5", 4),
    "day": ("Номер дня", "day", 1),
    "budget": ("Красивые до 1000 ₽", "budget", 4),
    "premium": ("Премиум номера", "premium", 4),
}


def reels_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👀 Какой выберешь?", callback_data="reels:choice")],
            [
                InlineKeyboardButton(text="⭐ Номер дня", callback_data="reels:day"),
                InlineKeyboardButton(text="💰 До 1000 ₽", callback_data="reels:budget"),
            ],
            [InlineKeyboardButton(text="💎 Премиум", callback_data="reels:premium")],
        ]
    )


def _center_text(draw, text, font_obj, width, y, fill):
    b = draw.textbbox((0, 0), text, font=font_obj)
    draw.text(((width - (b[2] - b[0])) // 2, y), text, font=font_obj, fill=fill)


def render_reels_frame(
    target: Path,
    headline: str,
    phone: str | None = None,
    price: int | None = None,
    footer: str | None = None,
) -> None:
    width, height = 1080, 1920
    img = background(width, height)
    d = ImageDraw.Draw(img)

    draw_brand(d, width, 70, True)

    hf = fit_font(d, headline.upper(), FONT_BOLD, 54, 30, width - 120)
    hb = d.textbbox((0, 0), headline.upper(), font=hf)
    d.text(((width - (hb[2] - hb[0])) // 2, 210), headline.upper(), font=hf, fill=(248, 251, 255))

    draw_subbrand(d, width, 300, "ПЛАТИТЕ ТОЛЬКО ЗА ТАРИФ", True)

    panel = (88, 430, width - 88, 1240)
    neon_box(img, panel, 44)
    d = ImageDraw.Draw(img)

    if phone:
        draw_price_ribbon(d, panel[0] + 26, panel[1] + 26, 530, 86, int(price or 0), True)
        phone_text = format_phone(phone)
        pf = fit_font(d, phone_text, FONT_MONO, 76, 48, width - 220)
        pb = d.textbbox((0, 0), phone_text, font=pf)
        d.text(((width - (pb[2] - pb[0])) // 2, panel[1] + 260), phone_text, font=pf, fill=(255, 255, 255))
        lf = font(FONT_BOLD, 28)
        label = "КРАСИВЫЙ НОМЕР"
        lb = d.textbbox((0, 0), label, font=lf)
        d.text(((width - (lb[2] - lb[0])) // 2, panel[1] + 170), label, font=lf, fill=(126, 198, 250))
        if footer:
            ff = fit_font(d, footer, FONT_BOLD, 30, 20, width - 160)
            fb = d.textbbox((0, 0), footer, font=ff)
            d.text(((width - (fb[2] - fb[0])) // 2, panel[3] - 90), footer, font=ff, fill=(223, 236, 248))
    else:
        text_out = footer or "НОМЕРА96"
        ff = fit_font(d, text_out, FONT_BOLD, 52, 30, width - 160)
        fb = d.textbbox((0, 0), text_out, font=ff)
        d.text(((width - (fb[2] - fb[0])) // 2, panel[1] + 300), text_out, font=ff, fill=(255, 255, 255))

    draw_cta(d, width, height, True)
    img.convert("RGB").save(target, "PNG", optimize=True)


def reels_script(
items: list[NumberItem], kind: str) -> str:
    title, _, _ = REELS_META[kind]
    lines = [f"0–1 сек — {title}"]
    sec = 1

    if kind == "day":
        item = items[0]
        lines.append(
            f"1–5 сек — {format_phone(item.phone)} • "
            f"{item.price:,}".replace(",", " ") + " ₽/мес"
        )
        lines += [
            "5–7 сек — Номер бесплатно",
            "7–9 сек — Платишь только за тариф",
            '9–11 сек — Пиши «НОМЕР» в Direct',
        ]
    else:
        for item in items:
            lines.append(
                f"{sec}–{sec+1} сек — {format_phone(item.phone)} • "
                f"{item.price:,}".replace(",", " ") + " ₽/мес"
            )
            sec += 1
        lines += [
            f"{sec}–{sec+1} сек — Номер бесплатно",
            f"{sec+1}–{sec+2} сек — Оплата только за тариф",
            f'{sec+2}–{sec+4} сек — Пиши «НОМЕР» в Direct',
        ]

    return "\n".join(lines)


def reels_caption(items: list[NumberItem], kind: str) -> str:
    title, _, _ = REELS_META[kind]
    lines = [f"🔥 {title}", ""]
    for item in items:
        lines.append(
            f"📱 {format_phone(item.phone)} — "
            f"{item.price:,}".replace(",", " ") + " ₽/мес"
        )
    lines += [
        "",
        "✅ Номер бесплатно",
        "💳 Оплата только за выбранный тариф",
        "✅ Оформление онлайн",
        "",
        'Пиши «НОМЕР» в Direct — подберём варианты 📩',
        "",
        "#номера96 #красивыеномера #красивыйномер #тарифы #reels",
    ]
    return "\n".join(lines)


def create_reels_package(items: list[NumberItem], kind: str) -> tuple[Path, Path, str, str]:
    title, _, _ = REELS_META[kind]
    folder = output_folder("reels")
    frames = folder / "frames"
    frames.mkdir()

    render_reels_frame(
        frames / "00_intro.png",
        title,
        footer="СМОТРИ ДО КОНЦА",
    )

    for idx, item in enumerate(items, start=1):
        render_reels_frame(
            frames / f"{idx:02d}_number.png",
            title,
            phone=item.phone,
            price=item.price,
            footer="НОМЕР БЕСПЛАТНО",
        )

    render_reels_frame(
        frames / f"{len(items)+1:02d}_outro.png",
        "ПОНРАВИЛСЯ НОМЕР?",
        footer='ПИШИ «НОМЕР» В DIRECT',
    )

    script = reels_script(items, kind)
    caption = reels_caption(items, kind)
    (folder / "script.txt").write_text(script, encoding="utf-8")
    (folder / "caption.txt").write_text(caption, encoding="utf-8")

    archive = folder / "nomera96_reels.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(frames.glob("*.png")):
            z.write(p, p.relative_to(folder))
        z.write(folder / "script.txt", "script.txt")
        z.write(folder / "caption.txt", "caption.txt")

    preview = frames / ("01_number.png" if items else "00_intro.png")
    return archive, preview, script, caption


async def generate_reels_for_chat(bot: Bot, chat_id: int, kind: str) -> bool:
    state = load_state()
    catalog = get_catalog(state)
    if not catalog:
        await bot.send_message(chat_id, "Сначала загрузи каталог через «📥 Обновить каталог».")
        return False

    title, mode, limit = REELS_META[kind]
    items = select_mode(catalog, mode, set(), limit)
    if not items:
        await bot.send_message(chat_id, "Для этого Reels подходящих номеров пока нет.")
        return False

    status = await bot.send_message(chat_id, "🎬 Готовлю Reels-пакет…")
    archive, preview, script, caption = await asyncio.to_thread(
        create_reels_package, items, kind
    )
    await status.edit_text("Reels готов ✅")

    await bot.send_photo(
        chat_id,
        FSInputFile(preview),
        caption="Пример кадра Reels 9:16",
    )
    await bot.send_document(
        chat_id,
        FSInputFile(archive),
        caption=(
            "🎬 Reels-пакет готов.\n\n"
            "Внутри: заставка, кадры с номерами, финальный CTA, "
            "script.txt и caption.txt."
        ),
    )
    await bot.send_message(chat_id, "🎞 Сценарий:\n\n" + script)
    await bot.send_message(chat_id, "📝 Подпись:\n\n" + caption)
    return True



# =========================
# Telegram UI
# =========================

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🔥 ТОП-5"),
            KeyboardButton(text="⭐ Номер дня"),
        ],
        [
            KeyboardButton(text="💰 До 1000 ₽"),
            KeyboardButton(text="💎 Премиум"),
        ],
        [KeyboardButton(text="📚 Все номера по тарифам")],
        [KeyboardButton(text="🎬 Reels")],
        [
            KeyboardButton(text="📥 Обновить каталог"),
            KeyboardButton(text="🤖 Автопилот"),
        ],
        [
            KeyboardButton(text="📊 Статус"),
            KeyboardButton(text="🔗 Суперссылка"),
        ],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выбери формат контента",
)


MODE_META = {
    "top5": ("ТОП-5 КРАСИВЫХ НОМЕРОВ", "самые запоминающиеся номера из каталога"),
    "budget": ("КРАСИВЫЕ ДО 1000 ₽", "доступные тарифы и красивые комбинации"),
    "premium": ("ПРЕМИУМ НОМЕРА", "самые сильные комбинации из дорогих тарифов"),
    "day": ("НОМЕР ДНЯ", "один номер, на котором легко сделать отдельный пост"),
}


def draft_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data="draft:approve"),
                InlineKeyboardButton(text="🔄 Другие", callback_data="draft:next"),
            ],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="draft:skip")],
        ]
    )


def get_owner_id() -> int | None:
    if OWNER_ID_ENV.isdigit():
        return int(OWNER_ID_ENV)
    if OWNER_FILE.exists():
        value = OWNER_FILE.read_text(encoding="utf-8").strip()
        if value.isdigit():
            return int(value)
    return None


def claim_or_check_owner(user_id: int) -> bool:
    owner = get_owner_id()
    if owner is None:
        OWNER_FILE.write_text(str(user_id), encoding="utf-8")
        return True
    return owner == user_id


async def deny(message: Message) -> bool:
    if not message.from_user:
        return True
    if not claim_or_check_owner(message.from_user.id):
        await message.answer("⛔ Бот используется владельцем Номера96.")
        return True
    return False


def catalog_stats(state: dict) -> tuple[int, int]:
    catalog = get_catalog(state)
    used = set(state.get("used", []))
    available = sum(1 for p, _ in catalog if p not in used)
    return len(catalog), available


async def send_draft(bot: Bot, chat_id: int, mode: str, rotate: bool = False) -> bool:
    state = load_state()
    catalog = get_catalog(state)

    if not catalog:
        await bot.send_message(
            chat_id,
            "Сначала нажми «📥 Обновить каталог» и вставь свежий список.",
        )
        return False

    used = set(state.get("used", []))

    if rotate and state.get("draft"):
        used.update(str(x["phone"]) for x in state["draft"])

    items = select_mode(catalog, mode, used, 5)
    if not items:
        await bot.send_message(chat_id, "Для этого режима подходящих номеров нет.")
        return False

    title, subtitle = MODE_META[mode]
    try:
        post, _, _ = await asyncio.to_thread(
            create_selection_bundle,
            items, title, subtitle, "draft",
        )
    except Exception as exc:
        logging.exception("Ошибка генерации режима %s", mode)
        await bot.send_message(
            chat_id,
            f"⚠️ Не удалось создать {title}. Ошибка: {type(exc).__name__}. "
            "Теперь ошибка отображается, а не пропадает молча.",
        )
        return False

    state["draft"] = [
        {"phone": x.phone, "price": x.price, "beauty": x.beauty}
        for x in items
    ]
    state["draft_mode"] = mode
    state["draft_title"] = title
    state["draft_created_at"] = datetime.now(TZ).isoformat()
    save_state(state)

    await bot.send_photo(
        chat_id,
        FSInputFile(post),
        caption=(
            f"🤖 Черновик: {title}\n\n"
            "✅ Одобрить — получить финальный пост + сторис + описание\n"
            "🔄 Другие — заменить номера\n"
            "⏭ Пропустить — оставить на потом"
        ),
        reply_markup=draft_keyboard(),
    )
    return True


# =========================
# Хендлеры
# =========================

@router.message(CommandStart())
async def start(message: Message):
    if await deny(message):
        return
    state = load_state()
    total, available = catalog_stats(state)
    await message.answer(
        f"Номера96 Автопилот {BUILD_VERSION} ✅\n\n"
        f"Каталог: {total} номеров\n"
        f"Не использовано: {available}\n\n"
        "Теперь есть отдельный режим 🎬 Reels с готовыми кадрами и сценарием.",
        reply_markup=MENU,
    )



@router.message(F.text == "🎬 Reels")
async def reels_menu(message: Message):
    if await deny(message):
        return
    await message.answer("🎬 Выбери формат Reels:", reply_markup=reels_menu_keyboard())


@router.callback_query(F.data.startswith("reels:"))
async def reels_callback(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    kind = (callback.data or "").split(":", 1)[1]
    if kind not in REELS_META:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    await callback.answer("Готовлю Reels…")
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await generate_reels_for_chat(bot, chat_id, kind)


@router.message(F.text == "🔥 ТОП-5")
async def top5(message: Message, bot: Bot):
    if await deny(message):
        return
    await send_draft(bot, message.chat.id, "top5")


@router.message(F.text == "⭐ Номер дня")
async def day_number(message: Message, bot: Bot):
    if await deny(message):
        return
    await send_draft(bot, message.chat.id, "day")


@router.message(F.text == "💰 До 1000 ₽")
async def budget(message: Message, bot: Bot):
    if await deny(message):
        return
    await send_draft(bot, message.chat.id, "budget")


@router.message(F.text == "💎 Премиум")
async def premium(message: Message, bot: Bot):
    if await deny(message):
        return
    await send_draft(bot, message.chat.id, "premium")


@router.message(F.text == "📚 Все номера по тарифам")
async def all_catalog(message: Message):
    if await deny(message):
        return

    state = load_state()
    catalog = get_catalog(state)
    if not catalog:
        await message.answer("Сначала загрузи каталог.")
        return

    status = await message.answer("Собираю полный каталог по тарифам…")
    archive, pages = await asyncio.to_thread(create_full_catalog, catalog)
    await status.edit_text(f"Готово ✅ Страниц в карусели: {pages}")
    await message.answer_document(
        FSInputFile(archive),
        caption=(
            "📚 Полный каталог Номера96\n"
            "Внутри:\n"
            "• квадратные посты по тарифам;\n"
            "• сторис;\n"
            "• описание."
        ),
    )


@router.message(F.text == "📥 Обновить каталог")
async def catalog_prompt(message: Message):
    if await deny(message):
        return
    await message.answer(
        "Вставь свежий список целиком:\n\n"
        "Тариф: 1600 руб/мес\n"
        "9003366888\n"
        "9011163333\n\n"
        "Тариф: 950 руб/мес\n"
        "9010777477"
    )


@router.message(F.text == "🤖 Автопилот")
async def toggle_auto(message: Message):
    if await deny(message):
        return
    state = load_state()
    state["autopilot"] = not bool(state.get("autopilot", True))
    save_state(state)
    txt = "ВКЛЮЧЁН ✅" if state["autopilot"] else "ВЫКЛЮЧЕН ⛔"
    await message.answer(
        f"🤖 Автопилот {txt}\n\n"
        "Каждый день бот сам меняет формат контента: "
        "ТОП-5, бюджетные, номер дня или премиум."
    )


@router.message(F.text == "📊 Статус")
async def status(message: Message):
    if await deny(message):
        return
    state = load_state()
    total, available = catalog_stats(state)
    await message.answer(
        f"📊 Номера96 • {BUILD_VERSION}\n\n"
        f"Всего номеров: {total}\n"
        f"Не использовано: {available}\n"
        f"Автопилот: {'ВКЛ ✅' if state.get('autopilot', True) else 'ВЫКЛ ⛔'}\n"
        f"Время автоподборки: {AUTOPILOT_HOUR:02d}:{AUTOPILOT_MINUTE:02d}"
    )


@router.message(F.text == "🔗 Суперссылка")
async def superlink(message: Message):
    if await deny(message):
        return
    await message.answer(f"🔗 {STORE_LINK}")



@router.message(Command("version"))
async def version(message: Message):
    if await deny(message):
        return
    await message.answer(
        f"✅ Запущена версия: {BUILD_VERSION}\n"
        "Стиль: синие тарифные плашки / группировка по тарифам / Direct + WhatsApp"
    )


@router.message(Command("myid"))
async def myid(message: Message):
    if message.from_user:
        await message.answer(f"Твой Telegram ID: {message.from_user.id}")


@router.callback_query(F.data == "draft:approve")
async def approve(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    state = load_state()
    raw = state.get("draft", [])
    mode = state.get("draft_mode") or "top5"
    if not raw:
        await callback.answer("Черновик устарел", show_alert=True)
        return

    items = [
        NumberItem(
            phone=str(x["phone"]),
            price=int(x["price"]),
            beauty=int(x.get("beauty", 0)),
        )
        for x in raw
    ]

    title, subtitle = MODE_META.get(mode, MODE_META["top5"])
    post, story, caption = await asyncio.to_thread(
        create_selection_bundle,
        items, title, subtitle, "approved",
    )

    used = set(state.get("used", []))
    used.update(x.phone for x in items)
    state["used"] = sorted(used)
    state["draft"] = []
    state["draft_mode"] = None
    save_state(state)

    await callback.answer("Одобрено ✅")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await bot.send_document(
        chat_id,
        FSInputFile(post),
        caption="✅ Пост 1:1 без сжатия",
    )
    await bot.send_document(
        chat_id,
        FSInputFile(story),
        caption="✅ Сторис 9:16 без сжатия",
    )
    await bot.send_message(chat_id, "📝 Описание:\n\n" + caption)


@router.callback_query(F.data == "draft:next")
async def next_draft(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    state = load_state()
    mode = state.get("draft_mode") or "top5"

    await callback.answer("Ищу другие…")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send_draft(bot, callback.message.chat.id, mode, rotate=True)


@router.callback_query(F.data == "draft:skip")
async def skip(callback: CallbackQuery):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    state = load_state()
    state["draft"] = []
    state["draft_mode"] = None
    save_state(state)

    await callback.answer("Пропущено")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("⏭ Пропущено. Номера остались в каталоге.")
        except Exception:
            pass


@router.message(F.text)
async def text_input(message: Message, bot: Bot):
    if await deny(message):
        return

    text = message.text or ""
    if "тариф" not in text.casefold():
        return

    try:
        rows = parse_catalog(text)
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    save_catalog(rows)
    await message.answer(
        f"Каталог обновлён ✅\n"
        f"Загружено номеров: {len(rows)}\n\n"
        "Сразу делаю ТОП-5."
    )
    await send_draft(bot, message.chat.id, "top5")


# =========================
# Автопилот по дням недели
# =========================

def auto_mode_for_today(now: datetime) -> str:
    # 0 пн ... 6 вс
    schedule = {
        0: "top5",
        1: "budget",
        2: "day",
        3: "premium",
        4: "top5",
        5: "budget",
        6: "day",
    }
    return schedule[now.weekday()]


async def autopilot_loop(bot: Bot):
    while True:
        try:
            await asyncio.sleep(20)

            owner = get_owner_id()
            if owner is None:
                continue

            state = load_state()
            if not state.get("autopilot", True) or not state.get("catalog"):
                continue

            now = datetime.now(TZ)
            today = now.date().isoformat()
            if state.get("last_auto_date") == today:
                continue

            target = now.replace(
                hour=AUTOPILOT_HOUR,
                minute=AUTOPILOT_MINUTE,
                second=0,
                microsecond=0,
            )

            if target <= now <= target + timedelta(hours=4):
                mode = auto_mode_for_today(now)
                sent = await send_draft(bot, owner, mode)
                if sent:
                    state = load_state()
                    state["last_auto_date"] = today
                    save_state(state)

        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка автопилота")
            await asyncio.sleep(60)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    task = asyncio.create_task(autopilot_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
