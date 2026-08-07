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
    os.getenv("CALL_TO_ACTION", "Понравился номер? Пиши в Direct").strip()
    or "Понравился номер? Пиши в Direct"
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
    img = Image.new("RGBA", (width, height), (4, 10, 28, 255))
    d = ImageDraw.Draw(img)

    # Более глубокий синий градиент
    for y in range(height):
        t = y / max(1, height - 1)
        d.line(
            (0, y, width, y),
            fill=(
                int(4 + 5 * (1 - t)),
                int(10 + 20 * (1 - t)),
                int(28 + 45 * (1 - t)),
                255,
            ),
        )

    # Верхняя неоновая дуга
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        (width * 0.42, -height * 0.24, width * 1.16, height * 0.30),
        fill=(0, 146, 255, 125),
    )
    gd.ellipse(
        (-width * 0.22, height * 0.58, width * 0.32, height * 1.06),
        fill=(22, 71, 255, 72),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(int(width * 0.13)))
    img.alpha_composite(glow)

    # Тонкая рамка по периметру — как отдельная фирменная карточка
    d = ImageDraw.Draw(img)
    margin = 28
    d.rounded_rectangle(
        (margin, margin, width - margin, height - margin),
        radius=42,
        outline=(39, 151, 255, 150),
        width=2,
    )
    return img


def neon_box(
    img: Image.Image,
    box: tuple[int, int, int, int],
    radius: int = 34,
    fill=(7, 23, 57, 232),
):
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle(
        box,
        radius=radius,
        outline=(0, 146, 255, 160),
        width=13,
    )
    glow = glow.filter(ImageFilter.GaussianBlur(17))
    img.alpha_composite(glow)

    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        box,
        radius=radius,
        fill=fill,
        outline=(44, 172, 255, 255),
        width=3,
    )


def draw_brand(draw, width: int, y: int, story: bool):
    scale = 1.12 if story else 1.0
    f1 = font(FONT_BOLD, int(64 * scale))
    f2 = font(FONT_BOLD, int(76 * scale))
    left, right = "НОМЕРА", "96"
    b1 = draw.textbbox((0, 0), left, font=f1)
    b2 = draw.textbbox((0, 0), right, font=f2)
    total = (b1[2] - b1[0]) + 16 + (b2[2] - b2[0])
    x = (width - total) // 2

    draw.text((x, y), left, font=f1, fill=(250, 252, 255))
    draw.text(
        (x + (b1[2] - b1[0]) + 16, y - 5),
        right,
        font=f2,
        fill=(35, 180, 255),
    )

    tag = "КРАСИВЫЕ НОМЕРА • ОФОРМЛЕНИЕ ОНЛАЙН"
    tf = font(FONT_BOLD, 20 if not story else 27)
    tb = draw.textbbox((0, 0), tag, font=tf)
    draw.text(
        ((width - (tb[2] - tb[0])) // 2, y + (84 if not story else 98)),
        tag,
        font=tf,
        fill=(132, 201, 255),
    )


def draw_cta(draw, width, height, story):
    text = CALL_TO_ACTION.upper()
    f = fit_font(
        draw, text, FONT_BOLD,
        36 if story else 27,
        24 if story else 19,
        width - 190,
    )
    b = draw.textbbox((0, 0), text, font=f)
    w = (b[2] - b[0]) + 76
    h = 84 if story else 62
    x = (width - w) // 2
    y = height - (128 if story else 82)

    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=h // 2,
        fill=(16, 114, 250),
        outline=(115, 220, 255),
        width=3,
    )
    draw.text(
        (x + 38, y + (18 if story else 13)),
        text,
        font=f,
        fill=(255, 255, 255),
    )


def render_selection(
    items: list[NumberItem],
    target: Path,
    title: str,
    subtitle: str,
    story: bool = False,
) -> None:
    width, height = (1080, 1920) if story else (1080, 1080)
    img = background(width, height)
    d = ImageDraw.Draw(img)

    draw_brand(d, width, 82 if story else 48, story)

    title_font = font(FONT_BOLD, 48 if story else 34)
    tb = d.textbbox((0, 0), title, font=title_font)
    title_y = 245 if story else 168
    d.text(
        ((width - (tb[2] - tb[0])) // 2, title_y),
        title,
        font=title_font,
        fill=(245, 250, 255),
    )

    sub_font = font(FONT_REGULAR, 25 if story else 18)
    sb = d.textbbox((0, 0), subtitle, font=sub_font)
    d.text(
        ((width - (sb[2] - sb[0])) // 2, title_y + (64 if story else 47)),
        subtitle,
        font=sub_font,
        fill=(145, 203, 249),
    )

    # Главное отличие: одна большая неоновая панель, а не карточка на каждый номер.
    panel_top = 355 if story else 240
    panel_bottom = height - (260 if story else 150)
    panel = (74, panel_top, width - 74, panel_bottom)
    neon_box(img, panel, 38)

    d = ImageDraw.Draw(img)
    inner_top = panel_top + 34
    inner_bottom = panel_bottom - 34
    row_h = (inner_bottom - inner_top) // max(1, len(items))

    for idx, item in enumerate(items):
        y = inner_top + idx * row_h
        if idx > 0:
            d.line(
                (110, y, width - 110, y),
                fill=(49, 105, 162, 135),
                width=2,
            )

        phone = format_phone(item.phone)
        num_font = fit_font(
            d, phone, FONT_MONO,
            56 if story else 45,
            38 if story else 32,
            600 if story else 560,
        )
        d.text(
            (118, y + (24 if story else 17)),
            phone,
            font=num_font,
            fill=(251, 253, 255),
        )

        price = f"{item.price:,}".replace(",", " ") + " ₽"
        pf = font(FONT_BOLD, 33 if story else 26)
        pb = d.textbbox((0, 0), price, font=pf)
        badge_w = (pb[2] - pb[0]) + 46
        badge_h = 58 if story else 48
        bx = width - 118 - badge_w
        by = y + (30 if story else 20)
        d.rounded_rectangle(
            (bx, by, bx + badge_w, by + badge_h),
            radius=badge_h // 2,
            fill=(15, 108, 236),
            outline=(101, 211, 255),
            width=2,
        )
        d.text(
            (bx + 23, by + (11 if story else 9)),
            price,
            font=pf,
            fill=(255, 255, 255),
        )

        psub = "в месяц"
        psf = font(FONT_REGULAR, 19 if story else 15)
        psb = d.textbbox((0, 0), psub, font=psf)
        d.text(
            (bx + (badge_w - (psb[2] - psb[0])) // 2, by + badge_h + 5),
            psub,
            font=psf,
            fill=(126, 190, 242),
        )

    benefit = "НОМЕР БЕСПЛАТНО • ПЛАТИТЕ ТОЛЬКО ЗА ТАРИФ"
    bf = font(FONT_BOLD, 28 if story else 21)
    bb = d.textbbox((0, 0), benefit, font=bf)
    d.text(
        ((width - (bb[2] - bb[0])) // 2, panel_bottom + (42 if story else 26)),
        benefit,
        font=bf,
        fill=(139, 207, 255),
    )

    draw_cta(d, width, height, story)
    img.convert("RGB").save(target, "PNG", optimize=True)


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

    draw_brand(d, width, 82 if story else 48, story)

    title = f"ТАРИФ {price:,}".replace(",", " ") + " ₽/МЕС"
    title_font = font(FONT_BOLD, 48 if story else 35)
    tb = d.textbbox((0, 0), title, font=title_font)
    title_y = 250 if story else 170
    d.text(
        ((width - (tb[2] - tb[0])) // 2, title_y),
        title,
        font=title_font,
        fill=(250, 252, 255),
    )

    panel_top = 345 if story else 235
    panel_bottom = height - (250 if story else 145)
    panel = (82, panel_top, width - 82, panel_bottom)
    neon_box(img, panel, 38)

    d = ImageDraw.Draw(img)
    row_h = (panel_bottom - panel_top - 60) // max(1, len(phones))
    nf = font(FONT_MONO, 61 if story else 48)

    for idx, phone in enumerate(phones):
        y = panel_top + 30 + idx * row_h
        if idx > 0:
            d.line(
                (120, y, width - 120, y),
                fill=(49, 105, 162, 135),
                width=2,
            )

        formatted = format_phone(phone)
        fb = d.textbbox((0, 0), formatted, font=nf)
        d.text(
            ((width - (fb[2] - fb[0])) // 2, y + 22),
            formatted,
            font=nf,
            fill=(250, 253, 255),
        )

    footer = f"{page_no}/{page_total} • НОМЕРА96"
    ff = font(FONT_BOLD, 20 if not story else 26)
    fbox = d.textbbox((0, 0), footer, font=ff)
    d.text(
        ((width - (fbox[2] - fbox[0])) // 2, panel_bottom + 32),
        footer,
        font=ff,
        fill=(124, 194, 246),
    )

    draw_cta(d, width, height, story)
    img.convert("RGB").save(target, "PNG", optimize=True)


def caption_for(items: list[NumberItem], title: str) -> str:
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
    post, _, _ = await asyncio.to_thread(
        create_selection_bundle,
        items, title, subtitle, "draft",
    )

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
        "Номера96 Автопилот v4 ✅\n\n"
        f"Каталог: {total} номеров\n"
        f"Не использовано: {available}\n\n"
        "Теперь бот умеет не только ТОП-5.",
        reply_markup=MENU,
    )


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
        f"📊 Номера96\n\n"
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
