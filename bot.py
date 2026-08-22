from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import random
import shutil
import time
import zipfile
import urllib.request
import urllib.parse
import urllib.error
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiohttp import web
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

BEZLIMIT_API_BASE = os.getenv(
    "BEZLIMIT_API_BASE",
    "https://api.store.bezlimit.ru",
).strip().rstrip("/")
BEZLIMIT_AUTH = os.getenv("BEZLIMIT_AUTH", "").strip()
BEZLIMIT_API_TOKEN = os.getenv("BEZLIMIT_API_TOKEN", "").strip()
BEZLIMIT_CACHE_MINUTES = int(os.getenv("BEZLIMIT_CACHE_MINUTES", "10"))
BEZLIMIT_FETCH_PAGES = max(1, min(12, int(os.getenv("BEZLIMIT_FETCH_PAGES", "5"))))
BEZLIMIT_PER_PAGE = max(20, min(100, int(os.getenv("BEZLIMIT_PER_PAGE", "100"))))

IG_USER_ID = os.getenv("IG_USER_ID", "").strip()
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()
META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0").strip() or "v23.0"

RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
if not PUBLIC_BASE_URL and RAILWAY_PUBLIC_DOMAIN:
    PUBLIC_BASE_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}"

PORT = int(os.getenv("PORT", "8080"))

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
PUBLIC_MEDIA_DIR = ROOT / "public_media"
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
PUBLIC_MEDIA_DIR.mkdir(exist_ok=True)

STATE_FILE = DATA_DIR / "autopilot_state.json"
OWNER_FILE = DATA_DIR / "owner_id.txt"
IG_STORIES_STORAGE_FILE = DATA_DIR / "ig_stories_storage.json"

IG_STORIES_LOGIN = os.getenv("IG_STORIES_LOGIN", "").strip()
IG_STORIES_PASSWORD = os.getenv("IG_STORIES_PASSWORD", "").strip()
IG_STORIES_HEADLESS = os.getenv("IG_STORIES_HEADLESS", "true").strip().lower() not in {"0", "false", "no"}
IG_STORIES_INTERVAL_SECONDS = max(60, int(os.getenv("IG_STORIES_INTERVAL_SECONDS", "120")))

router = Router()

BUILD_VERSION = "v19.2-startup-fixed"

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
    "api_updated_at": None,
    "api_last_error": None,
    "last_approved": None,
    "last_instagram_media_id": None,
    "last_instagram_story_id": None,
    "last_instagram_error": None,
    "ig_auto_enabled": False,
    "ig_auto_top5_time": "10:00",
    "ig_auto_budget_time": "15:00",
    "ig_auto_day_time": "19:00",
    "ig_auto_last_top5_date": None,
    "ig_auto_last_budget_date": None,
    "ig_auto_last_day_date": None,
    "ig_auto_awaiting_time_mode": None,
    "ig_auto_history": [],
    "stories_enabled": False,
    "stories_daily_limit": 30,
    "stories_usernames": [],
    "stories_viewed_today": 0,
    "stories_viewed_date": None,
    "stories_history": [],
    "stories_awaiting": None,
    "stories_last_error": None,
    "stories_last_username": None,
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




def _pick_best_from_tariff(
    items: list[NumberItem],
    target_price: int,
) -> NumberItem | None:
    pool = [x for x in items if x.price == target_price]
    if not pool:
        return None
    pool.sort(key=lambda x: x.beauty, reverse=True)
    top = pool[: min(12, len(pool))]
    weights = list(range(len(top), 0, -1))
    return random.choices(top, weights=weights, k=1)[0]


def _pick_from_weighted_tariffs(
    items: list[NumberItem],
    distribution: list[tuple[int, float]],
) -> NumberItem | None:
    available = [
        (price, weight)
        for price, weight in distribution
        if any(x.price == price for x in items)
    ]
    if not available:
        return None
    prices = [x[0] for x in available]
    weights = [x[1] for x in available]
    chosen_price = random.choices(prices, weights=weights, k=1)[0]
    return _pick_best_from_tariff(items, chosen_price)


def select_manual_cheap_item(
    catalog: list[tuple[str, int]],
    used: set[str],
) -> NumberItem | None:
    items = [x for x in build_items(catalog) if x.phone not in used]
    if not items:
        items = build_items(catalog)

    item = _pick_from_weighted_tariffs(
        items,
        [(550, 40), (750, 30), (399, 20), (950, 10)],
    )
    if item:
        return item

    fallback = [x for x in items if x.price <= 950]
    if not fallback:
        return None
    cheapest = min(x.price for x in fallback)
    return _pick_best_from_tariff(fallback, cheapest)


def select_feed_item(
    catalog: list[tuple[str, int]],
    used: set[str],
) -> NumberItem | None:
    items = [x for x in build_items(catalog) if x.phone not in used]
    if not items:
        items = build_items(catalog)
    if not items:
        return None

    # 95%: affordable target tariffs.
    if random.random() < 0.95:
        item = _pick_from_weighted_tariffs(
            items,
            [(550, 35), (750, 30), (399, 20), (950, 10)],
        )
        if item:
            return item

    # 5%: other tariffs for variety.
    others = [x for x in items if x.price not in {399, 550, 750, 950}]
    if not others:
        return _pick_from_weighted_tariffs(
            items,
            [(550, 35), (750, 30), (399, 20), (950, 10)],
        )

    price_groups: dict[int, list[NumberItem]] = {}
    for item in others:
        price_groups.setdefault(item.price, []).append(item)

    prices = sorted(price_groups)
    weights = [max(1, len(prices) - i) for i in range(len(prices))]
    chosen_price = random.choices(prices, weights=weights, k=1)[0]
    return _pick_best_from_tariff(others, chosen_price)


def select_mode(
    catalog: list[tuple[str, int]],
    mode: str,
    used: set[str],
    limit: int = 5,
) -> list[NumberItem]:
    if mode == "manual_cheap":
        item = select_manual_cheap_item(catalog, used)
        return [item] if item else []

    if mode == "feed":
        item = select_feed_item(catalog, used)
        return [item] if item else []

    items = build_items(catalog)

    if mode == "budget":
        items = [x for x in items if x.price <= 1000]
    elif mode == "premium":
        items = [x for x in items if x.price >= 2000]
        limit = 1
    elif mode in {"day", "single"}:
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

    draw_brand(d, width, 46 if not story else 84, story)

    title = "НОМЕР ДНЯ"
    tf = fit_font(
        d, title, FONT_BOLD,
        90 if not story else 114,
        48,
        width - 110,
    )
    tb = d.textbbox((0, 0), title, font=tf)
    ty = 180 if not story else 300
    d.text(
        ((width - (tb[2] - tb[0])) // 2, ty),
        title,
        font=tf,
        fill=(255, 255, 255),
    )

    draw_subbrand(
        d,
        width,
        ty + (112 if not story else 145),
        "САМЫЙ КРАСИВЫЙ НОМЕР СЕГОДНЯ",
        story,
    )

    panel = (
        (76, 410, width - 76, 790)
        if not story
        else (70, 650, width - 70, 1320)
    )
    neon_box(img, panel, 42)
    d = ImageDraw.Draw(img)

    phone = format_phone(item.phone).replace("+7 (", "").replace(") ", " ")
    pf = fit_font(
        d,
        phone,
        FONT_MONO,
        116 if not story else 148,
        58,
        panel[2] - panel[0] - 80,
    )
    pb = d.textbbox((0, 0), phone, font=pf)
    py = panel[1] + ((panel[3] - panel[1]) - (pb[3] - pb[1])) // 2 - 8
    d.text(
        ((width - (pb[2] - pb[0])) // 2, py),
        phone,
        font=pf,
        fill=(255, 255, 255),
    )

    draw_cta(d, width, height, story)
    img.convert("RGB").save(target, "PNG", optimize=True)


def render_single_number(
    item: NumberItem,
    target: Path,
    title: str = "КРАСИВЫЙ НОМЕР",
    story: bool = False,
) -> None:
    width, height = (1080, 1920) if story else (1080, 1080)
    img = background(width, height)
    d = ImageDraw.Draw(img)
    draw_brand(d, width, 48 if not story else 90, story)

    tf = fit_font(d, title, FONT_BOLD, 76 if not story else 96, 40, width-100)
    tb = d.textbbox((0,0), title, font=tf)
    ty = 190 if not story else 300
    d.text(((width-(tb[2]-tb[0]))//2, ty), title, font=tf, fill=(255,255,255))
    draw_subbrand(d, width, ty+(95 if not story else 125), "ПЛАТИТЕ ТОЛЬКО ЗА ТАРИФ", story)

    panel = (70, 400, width-70, 790) if not story else (70, 650, width-70, 1320)
    neon_box(img, panel, 42)
    d = ImageDraw.Draw(img)

    phone = format_phone(item.phone).replace("+7 (", "").replace(") ", " ")
    pf = fit_font(d, phone, FONT_MONO, 112 if not story else 140, 56, panel[2]-panel[0]-70)
    pb = d.textbbox((0,0), phone, font=pf)
    py = panel[1] + 115 if not story else panel[1] + 235
    d.text(((width-(pb[2]-pb[0]))//2, py), phone, font=pf, fill=(255,255,255))

    price = f"{item.price:,} ₽/мес".replace(",", " ")
    sf = font(FONT_BOLD, 34 if not story else 44)
    sb = d.textbbox((0,0), price, font=sf)
    d.text(((width-(sb[2]-sb[0]))//2, panel[3]-92 if not story else panel[3]-130),
           price, font=sf, fill=(69,159,255))

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
    d.rounded_rectangle(
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
    width, height = (1080, 1920) if story else (1080, 1080)
    img = background(width, height)
    d = ImageDraw.Draw(img)

    draw_brand(d, width, 40 if not story else 78, story)

    # Заголовок зависит от режима, но визуальный шаблон один.
    if "1000" in title:
        head1 = "КРАСИВЫЕ НОМЕРА"
        head2 = "ДО 1000 ₽"
    elif "ПРЕМИУМ" in title:
        head1 = "ПРЕМИУМ"
        head2 = "КРАСИВЫЕ НОМЕРА"
    else:
        head1 = "ТОП-5"
        head2 = "КРАСИВЫХ НОМЕРОВ"

    f1 = fit_font(
        d, head1, FONT_BOLD,
        64 if not story else 80,
        34,
        width - 100,
    )
    b1 = d.textbbox((0, 0), head1, font=f1)
    y1 = 150 if not story else 225
    d.text(
        ((width - (b1[2] - b1[0])) // 2, y1),
        head1,
        font=f1,
        fill=(55, 151, 255) if head1 == "ТОП-5" else (255, 255, 255),
    )

    f2 = fit_font(
        d, head2, FONT_BOLD,
        70 if not story else 88,
        36,
        width - 90,
    )
    b2 = d.textbbox((0, 0), head2, font=f2)
    y2 = y1 + (72 if not story else 94)
    d.text(
        ((width - (b2[2] - b2[0])) // 2, y2),
        head2,
        font=f2,
        fill=(255, 255, 255) if head1 == "ТОП-5" else (55, 151, 255),
    )

    draw_subbrand(
        d,
        width,
        y2 + (88 if not story else 112),
        "ПЛАТИТЕ ТОЛЬКО ЗА ТАРИФ",
        story,
    )

    # Ровно пять строк подряд на одной картинке.
    shown = items[:5]
    top = 385 if not story else 590
    bottom = height - (175 if not story else 280)
    left, right = 88, width - 88
    gap = 14 if not story else 20
    row_h = (bottom - top - gap * (len(shown) - 1)) // max(1, len(shown))

    for idx, item in enumerate(shown):
        y = top + idx * (row_h + gap)
        box = (left, y, right, y + row_h)
        neon_box(img, box, 24 if not story else 30)
        d = ImageDraw.Draw(img)

        phone = format_phone(item.phone).replace("+7 (", "").replace(") ", " ")
        pf = fit_font(
            d,
            phone,
            FONT_MONO,
            54 if not story else 68,
            30 if not story else 38,
            (right - left) - 80,
        )
        pb = d.textbbox((0, 0), phone, font=pf)
        px = (width - (pb[2] - pb[0])) // 2
        py = y + (row_h - (pb[3] - pb[1])) // 2 - 4
        d.text((px, py), phone, font=pf, fill=(255, 255, 255))

    draw_cta(d, width, height, story)
    img.convert("RGB").save(target, "PNG", optimize=True)



def tariff_benefits(price: int) -> tuple[int, int, int]:
    if price <= 950:
        return 1000, 1000, 1000
    if price <= 2000:
        return 2000, 1000, 1000
    return 3000, 1000, 1000


def _city_background(width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height), (2, 7, 20, 255))
    d = ImageDraw.Draw(img)

    for y in range(height):
        t = y / max(1, height - 1)
        d.line(
            (0, y, width, y),
            fill=(3, int(10 + 15 * (1 - t)), int(28 + 34 * (1 - t)), 255),
        )

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    horizon = int(height * 0.45)
    gd.ellipse(
        (80, horizon - 190, width - 80, horizon + 190),
        fill=(0, 110, 255, 70),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)

    rng = random.Random(96)
    for _ in range(120):
        x = rng.randint(15, width - 15)
        y = rng.randint(80, int(height * 0.52))
        rr = rng.choice([1, 1, 2])
        d.ellipse(
            (x - rr, y - rr, x + rr, y + rr),
            fill=(70, 155, 255, rng.randint(50, 130)),
        )

    base_y = int(height * 0.56)
    x = 0
    while x < width:
        bw = rng.randint(48, 105)
        bh = rng.randint(85, 255)
        top = base_y - bh
        fill = rng.choice(
            [(4, 18, 40, 255), (5, 22, 49, 255), (7, 27, 57, 255)]
        )
        d.rectangle((x, top, min(width, x + bw), base_y), fill=fill)
        if rng.random() < 0.35:
            d.polygon(
                [(x, top), (x + bw // 2, top - rng.randint(15, 35)), (x + bw, top)],
                fill=fill,
            )
        for wx in range(x + 12, min(width, x + bw - 8), 18):
            for wy in range(top + 18, base_y - 10, 24):
                if rng.random() < 0.55:
                    d.rectangle(
                        (wx, wy, wx + 5, wy + 8),
                        fill=(34, 111, 210, rng.randint(80, 190)),
                    )
        x += bw + rng.randint(4, 12)

    # Stylized central mosque/city silhouette.
    cx = width // 2
    dome_y = int(height * 0.37)
    d.rectangle((cx - 135, dome_y + 110, cx + 135, base_y + 5), fill=(4, 15, 34, 255))
    d.ellipse((cx - 108, dome_y + 20, cx + 108, dome_y + 185), fill=(5, 21, 45, 255))
    d.polygon(
        [(cx - 8, dome_y + 25), (cx, dome_y - 28), (cx + 8, dome_y + 25)],
        fill=(28, 93, 178, 255),
    )

    for mx in (cx - 175, cx + 175):
        d.rectangle((mx - 12, dome_y + 40, mx + 12, base_y + 5), fill=(4, 17, 38, 255))
        d.polygon(
            [(mx - 11, dome_y + 40), (mx, dome_y - 28), (mx + 11, dome_y + 40)],
            fill=(13, 60, 120, 255),
        )

    d.line((0, base_y, width, base_y), fill=(41, 128, 244, 150), width=2)

    fade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fade)
    for y in range(int(height * 0.50), height):
        t = (y - height * 0.50) / (height * 0.50)
        fd.line((0, y, width, y), fill=(0, 3, 12, int(40 + 155 * t)))

    return Image.alpha_composite(img, fade)


def _draw_city_brand(
    d: ImageDraw.ImageDraw,
    width: int,
    y: int,
    story: bool,
) -> None:
    brand = "Номера"
    bf = font(FONT_BOLD, 72 if not story else 88)
    nf = font(FONT_BOLD, 80 if not story else 98)
    bb = d.textbbox((0, 0), brand, font=bf)
    nb = d.textbbox((0, 0), "96", font=nf)
    total = (bb[2] - bb[0]) + (nb[2] - nb[0]) + 8
    x = (width - total) // 2
    d.text((x, y), brand, font=bf, fill=(250, 252, 255))
    d.text((x + (bb[2] - bb[0]) + 8, y - 5), "96", font=nf, fill=(25, 132, 255))


def render_city_single(
    item: NumberItem,
    target: Path,
    *,
    premium: bool = False,
    story: bool = False,
) -> None:
    width, height = (1080, 1920) if story else (1080, 1080)
    img = _city_background(width, height)
    d = ImageDraw.Draw(img)

    _draw_city_brand(d, width, 54 if not story else 115, story)

    label = "ПРЕМИУМ НОМЕР" if premium else "КРАСИВЫЙ НОМЕР"
    lf = fit_font(d, label, FONT_BOLD, 42 if not story else 58, 28, width - 130)
    lb = d.textbbox((0, 0), label, font=lf)
    ly = 150 if not story else 245
    d.text(
        ((width - (lb[2] - lb[0])) // 2, ly),
        label,
        font=lf,
        fill=(235, 244, 255),
    )

    phone_top = 500 if not story else 875
    phone_h = 160 if not story else 220
    phone_box = (55, phone_top, width - 55, phone_top + phone_h)
    neon_box(img, phone_box, 38)
    d = ImageDraw.Draw(img)

    phone = format_phone(item.phone)
    pf = fit_font(
        d,
        phone,
        FONT_MONO,
        74 if not story else 96,
        42 if not story else 52,
        width - 150,
    )
    pb = d.textbbox((0, 0), phone, font=pf)
    d.text(
        (
            (width - (pb[2] - pb[0])) // 2,
            phone_box[1] + (phone_h - (pb[3] - pb[1])) // 2 - 8,
        ),
        phone,
        font=pf,
        fill=(255, 255, 255),
    )

    minutes, internet, sms = tariff_benefits(item.price)
    stats = [
        ("ТАРИФ", f"{item.price:,} ₽".replace(",", " ")),
        ("МИНУТЫ", f"{minutes:,}".replace(",", " ")),
        ("ИНТЕРНЕТ", f"{internet} ГБ"),
        ("SMS", f"{sms:,}".replace(",", " ")),
    ]

    stats_top = phone_box[3] + (30 if not story else 50)
    gap = 14 if not story else 20
    box_w = (width - 110 - gap * 3) // 4
    box_h = 145 if not story else 200

    for i, (name, value) in enumerate(stats):
        x1 = 55 + i * (box_w + gap)
        y1 = stats_top
        x2 = x1 + box_w
        y2 = y1 + box_h
        neon_box(img, (x1, y1, x2, y2), 24)
        d = ImageDraw.Draw(img)

        nf = fit_font(d, name, FONT_BOLD, 20 if not story else 26, 13, box_w - 20)
        nb = d.textbbox((0, 0), name, font=nf)
        d.text(
            ((x1 + x2 - (nb[2] - nb[0])) // 2, y1 + 22),
            name,
            font=nf,
            fill=(91, 185, 255),
        )

        vf = fit_font(d, value, FONT_BOLD, 35 if not story else 46, 21, box_w - 18)
        vb = d.textbbox((0, 0), value, font=vf)
        d.text(
            (
                (x1 + x2 - (vb[2] - vb[0])) // 2,
                y1 + (72 if not story else 100),
            ),
            value,
            font=vf,
            fill=(255, 255, 255),
        )

    cta = "ПИШИ В TELEGRAM / WHATSAPP"
    cf = fit_font(d, cta, FONT_BOLD, 34 if not story else 46, 21, width - 150)
    cb = d.textbbox((0, 0), cta, font=cf)
    cy = height - 100 if not story else height - 185
    d.text(
        ((width - (cb[2] - cb[0])) // 2, cy),
        cta,
        font=cf,
        fill=(246, 250, 255),
    )

    sf = font(FONT_BOLD, 17 if not story else 24)
    sub = "ОФОРМЛЕНИЕ ОНЛАЙН • НОМЕР БЕСПЛАТНО"
    sb = d.textbbox((0, 0), sub, font=sf)
    d.text(
        (
            (width - (sb[2] - sb[0])) // 2,
            cy + (48 if not story else 64),
        ),
        sub,
        font=sf,
        fill=(67, 157, 255),
    )

    img.convert("RGB").save(target, "PNG", optimize=True)



SINGLE_TEMPLATE_FILE = ROOT / "nomera96_single_template.png"


def _template_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(path, size)


def _draw_centered_text(
    d: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    value: str,
    font_obj: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    x1, y1, x2, y2 = box
    bb = d.textbbox((0, 0), value, font=font_obj)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    d.text(
        (x1 + (x2 - x1 - tw) // 2, y1 + (y2 - y1 - th) // 2 - bb[1]),
        value,
        font=font_obj,
        fill=fill,
    )



def render_five_number_story(items: list[NumberItem], target: Path) -> None:
    """Approved blue Nomera96 Story artwork with 5 live numbers."""
    if not STORY_FIVE_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Не найден шаблон Stories: {STORY_FIVE_TEMPLATE_PATH}")
    if len(items) < 5:
        raise ValueError("Для Stories нужно 5 номеров.")

    base = Image.open(STORY_FIVE_TEMPLATE_PATH).convert("RGB")
    draw = ImageDraw.Draw(base)
    w, h = base.size
    sx, sy = w / 1024.0, h / 1536.0
    row_y = [530, 666, 802, 938, 1074]
    nf = _template_font(max(30, int(40*sx)), True)
    tf = _template_font(max(27, int(36*sx)), True)
    sf = _template_font(max(13, int(16*sx)), True)

    def B(x1,y1,x2,y2):
        return (int(x1*sx),int(y1*sy),int(x2*sx),int(y2*sy))

    for i,item in enumerate(items[:5]):
        y=row_y[i]
        draw.rounded_rectangle(B(150,y-38,675,y+58),radius=max(8,int(18*sx)),fill=(3,12,29))
        draw.rounded_rectangle(B(685,y-38,900,y+58),radius=max(8,int(18*sx)),fill=(3,12,29))
        phone=format_phone(item.phone)
        if phone.startswith("+7 "): phone=phone[3:]
        draw.text((int(185*sx),int((y-18)*sy)),phone,font=nf,fill=(248,251,255))
        draw.text((int(735*sx),int((y-29)*sy)),"ТАРИФ",font=sf,fill=(248,251,255))
        draw.text((int(725*sx),int((y-2)*sy)),f"{item.price}₽",font=tf,fill=(35,150,255))
        draw.text((int(750*sx),int((y+39)*sy)),"/ МЕС",font=sf,fill=(248,251,255))

    target.parent.mkdir(parents=True,exist_ok=True)
    base.save(target,"PNG",optimize=True)


def select_story_five(catalog: list[tuple[str, int]], used: set[str]) -> list[NumberItem]:
    """Five varied affordable numbers for one Story."""
    pool=[x for x in build_items(catalog) if x.phone not in used]
    if len(pool)<5:
        pool=build_items(catalog)

    result=[]
    # Prefer affordable tariff diversity first.
    for target_price in (550, 750, 399, 550, 750, 950):
        candidates=[x for x in pool if x.price==target_price and x.phone not in {r.phone for r in result}]
        candidates.sort(key=lambda x:x.beauty,reverse=True)
        if candidates:
            result.append(candidates[0])
        if len(result)==5:
            return result

    rest=[x for x in pool if x.phone not in {r.phone for r in result}]
    rest.sort(key=lambda x:(x.price>950, x.price, -x.beauty))
    result.extend(rest[:5-len(result)])
    return result[:5]


def render_exact_single_template(
    item: NumberItem,
    target: Path,
    story: bool = False,
) -> None:
    """
    The square post uses the approved static Nomera96 artwork.
    Only live number/tariff values are overlaid.
    Telegram and Instagram receive this exact same post.png.
    """
    if not SINGLE_TEMPLATE_FILE.exists():
        raise FileNotFoundError(
            "Нет nomera96_single_template.png рядом с bot.py."
        )

    base = Image.open(SINGLE_TEMPLATE_FILE).convert("RGB")
    d = ImageDraw.Draw(base)

    # Phone panel: cover old sample digits, preserving the original neon frame.
    d.rounded_rectangle(
        (105, 452, 1148, 586),
        radius=34,
        fill=(2, 10, 23),
    )

    phone = format_phone(item.phone)
    phone_font = _template_font(86, True)
    while d.textbbox((0, 0), phone, font=phone_font)[2] > 990:
        phone_font = _template_font(phone_font.size - 2, True)

    # Last 2 digits in blue when possible.
    prefix = phone[:-2]
    suffix = phone[-2:]
    pre_bb = d.textbbox((0, 0), prefix, font=phone_font)
    suf_bb = d.textbbox((0, 0), suffix, font=phone_font)
    total_w = (pre_bb[2] - pre_bb[0]) + (suf_bb[2] - suf_bb[0])
    start_x = (1254 - total_w) // 2
    y = 476
    d.text((start_x, y), prefix, font=phone_font, fill=(255, 255, 255))
    d.text(
        (start_x + (pre_bb[2] - pre_bb[0]), y),
        suffix,
        font=phone_font,
        fill=(24, 134, 255),
    )

    # Tariff price.
    d.rounded_rectangle((455, 688, 802, 756), radius=18, fill=(2, 10, 23))
    tariff_text = f"{item.price:,} ₽ / мес".replace(",", " ")
    tf = _template_font(43, True)
    _draw_centered_text(d, (455, 688, 802, 756), tariff_text, tf, (255, 255, 255))

    # Real tariff parameters from Bezlimit API.
    meta = tariff_meta_for(item.phone)
    values = [
        ("—" if meta.get("minutes") is None else f"{int(meta['minutes']):,}".replace(",", " ")),
        ("—" if meta.get("internet") is None else f"{int(meta['internet']):,}".replace(",", " ")),
        ("—" if meta.get("sms") is None else f"{int(meta['sms']):,}".replace(",", " ")),
    ]

    value_boxes = [
        (250, 795, 420, 855),
        (570, 795, 735, 855),
        (935, 795, 1085, 855),
    ]
    for box, value in zip(value_boxes, values):
        d.rectangle(box, fill=(2, 10, 23))
        vf = _template_font(36, True)
        _draw_centered_text(d, box, value, vf, (250, 252, 255))

    # Correct the contact footer: no fake bot/phone from the visual mock.
    d.rounded_rectangle((82, 1030, 1172, 1162), radius=22, fill=(2, 10, 23))
    footer1 = "ПИШИ В DIRECT / WHATSAPP"
    footer2 = "ОФОРМЛЕНИЕ ОНЛАЙН • НОМЕР БЕСПЛАТНО"
    f1 = _template_font(36, True)
    f2 = _template_font(20, True)
    _draw_centered_text(d, (100, 1042, 1155, 1104), footer1, f1, (250, 252, 255))
    _draw_centered_text(d, (100, 1105, 1155, 1146), footer2, f2, (37, 145, 255))

    if not story:
        base.save(target, "PNG", optimize=True)
        return

    # Story 9:16 — same approved artwork, extended vertically.
    canvas = Image.new("RGB", (1080, 1920), (1, 6, 17))

    square = base.resize((1080, 1080), Image.Resampling.LANCZOS)
    canvas.paste(square, (0, 300))

    sd = ImageDraw.Draw(canvas)

    top_title = "КРАСИВЫЙ НОМЕР"
    tf = _template_font(48, True)
    tb = sd.textbbox((0, 0), top_title, font=tf)
    sd.text(
        ((1080 - (tb[2] - tb[0])) // 2, 150),
        top_title,
        font=tf,
        fill=(255, 255, 255),
    )

    top_sub = "НОМЕР БЕСПЛАТНО • ПЛАТИТЕ ТОЛЬКО ЗА ТАРИФ"
    sf = _template_font(25, True)
    sb = sd.textbbox((0, 0), top_sub, font=sf)
    sd.text(
        ((1080 - (sb[2] - sb[0])) // 2, 220),
        top_sub,
        font=sf,
        fill=(42, 148, 255),
    )

    bottom = "ПИШИ В DIRECT / WHATSAPP"
    bf = _template_font(42, True)
    bb = sd.textbbox((0, 0), bottom, font=bf)
    sd.text(
        ((1080 - (bb[2] - bb[0])) // 2, 1510),
        bottom,
        font=bf,
        fill=(255, 255, 255),
    )

    sub2 = "ОФОРМЛЕНИЕ ОНЛАЙН"
    sf2 = _template_font(28, True)
    sb2 = sd.textbbox((0, 0), sub2, font=sf2)
    sd.text(
        ((1080 - (sb2[2] - sb2[0])) // 2, 1580),
        sub2,
        font=sf2,
        fill=(42, 148, 255),
    )

    canvas.save(target, "PNG", optimize=True)


def render_selection(

    items: list[NumberItem],
    target: Path,
    title: str,
    subtitle: str,
    story: bool = False,
) -> None:
    if len(items) == 1:
        render_exact_single_template(items[0], target, story=story)
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
    story_items: list[NumberItem] | None = None,
) -> tuple[Path, Path, str]:
    folder = output_folder(prefix)
    post = folder / "post.png"
    story = folder / "story.png"
    render_selection(items, post, title, subtitle, False)
    if story_items and len(story_items) >= 5:
        render_five_number_story(story_items[:5], story)
    else:
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
    catalog = await get_live_catalog(bot, chat_id, force=False)
    if not catalog:
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
# Bezlimit API
# =========================

class BezlimitApiError(RuntimeError):
    pass


def _normalized_auth(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.lower().startswith("basic "):
        return value
    return "Basic " + value


def bezlimit_api_configured() -> bool:
    return bool(BEZLIMIT_AUTH and BEZLIMIT_API_TOKEN)


def _api_headers() -> dict[str, str]:
    if not bezlimit_api_configured():
        raise BezlimitApiError(
            "Не заданы BEZLIMIT_AUTH и BEZLIMIT_API_TOKEN в Railway Variables."
        )
    return {
        "Accept": "application/json",
        "Authorization": _normalized_auth(BEZLIMIT_AUTH),
        "api-token": BEZLIMIT_API_TOKEN,
        "Origin": "https://store.bezlimit.ru",
        "Referer": "https://store.bezlimit.ru/",
        "User-Agent": "Mozilla/5.0 Nomera96Bot/12",
    }


def _http_json(path: str, params: dict[str, object] | None = None) -> object:
    params = params or {}
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{BEZLIMIT_API_BASE}{path}"
    if query:
        url += "?" + query

    req = urllib.request.Request(
        url,
        headers=_api_headers(),
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            if response.status != 200:
                raise BezlimitApiError(f"HTTP {response.status}")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise BezlimitApiError(
                f"API вернул HTTP {exc.code}: проверь BEZLIMIT_AUTH и BEZLIMIT_API_TOKEN."
            ) from exc
        raise BezlimitApiError(f"API вернул HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise BezlimitApiError(f"Нет соединения с API: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise BezlimitApiError("API вернул некорректный JSON.") from exc


def _convert_api_item(raw: dict) -> dict | None:
    try:
        phone = normalize_phone(str(raw.get("phone", "")))
        if not phone:
            return None

        if raw.get("reservation") is not None:
            return None

        status = raw.get("status")
        if status is not None and int(status) != 2:
            return None

        type_category = str(raw.get("type_category", ""))
        if type_category and type_category != "standard":
            return None

        price_params = raw.get("priceParams") or {}
        mask_price = int(price_params.get("mask_price") or 0)
        if mask_price != 0:
            return None

        tariff = raw.get("tariff") or {}
        tariff_price = int(tariff.get("price") or 0)
        if tariff_price <= 0:
            return None

        def _num(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return {
            "phone": phone,
            "price": tariff_price,
            "minutes": _num(tariff.get("minutes")),
            "internet": _num(tariff.get("internet")),
            "sms": _num(tariff.get("sms")),
            "tariff_name": str(tariff.get("name") or ""),
        }
    except (TypeError, ValueError):
        return None


def fetch_bezlimit_catalog() -> list[dict]:
    """
    Получаем случайную выборку свободных стандартных номеров.
    Сохраняем и параметры тарифа, чтобы карточка показывала реальные данные.
    """
    found: dict[str, dict] = {}

    for page in range(1, BEZLIMIT_FETCH_PAGES + 1):
        payload = _http_json(
            "/v2/phones",
            {
                "type": "standard",
                "sort": "random",
                "page": page,
                "per_page": BEZLIMIT_PER_PAGE,
                "expand": "reservation,tariff,region,mask,priceParams",
            },
        )

        if not isinstance(payload, dict):
            raise BezlimitApiError("Неожиданный формат /v2/phones.")

        items = payload.get("items")
        if not isinstance(items, list):
            raise BezlimitApiError("В ответе API отсутствует items.")

        for raw in items:
            if not isinstance(raw, dict):
                continue
            converted = _convert_api_item(raw)
            if converted:
                found[converted["phone"]] = converted

    if not found:
        raise BezlimitApiError(
            "API ответил, но свободных стандартных номеров в выборке не найдено."
        )

    return list(found.values())


def _cache_is_fresh(state: dict) -> bool:
    raw = state.get("api_updated_at")
    if not raw:
        return False
    try:
        updated = datetime.fromisoformat(raw)
        now = datetime.now(TZ)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=TZ)
        return now - updated < timedelta(minutes=BEZLIMIT_CACHE_MINUTES)
    except Exception:
        return False


def refresh_catalog_from_api(force: bool = False) -> tuple[list[tuple[str, int]], bool]:
    """
    Returns (simple_catalog, fetched_now).
    Rich tariff metadata stays in state["catalog"] for rendering.
    """
    state = load_state()

    if not force and _cache_is_fresh(state) and state.get("catalog"):
        return get_catalog(state), False

    rows = fetch_bezlimit_catalog()

    state["catalog"] = rows
    state["api_updated_at"] = datetime.now(TZ).isoformat()
    state["api_last_error"] = None
    save_state(state)

    return get_catalog(state), True


def tariff_meta_for(phone: str) -> dict:
    state = load_state()
    for row in state.get("catalog", []):
        if str(row.get("phone")) == phone:
            return {
                "minutes": row.get("minutes"),
                "internet": row.get("internet"),
                "sms": row.get("sms"),
                "tariff_name": row.get("tariff_name") or "",
            }
    return {}


async def get_live_catalog(
    bot: Bot,
    chat_id: int,
    force: bool = False,
) -> list[tuple[str, int]] | None:
    if not bezlimit_api_configured():
        await bot.send_message(
            chat_id,
            "⚠️ API Безлимита ещё не подключён.\n\n"
            "В Railway → Variables добавь:\n"
            "BEZLIMIT_AUTH\n"
            "BEZLIMIT_API_TOKEN\n\n"
            "Значения можно получить скриптом extract_bezlimit_keys.py из архива v12.",
        )
        return None

    try:
        catalog, fetched_now = await asyncio.to_thread(
            refresh_catalog_from_api,
            force,
        )
        if fetched_now:
            await bot.send_message(
                chat_id,
                f"🔄 Получил свежие номера из API Безлимита: {len(catalog)} шт.",
            )
        return catalog
    except Exception as exc:
        logging.exception("Bezlimit API error")
        state = load_state()
        state["api_last_error"] = f"{type(exc).__name__}: {exc}"
        save_state(state)

        # Если API временно упал, можно использовать последний успешный кэш.
        cached = get_catalog(state)
        if cached:
            await bot.send_message(
                chat_id,
                f"⚠️ API временно недоступен: {exc}\n"
                f"Использую последний кэш: {len(cached)} номеров.",
            )
            return cached

        await bot.send_message(
            chat_id,
            f"❌ Не удалось получить номера из API Безлимита:\n{exc}",
        )
        return None




# =========================
# Instagram / Meta publishing
# =========================

class InstagramPublishError(RuntimeError):
    pass


def instagram_configured() -> bool:
    return bool(IG_ACCESS_TOKEN and PUBLIC_BASE_URL)


def _graph_url(path: str) -> str:
    return f"https://graph.instagram.com/{META_GRAPH_VERSION}/{path.lstrip('/')}"


def _post_form_json(url: str, fields: dict[str, object]) -> dict:
    encoded = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {IG_ACCESS_TOKEN}",
            "User-Agent": "Nomera96Bot/13.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            msg = (
                parsed.get("error", {}).get("message")
                if isinstance(parsed, dict)
                else body
            )
        except Exception:
            msg = f"HTTP {exc.code}"
        raise InstagramPublishError(str(msg or f"HTTP {exc.code}")) from exc
    except urllib.error.URLError as exc:
        raise InstagramPublishError(f"Нет соединения с Meta: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise InstagramPublishError("Meta вернула некорректный JSON.") from exc

    if isinstance(payload, dict) and payload.get("error"):
        raise InstagramPublishError(
            str(payload["error"].get("message") or payload["error"])
        )
    if not isinstance(payload, dict):
        raise InstagramPublishError("Неожиданный ответ Meta API.")
    return payload


def _safe_public_name(source: Path) -> str:
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S_%f")
    return f"nomera96_{stamp}{source.suffix.lower() or '.png'}"


def expose_media_file(source: Path) -> tuple[Path, str]:
    if not PUBLIC_BASE_URL:
        raise InstagramPublishError(
            "Нет PUBLIC_BASE_URL/RAILWAY_PUBLIC_DOMAIN. "
            "В Railway нужно включить Public Networking → Generate Domain."
        )

    if not source.exists():
        raise InstagramPublishError("Файл поста уже недоступен.")

    name = _safe_public_name(source)
    dest = PUBLIC_MEDIA_DIR / name
    shutil.copy2(source, dest)
    url = f"{PUBLIC_BASE_URL}/media/{urllib.parse.quote(name)}"
    return dest, url


def _get_json(url: str, params: dict[str, object]) -> dict:
    query = urllib.parse.urlencode(params, doseq=True)
    full_url = url + ("?" if "?" not in url else "&") + query
    req = urllib.request.Request(
        full_url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {IG_ACCESS_TOKEN}",
            "User-Agent": "Nomera96Bot/13.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            msg = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else body
        except Exception:
            msg = f"HTTP {exc.code}"
        raise InstagramPublishError(str(msg or f"HTTP {exc.code}")) from exc
    except urllib.error.URLError as exc:
        raise InstagramPublishError(f"Нет соединения с Instagram API: {exc.reason}") from exc

    if not isinstance(payload, dict):
        raise InstagramPublishError("Неожиданный ответ Instagram API.")
    if payload.get("error"):
        raise InstagramPublishError(str(payload["error"].get("message") or payload["error"]))
    return payload


def resolve_ig_user_id() -> tuple[str, str]:
    if not IG_ACCESS_TOKEN:
        raise InstagramPublishError("Не задан IG_ACCESS_TOKEN.")

    payload = _get_json(
        _graph_url("me"),
        {
            "fields": "id,username",
            "access_token": IG_ACCESS_TOKEN,
        },
    )
    user_id = str(payload.get("id") or "")
    username = str(payload.get("username") or "")
    if not user_id:
        raise InstagramPublishError("Не удалось определить Instagram User ID по токену.")
    return user_id, username


def wait_for_instagram_container(
    creation_id: str,
    timeout_seconds: int = 180,
    poll_seconds: int = 10,
) -> str:
    """Wait until Instagram finishes processing the media container."""
    deadline = time.monotonic() + timeout_seconds
    last_status = "UNKNOWN"

    while time.monotonic() < deadline:
        payload = _get_json(
            _graph_url(creation_id),
            {
                "fields": "id,status_code",
                "access_token": IG_ACCESS_TOKEN,
            },
        )
        last_status = str(payload.get("status_code") or "UNKNOWN").upper()

        if last_status in {"FINISHED", "PUBLISHED"}:
            return last_status

        if last_status in {"ERROR", "EXPIRED"}:
            raise InstagramPublishError(
                f"Контейнер Instagram завершился со статусом {last_status}."
            )

        time.sleep(poll_seconds)

    raise InstagramPublishError(
        f"Instagram слишком долго обрабатывает изображение "
        f"(последний статус: {last_status}). Попробуй публикацию ещё раз."
    )


def publish_image_to_instagram(image_url: str, caption: str) -> str:
    if not IG_ACCESS_TOKEN:
        raise InstagramPublishError(
            "Не задан IG_ACCESS_TOKEN в Railway Variables."
        )

    ig_user_id = IG_USER_ID
    if not ig_user_id:
        ig_user_id, _username = resolve_ig_user_id()

    created = _post_form_json(
        _graph_url(f"{ig_user_id}/media"),
        {
            "image_url": image_url,
            "caption": caption,
            "access_token": IG_ACCESS_TOKEN,
        },
    )
    creation_id = str(created.get("id") or "")
    if not creation_id:
        raise InstagramPublishError("Meta не вернула creation_id.")

    # Wait until Instagram has actually finished processing the image.
    # Publishing too early may return: "Media ID is not available".
    wait_for_instagram_container(creation_id)

    try:
        published = _post_form_json(
            _graph_url(f"{ig_user_id}/media_publish"),
            {
                "creation_id": creation_id,
                "access_token": IG_ACCESS_TOKEN,
            },
        )
    except InstagramPublishError as exc:
        # Rare race: FINISHED can appear just before media_publish is ready.
        if "media id is not available" not in str(exc).lower():
            raise
        time.sleep(10)
        published = _post_form_json(
            _graph_url(f"{ig_user_id}/media_publish"),
            {
                "creation_id": creation_id,
                "access_token": IG_ACCESS_TOKEN,
            },
        )

    media_id = str(published.get("id") or "")
    if not media_id:
        raise InstagramPublishError("Meta не вернула ID опубликованного поста.")
    return media_id


def publish_story_to_instagram(image_url: str) -> str:
    if not IG_ACCESS_TOKEN:
        raise InstagramPublishError("Не задан IG_ACCESS_TOKEN.")

    ig_user_id = IG_USER_ID
    if not ig_user_id:
        ig_user_id, _username = resolve_ig_user_id()

    created = _post_form_json(
        _graph_url(f"{ig_user_id}/media"),
        {
            "image_url": image_url,
            "media_type": "STORIES",
            "access_token": IG_ACCESS_TOKEN,
        },
    )
    creation_id = str(created.get("id") or "")
    if not creation_id:
        raise InstagramPublishError("Instagram не вернул creation_id для Stories.")

    wait_for_instagram_container(creation_id)

    try:
        published = _post_form_json(
            _graph_url(f"{ig_user_id}/media_publish"),
            {
                "creation_id": creation_id,
                "access_token": IG_ACCESS_TOKEN,
            },
        )
    except InstagramPublishError as exc:
        if "media id is not available" not in str(exc).lower():
            raise
        time.sleep(10)
        published = _post_form_json(
            _graph_url(f"{ig_user_id}/media_publish"),
            {
                "creation_id": creation_id,
                "access_token": IG_ACCESS_TOKEN,
            },
        )

    media_id = str(published.get("id") or "")
    if not media_id:
        raise InstagramPublishError("Instagram не вернул Media ID Stories.")
    return media_id


def cleanup_public_media(max_age_hours: int = 24) -> None:
    cutoff = datetime.now().timestamp() - max_age_hours * 3600
    for path in PUBLIC_MEDIA_DIR.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except Exception:
            logging.exception("Не удалось удалить старый public media %s", path)


async def media_handler(request: web.Request) -> web.StreamResponse:
    name = request.match_info.get("name", "")
    # Не разрешаем path traversal.
    safe = Path(name).name
    if safe != name:
        raise web.HTTPNotFound()
    path = PUBLIC_MEDIA_DIR / safe
    if not path.exists() or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "service": "nomera96-bot",
            "version": BUILD_VERSION,
            "instagram_configured": instagram_configured(),
        }
    )


async def start_public_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/media/{name}", media_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Public media server listening on 0.0.0.0:%s", PORT)
    return runner


def instagram_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📲 Опубликовать пост",
                    callback_data="ig:publish",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📱 Опубликовать Stories",
                    callback_data="ig:story",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚀 Пост + Stories",
                    callback_data="ig:both",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Только файлы",
                    callback_data="ig:skip",
                )
            ],
        ]
    )


# =========================
# Telegram UI
# =========================

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Красивый номер")],
        [KeyboardButton(text="📸 Автопубликация")],
        [KeyboardButton(text="📚 Все номера по тарифам")],
        [KeyboardButton(text="🎬 Reels")],
        [KeyboardButton(text="👀 Stories")],
        [
            KeyboardButton(text="🔄 Обновить из Безлимит"),
            KeyboardButton(text="📊 Статус"),
        ],
        [KeyboardButton(text="🔗 Суперссылка")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выбери действие",
)


MODE_META = {
    "manual_cheap": ("КРАСИВЫЙ НОМЕР", "красивый номер на доступном тарифе до 950 ₽"),
    "feed": ("КРАСИВЫЙ НОМЕР", "один сильный номер с приоритетом доступного тарифа"),
    "top5": ("ТОП-5 КРАСИВЫХ НОМЕРОВ", "самые запоминающиеся номера из каталога"),
    "budget": ("КРАСИВЫЕ ДО 1000 ₽", "доступные тарифы и красивые комбинации"),
    "premium": ("ПРЕМИУМ НОМЕР", "один сильный номер в эффектном городском стиле"),
    "single": ("ОТДЕЛЬНЫЙ ПОСТ", "один красивый номер для отдельной публикации"),
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
    catalog = await get_live_catalog(bot, chat_id, force=False)
    if not catalog:
        return False

    # state перечитываем после возможного API refresh
    state = load_state()
    used = set(state.get("used", []))

    if rotate and state.get("draft"):
        used.update(str(x["phone"]) for x in state["draft"])

    items = select_mode(catalog, mode, used, 1 if mode in {"feed", "manual_cheap"} else 5)
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
# Instagram autopublish core
# =========================

def _format_auto_status(state: dict) -> str:
    enabled = bool(state.get("ig_auto_enabled"))
    return (
        "📸 АВТОПУБЛИКАЦИЯ INSTAGRAM\n\n"
        f"Статус: {'ВКЛЮЧЕНА ✅' if enabled else 'ВЫКЛЮЧЕНА ⛔️'}\n\n"
        f"📱 Публикация 1 — {state.get('ig_auto_top5_time', '10:00')}\n"
        f"📱 Публикация 2 — {state.get('ig_auto_budget_time', '15:00')}\n"
        f"📱 Публикация 3 — {state.get('ig_auto_day_time', '19:00')}\n"
        "🌍 Часовой пояс — Москва\n\n"
        "Каждый выход — ОДИН красивый номер.\n"
        "Приоритет: 550 ₽ — 35% • 750 ₽ — 30% • 399 ₽ — 20% • 950 ₽ — 10% • остальные — 5%."
    )


def auto_publish_keyboard(state: dict) -> InlineKeyboardMarkup:
    enabled = bool(state.get("ig_auto_enabled"))
    t1 = state.get("ig_auto_top5_time", "10:00")
    t2 = state.get("ig_auto_budget_time", "15:00")
    t3 = state.get("ig_auto_day_time", "19:00")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⛔️ Выключить" if enabled else "✅ Включить автопубликацию",
                    callback_data="igauto:toggle",
                )
            ],
            [InlineKeyboardButton(text=f"🕒 Публикация 1 — {t1}", callback_data="igauto:edit:top5")],
            [InlineKeyboardButton(text=f"🕒 Публикация 2 — {t2}", callback_data="igauto:edit:budget")],
            [InlineKeyboardButton(text=f"🕒 Публикация 3 — {t3}", callback_data="igauto:edit:day")],
            [InlineKeyboardButton(text="🧪 Тест публикации сейчас", callback_data="igauto:test:day")],
        ]
    )


async def autopublish_mode(
    bot: Bot,
    chat_id: int,
    mode: str,
    *,
    notify: bool = True,
) -> str:
    if not instagram_configured():
        raise InstagramPublishError(
            "Instagram API не готов. Проверь /igstatus."
        )

    catalog = await get_live_catalog(bot, chat_id, force=True)
    if not catalog:
        raise RuntimeError("Не удалось получить свежий каталог Безлимита.")

    state = load_state()
    used = set(state.get("used", []))
    mode = "feed"
    items = select_mode(catalog, "feed", used, limit=1)
    if not items:
        # Если всё уже использовано, очищаем только историю used для выбора
        # и повторяем на свежем каталоге. Это защита от полной остановки автопилота.
        used = set()
        items = select_mode(catalog, "feed", used, limit=1)

    if not items:
        raise RuntimeError("API не дал подходящих номеров для публикации.")

    title, subtitle = MODE_META["feed"]
    post, story, caption = await asyncio.to_thread(
        create_selection_bundle,
        items,
        title,
        subtitle,
        f"auto_{mode}",
    )

    cleanup_public_media()
    public_path, public_url = await asyncio.to_thread(
        expose_media_file,
        Path(post),
    )
    await asyncio.sleep(1)

    media_id = await asyncio.to_thread(
        publish_image_to_instagram,
        public_url,
        caption,
    )

    # Сохраняем результат только после успешной публикации.
    state = load_state()
    used = set(state.get("used", []))
    used.update(item.phone for item in items)
    state["used"] = sorted(used)
    state["last_instagram_media_id"] = media_id
    state["last_instagram_error"] = None

    history = list(state.get("ig_auto_history", []))
    history.append({
        "at": datetime.now(TZ).isoformat(),
        "mode": mode,
        "phones": [x.phone for x in items],
        "media_id": media_id,
        "post": str(post),
        "public_media": str(public_path),
    })
    state["ig_auto_history"] = history[-50:]
    save_state(state)

    if notify:
        kind = "Красивый номер"
        phones = "\n".join(f"• {format_phone(x.phone)}" for x in items)
        await bot.send_message(
            chat_id,
            f"🤖 Автопилот опубликовал: {kind} ✅\n\n"
            f"{phones}\n\n"
            f"Instagram Media ID: {media_id}"
        )

    return media_id


async def instagram_autopilot_loop(bot: Bot) -> None:
    await asyncio.sleep(15)
    while True:
        try:
            state = load_state()
            if not state.get("ig_auto_enabled"):
                await asyncio.sleep(30)
                continue

            owner = get_owner_id()
            if not owner:
                await asyncio.sleep(30)
                continue

            now = datetime.now(TZ)
            today = now.date().isoformat()
            current_hm = now.strftime("%H:%M")

            top5_time = state.get("ig_auto_top5_time", "10:00")
            budget_time = state.get("ig_auto_budget_time", "15:00")
            day_time = state.get("ig_auto_day_time", "19:00")

            if (
                current_hm == top5_time
                and state.get("ig_auto_last_top5_date") != today
            ):
                try:
                    await autopublish_mode(bot, owner, "feed", notify=True)
                    state = load_state()
                    state["ig_auto_last_top5_date"] = today
                    save_state(state)
                except Exception as exc:
                    logging.exception("Auto TOP-5 failed")
                    state = load_state()
                    state["last_instagram_error"] = f"AUTO TOP5: {type(exc).__name__}: {exc}"
                    save_state(state)
                    await bot.send_message(owner, f"❌ Авто ТОП-5: {exc}")

            if (
                current_hm == budget_time
                and state.get("ig_auto_last_budget_date") != today
            ):
                try:
                    await autopublish_mode(bot, owner, "feed", notify=True)
                    state = load_state()
                    state["ig_auto_last_budget_date"] = today
                    save_state(state)
                except Exception as exc:
                    logging.exception("Auto Budget failed")
                    state = load_state()
                    state["last_instagram_error"] = f"AUTO BUDGET: {type(exc).__name__}: {exc}"
                    save_state(state)
                    await bot.send_message(owner, f"❌ Авто До 1000 ₽: {exc}")

            if (
                current_hm == day_time
                and state.get("ig_auto_last_day_date") != today
            ):
                try:
                    await autopublish_mode(bot, owner, "feed", notify=True)
                    state = load_state()
                    state["ig_auto_last_day_date"] = today
                    save_state(state)
                except Exception as exc:
                    logging.exception("Auto Number Day failed")
                    state = load_state()
                    state["last_instagram_error"] = f"AUTO DAY: {type(exc).__name__}: {exc}"
                    save_state(state)
                    await bot.send_message(owner, f"❌ Авто Номер дня: {exc}")

        except Exception:
            logging.exception("Instagram autopilot loop error")

        await asyncio.sleep(20)



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
        f"Кэш API: {total} номеров\n"
        f"Не использовано: {available}\n\n"
        "Теперь есть отдельный режим 🎬 Reels с готовыми кадрами и сценарием.",
        reply_markup=MENU,
    )



@router.message(F.text == "📱 Красивый номер")
async def beautiful_number_feed(message: Message, bot: Bot):
    if await deny(message):
        return
    await send_draft(bot, message.chat.id, "manual_cheap")


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


@router.message(F.text == "🌆 Отдельный пост")
async def single_city_post(message: Message, bot: Bot):
    if await deny(message):
        return
    await send_draft(bot, message.chat.id, "single")


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



@router.message(F.text == "🔄 Обновить из Безлимит")
async def refresh_from_bezlimit(message: Message, bot: Bot):
    if await deny(message):
        return

    status = await message.answer("🔄 Загружаю свежие номера из API Безлимита…")
    catalog = await get_live_catalog(bot, message.chat.id, force=True)
    if not catalog:
        await status.edit_text("❌ Обновление не выполнено.")
        return

    state = load_state()
    await status.edit_text(
        f"Готово ✅\n\n"
        f"Свежих свободных номеров в кэше: {len(catalog)}\n"
        f"Обновлено: {state.get('api_updated_at') or 'сейчас'}"
    )


@router.message(Command("apistatus"))
async def api_status(message: Message):
    if await deny(message):
        return

    state = load_state()
    configured = "ДА ✅" if bezlimit_api_configured() else "НЕТ ❌"
    total = len(get_catalog(state))
    await message.answer(
        "🔌 Bezlimit API\n\n"
        f"Настроен: {configured}\n"
        f"Кэш номеров: {total}\n"
        f"Последнее обновление: {state.get('api_updated_at') or 'нет'}\n"
        f"Последняя ошибка: {state.get('api_last_error') or 'нет'}"
    )



@router.message(F.text == "👀 Stories")
async def stories_menu(message: Message):
    if await deny(message):
        return
    state = load_state()
    _reset_stories_daily_counter(state)
    save_state(state)
    await message.answer(_stories_status_text(state), reply_markup=stories_keyboard(state))


@router.callback_query(F.data == "stories:toggle")
async def stories_toggle(callback: CallbackQuery):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    state = load_state()
    new_value = not bool(state.get("stories_enabled"))
    if new_value and not _stories_configured():
        await callback.answer("Сначала добавь логин и пароль Instagram в Railway", show_alert=True)
        return
    if new_value and not state.get("stories_usernames"):
        await callback.answer("Сначала добавь хотя бы один аккаунт", show_alert=True)
        return
    state["stories_enabled"] = new_value
    save_state(state)
    await callback.answer("Сохранено")
    if callback.message:
        await callback.message.edit_text(_stories_status_text(state), reply_markup=stories_keyboard(state))


@router.callback_query(F.data == "stories:add")
async def stories_add_prompt(callback: CallbackQuery):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    state = load_state()
    state["stories_awaiting"] = "add"
    save_state(state)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "➕ Пришли аккаунты одним сообщением. Можно через пробел или с новой строки.\n"
            "Пример: @account1 @account2"
        )


@router.callback_query(F.data == "stories:limit")
async def stories_limit_prompt(callback: CallbackQuery):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    state = load_state()
    state["stories_awaiting"] = "limit"
    save_state(state)
    await callback.answer()
    if callback.message:
        await callback.message.answer("🎯 Отправь дневной лимит числом от 1 до 200. Например: 30")


@router.callback_query(F.data == "stories:list")
async def stories_list(callback: CallbackQuery):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    state = load_state()
    usernames = list(state.get("stories_usernames", []))
    await callback.answer()
    text = "📋 Список пуст." if not usernames else "📋 Аккаунты:\n\n" + "\n".join(f"• @{u}" for u in usernames[:100])
    if len(usernames) > 100:
        text += f"\n\n…ещё {len(usernames) - 100}"
    if callback.message:
        await callback.message.answer(text)


@router.callback_query(F.data == "stories:clear")
async def stories_clear(callback: CallbackQuery):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    state = load_state()
    state["stories_usernames"] = []
    state["stories_enabled"] = False
    save_state(state)
    await callback.answer("Список очищен")
    if callback.message:
        await callback.message.edit_text(_stories_status_text(state), reply_markup=stories_keyboard(state))


@router.callback_query(F.data == "stories:test")
async def stories_test(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    state = load_state()
    if not _stories_configured():
        await callback.answer("Не настроены IG_STORIES_LOGIN / IG_STORIES_PASSWORD", show_alert=True)
        return
    if not state.get("stories_usernames"):
        await callback.answer("Список аккаунтов пуст", show_alert=True)
        return
    await callback.answer("Открываю Stories…")
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    try:
        done = await view_next_story_account(bot, chat_id, notify=True)
        if not done:
            await bot.send_message(chat_id, "ℹ️ На сегодня список уже обработан или достигнут лимит.")
    except Exception as exc:
        await bot.send_message(chat_id, f"❌ Stories: {exc}")


@router.message(F.text == "📸 Автопубликация")
async def instagram_auto_menu(message: Message):
    if await deny(message):
        return
    state = load_state()
    await message.answer(
        _format_auto_status(state),
        reply_markup=auto_publish_keyboard(state),
    )


@router.message(Command("autopublish"))
async def instagram_auto_command(message: Message):
    if await deny(message):
        return
    state = load_state()
    await message.answer(
        _format_auto_status(state),
        reply_markup=auto_publish_keyboard(state),
    )


@router.callback_query(F.data == "igauto:toggle")
async def instagram_auto_toggle(callback: CallbackQuery):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    state = load_state()
    new_value = not bool(state.get("ig_auto_enabled"))

    if new_value and not instagram_configured():
        await callback.answer(
            "Сначала настрой Instagram API: /igstatus",
            show_alert=True,
        )
        return

    state["ig_auto_enabled"] = new_value
    save_state(state)
    await callback.answer("Сохранено")
    if callback.message:
        await callback.message.edit_text(
            _format_auto_status(state),
            reply_markup=auto_publish_keyboard(state),
        )



@router.callback_query(F.data.startswith("igauto:edit:"))
async def instagram_auto_edit_time(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    mode = callback.data.rsplit(":", 1)[-1]
    labels = {
        "top5": "🔥 ТОП-5",
        "budget": "💰 До 1000 ₽",
        "day": "⭐ Номер дня",
    }
    if mode not in labels:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    state = load_state()
    state["ig_auto_awaiting_time_mode"] = mode
    save_state(state)

    await callback.answer()
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await bot.send_message(
        chat_id,
        f"🕒 Изменение времени: {labels[mode]}\\n\\n"
        "Отправь новое время одним сообщением в формате ЧЧ:ММ.\\n"
        "Например: 10:30",
    )


@router.message(F.text.regexp(r"^\\s*\\d{1,2}:\\d{2}\\s*$"))
async def instagram_auto_receive_time(message: Message):
    if await deny(message):
        return

    state = load_state()
    mode = state.get("ig_auto_awaiting_time_mode")
    if mode not in {"top5", "budget", "day"}:
        return

    value = (message.text or "").strip()
    try:
        _parse_hhmm(value)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    key_map = {
        "top5": "ig_auto_top5_time",
        "budget": "ig_auto_budget_time",
        "day": "ig_auto_day_time",
    }
    label_map = {
        "top5": "🔥 ТОП-5",
        "budget": "💰 До 1000 ₽",
        "day": "⭐ Номер дня",
    }

    state[key_map[mode]] = value
    state["ig_auto_awaiting_time_mode"] = None
    save_state(state)

    await message.answer(
        f"✅ {label_map[mode]} теперь будет публиковаться в {value} по Москве.",
        reply_markup=auto_publish_keyboard(state),
    )


@router.message(Command("schedule"))
async def instagram_schedule_command(message: Message):
    if await deny(message):
        return
    state = load_state()
    await message.answer(
        _format_auto_status(state),
        reply_markup=auto_publish_keyboard(state),
    )


@router.callback_query(F.data == "igauto:test:top5")
async def instagram_auto_test_top5(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("Запускаю тест ТОП-5…")
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    try:
        await autopublish_mode(bot, chat_id, "top5", notify=True)
    except Exception as exc:
        await bot.send_message(chat_id, f"❌ Тест ТОП-5: {exc}")



@router.callback_query(F.data == "igauto:test:budget")
async def instagram_auto_test_budget(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("Запускаю тест До 1000 ₽…")
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    try:
        await autopublish_mode(bot, chat_id, "budget", notify=True)
    except Exception as exc:
        await bot.send_message(chat_id, f"❌ Тест До 1000 ₽: {exc}")


@router.callback_query(F.data == "igauto:test:day")
async def instagram_auto_test_day(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("Запускаю тест Номер дня…")
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    try:
        await autopublish_mode(bot, chat_id, "day", notify=True)
    except Exception as exc:
        await bot.send_message(chat_id, f"❌ Тест Номер дня: {exc}")


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
        "Основной поток: один красивый номер с приоритетом доступных тарифов."
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
        "Источник: Bezlimit API • ТОП-5 одной картинкой • Instagram publish"
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
    catalog = await get_live_catalog(bot, callback.message.chat.id if callback.message else callback.from_user.id, force=False)
    story_items = select_story_five(catalog, set(state.get("used", []))) if catalog else []
    post, story, caption = await asyncio.to_thread(
        create_selection_bundle,
        items, title, subtitle, "approved", story_items,
    )

    used = set(state.get("used", []))
    used.update(x.phone for x in items)
    state["used"] = sorted(used)
    state["draft"] = []
    state["draft_mode"] = None
    state["last_approved"] = {
        "post": str(post),
        "story": str(story),
        "caption": caption,
        "mode": mode,
        "created_at": datetime.now(TZ).isoformat(),
        "phones": [x.phone for x in items],
    }
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

    if instagram_configured():
        await bot.send_message(
            chat_id,
            "Куда дальше?",
            reply_markup=instagram_keyboard(),
        )
    else:
        await bot.send_message(
            chat_id,
            "📲 Instagram-публикация уже встроена, но ещё не хватает "
            "IG_ACCESS_TOKEN или публичного домена Railway.\n"
            "Проверь командой /igstatus."
        )


@router.callback_query(F.data == "ig:publish")
async def publish_instagram(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    state = load_state()
    approved = state.get("last_approved") or {}
    post_raw = approved.get("post")
    caption = str(approved.get("caption") or "")

    if not post_raw:
        await callback.answer("Нет одобренного поста", show_alert=True)
        return

    if not instagram_configured():
        await callback.answer("Instagram API ещё не настроен", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "Нужны IG_ACCESS_TOKEN и публичный домен Railway.\n"
                "Проверка: /igstatus"
            )
        return

    await callback.answer("Публикую…")
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id

    try:
        cleanup_public_media()
        public_path, public_url = await asyncio.to_thread(
            expose_media_file,
            Path(post_raw),
        )

        # Даём Railway успеть начать отдавать файл по HTTPS.
        await asyncio.sleep(1)

        media_id = await asyncio.to_thread(
            publish_image_to_instagram,
            public_url,
            caption,
        )

        state = load_state()
        state["last_instagram_media_id"] = media_id
        state["last_instagram_error"] = None
        if isinstance(state.get("last_approved"), dict):
            state["last_approved"]["instagram_media_id"] = media_id
            state["last_approved"]["public_media"] = str(public_path)
        save_state(state)

        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

        await bot.send_message(
            chat_id,
            f"✅ Опубликовано в Instagram.\nMedia ID: {media_id}"
        )

    except Exception as exc:
        logging.exception("Instagram publish failed")
        state = load_state()
        state["last_instagram_error"] = f"{type(exc).__name__}: {exc}"
        save_state(state)
        await bot.send_message(
            chat_id,
            f"❌ Instagram не опубликовал пост:\n{exc}"
        )



@router.callback_query(F.data == "ig:story")
async def publish_instagram_story(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    state = load_state()
    approved = state.get("last_approved") or {}
    story_raw = approved.get("story")

    if not story_raw:
        await callback.answer("Нет готовой Stories", show_alert=True)
        return

    if not instagram_configured():
        await callback.answer("Instagram API ещё не настроен", show_alert=True)
        return

    await callback.answer("Публикую Stories…")
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id

    try:
        cleanup_public_media()
        _public_path, public_url = await asyncio.to_thread(
            expose_media_file,
            Path(story_raw),
        )
        await asyncio.sleep(1)

        media_id = await asyncio.to_thread(
            publish_story_to_instagram,
            public_url,
        )

        state = load_state()
        state["last_instagram_story_id"] = media_id
        state["last_instagram_error"] = None
        save_state(state)

        await bot.send_message(
            chat_id,
            f"✅ Stories опубликована.\nMedia ID: {media_id}",
        )
    except Exception as exc:
        logging.exception("Instagram story publish failed")
        state = load_state()
        state["last_instagram_error"] = f"STORY: {type(exc).__name__}: {exc}"
        save_state(state)
        await bot.send_message(
            chat_id,
            f"❌ Не удалось опубликовать Stories:\n{exc}",
        )


@router.callback_query(F.data == "ig:both")
async def publish_instagram_both(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    state = load_state()
    approved = state.get("last_approved") or {}
    post_raw = approved.get("post")
    story_raw = approved.get("story")
    caption = str(approved.get("caption") or "")

    if not post_raw or not story_raw:
        await callback.answer("Нет готовых файлов", show_alert=True)
        return

    if not instagram_configured():
        await callback.answer("Instagram API ещё не настроен", show_alert=True)
        return

    await callback.answer("Публикую пост и Stories…")
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id

    try:
        cleanup_public_media()

        _post_public, post_url = await asyncio.to_thread(
            expose_media_file,
            Path(post_raw),
        )
        await asyncio.sleep(1)
        post_id = await asyncio.to_thread(
            publish_image_to_instagram,
            post_url,
            caption,
        )

        await asyncio.sleep(2)

        _story_public, story_url = await asyncio.to_thread(
            expose_media_file,
            Path(story_raw),
        )
        await asyncio.sleep(1)
        story_id = await asyncio.to_thread(
            publish_story_to_instagram,
            story_url,
        )

        state = load_state()
        state["last_instagram_media_id"] = post_id
        state["last_instagram_story_id"] = story_id
        state["last_instagram_error"] = None
        save_state(state)

        await bot.send_message(
            chat_id,
            "✅ Пост и Stories опубликованы.\n"
            f"Post ID: {post_id}\n"
            f"Story ID: {story_id}",
        )

    except Exception as exc:
        logging.exception("Instagram post+story publish failed")
        state = load_state()
        state["last_instagram_error"] = f"POST+STORY: {type(exc).__name__}: {exc}"
        save_state(state)
        await bot.send_message(
            chat_id,
            f"❌ Ошибка публикации поста/Stories:\n{exc}",
        )


@router.callback_query(F.data == "ig:skip")
async def skip_instagram(callback: CallbackQuery):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("Оставил только файлы")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@router.message(Command("igstatus"))
async def instagram_status(message: Message):
    if await deny(message):
        return

    state = load_state()
    resolved_id = IG_USER_ID
    resolved_username = ""
    token_check = "НЕТ ❌"
    token_error = ""
    if IG_ACCESS_TOKEN:
        try:
            resolved_id, resolved_username = await asyncio.to_thread(resolve_ig_user_id)
            token_check = "РАБОТАЕТ ✅"
        except Exception as exc:
            token_check = "ОШИБКА ❌"
            token_error = str(exc)

    parts = [
        "📸 Instagram API (Instagram Login)",
        "",
        f"IG_ACCESS_TOKEN: {'ЕСТЬ ✅' if IG_ACCESS_TOKEN else 'НЕТ ❌'}",
        f"Проверка токена: {token_check}",
        f"Аккаунт: @{resolved_username}" if resolved_username else "Аккаунт: не определён",
        f"IG User ID: {resolved_id or 'определится автоматически'}",
        f"Публичная ссылка Railway: {'ЕСТЬ ✅' if PUBLIC_BASE_URL else 'НЕТ ❌'}",
        f"Graph API: graph.instagram.com / {META_GRAPH_VERSION}",
        f"Готов к публикации: {'ДА ✅' if (instagram_configured() and token_check == 'РАБОТАЕТ ✅') else 'НЕТ ❌'}",
        f"Последний Post ID: {state.get('last_instagram_media_id') or 'нет'}",
        f"Последний Story ID: {state.get('last_instagram_story_id') or 'нет'}",
        f"Последняя ошибка: {state.get('last_instagram_error') or 'нет'}",
    ]
    if token_error:
        parts.append(f"Ошибка токена: {token_error}")
    await message.answer("\n".join(parts))


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


async def handle_stories_pending_input(message: Message) -> bool:
    state = load_state()
    awaiting = state.get("stories_awaiting")
    if awaiting not in {"add", "limit"}:
        return False

    text = (message.text or "").strip()
    if awaiting == "add":
        raw_parts = re.split(r"[\s,;]+", text)
        new_names = []
        bad = []
        for raw in raw_parts:
            if not raw:
                continue
            username = _normalize_instagram_username(raw)
            if username:
                if username not in new_names:
                    new_names.append(username)
            else:
                bad.append(raw)
        if not new_names:
            await message.answer("❌ Не нашёл ни одного корректного Instagram username.")
            return True
        current = list(state.get("stories_usernames", []))
        for username in new_names:
            if username not in current:
                current.append(username)
        state["stories_usernames"] = current[:500]
        state["stories_awaiting"] = None
        save_state(state)
        suffix = f"\nНе распознано: {', '.join(bad[:5])}" if bad else ""
        await message.answer(
            f"✅ Добавлено: {len(new_names)}\nВсего в списке: {len(current)}{suffix}",
            reply_markup=stories_keyboard(state),
        )
        return True

    try:
        limit = int(text)
    except ValueError:
        await message.answer("❌ Отправь только число от 1 до 200.")
        return True
    if not 1 <= limit <= 200:
        await message.answer("❌ Допустимый лимит: от 1 до 200.")
        return True
    state["stories_daily_limit"] = limit
    state["stories_awaiting"] = None
    save_state(state)
    await message.answer(f"✅ Дневной лимит установлен: {limit}", reply_markup=stories_keyboard(state))
    return True


@router.message(F.text)
async def text_input(message: Message, bot: Bot):
    if await deny(message):
        return

    if await handle_stories_pending_input(message):
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
        f"Ручной резервный каталог обновлён ✅\n"
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

    web_runner = await start_public_server()
    task = asyncio.create_task(autopilot_loop(bot))
    ig_auto_task = asyncio.create_task(instagram_autopilot_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        ig_auto_task.cancel()
        await web_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
