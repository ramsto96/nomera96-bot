from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
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


# ---------------------------
# Настройки
# ---------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_ENV = os.getenv("OWNER_ID", "").strip()
CALL_TO_ACTION = (
    os.getenv("CALL_TO_ACTION", "Понравился номер? Пиши в Direct").strip()
    or "Понравился номер? Пиши в Direct"
)
STORE_LINK = os.getenv(
    "STORE_LINK",
    "https://l.bezlimit.ru/store/659787",
).strip()
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


# ---------------------------
# Модели и парсер
# ---------------------------

TARIFF_RE = re.compile(
    r"тариф\s*:\s*([\d\s]+)\s*(?:руб(?:\.|лей)?|₽)?\s*(?:/|в)?\s*мес",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NumberItem:
    phone: str
    price: int
    beauty: int


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

        match = TARIFF_RE.search(line)
        if match:
            current_price = int(re.sub(r"\D", "", match.group(1)))
            continue

        phone = normalize_phone(line)
        if phone and current_price is not None:
            row = (phone, current_price)
            if row not in rows:
                rows.append(row)

    if not rows:
        raise ValueError(
            "Не нашёл номера. Нужен формат: «Тариф: 950 руб/мес», "
            "а ниже номера по одному в строке."
        )
    return rows


# ---------------------------
# Оценка красоты номера
# ---------------------------

def beauty_score(phone: str) -> int:
    # Основной акцент на последних 7 цифрах — их клиент запоминает лучше всего.
    s = phone[3:]
    score = 0

    # Длинные повторы: 0000, 7777, 555 и т.п.
    runs = re.findall(r"((\d)\2+)", s)
    for run, _digit in runs:
        n = len(run)
        score += (n - 1) * 12 + max(0, n - 2) * 10

    # Частое повторение одной цифры.
    counts = Counter(s)
    for count in counts.values():
        if count >= 3:
            score += (count - 2) * 7

    # Красивые окончания.
    last4 = s[-4:]
    last6 = s[-6:]

    if len(set(last4)) == 1:
        score += 65
    if last4[:2] == last4[2:]:
        score += 48  # 9696 / 2222 / 1212
    if s[-2:] == s[-4:-2]:
        score += 36  # 22-22 / 88-88
    if last6[:3] == last6[3:]:
        score += 52  # 123123
    if last4 == last4[::-1]:
        score += 28
    if s[-5:] == s[-5:][::-1]:
        score += 34

    # Круглые комбинации.
    if s.endswith("0000"):
        score += 70
    elif s.endswith("000"):
        score += 35
    elif s.endswith("00"):
        score += 14

    # Парные блоки в форматировании XXX-XX-XX.
    a, b, c = s[:3], s[3:5], s[5:7]
    if b == c:
        score += 42
    if len(set(a)) == 1:
        score += 24
    if a[-1] == b[0] == b[1]:
        score += 8

    # Общая симметрия.
    if s == s[::-1]:
        score += 80

    return score


def sales_rank(phone: str, price: int) -> int:
    # Красота важнее цены, но доступные тарифы получают небольшой бонус.
    affordability_bonus = max(0, 10 - price // 600)
    return beauty_score(phone) + affordability_bonus


def select_best(
    catalog: list[tuple[str, int]],
    used: set[str],
    limit: int = 5,
) -> list[NumberItem]:
    candidates = [
        NumberItem(phone=p, price=price, beauty=sales_rank(p, price))
        for p, price in catalog
        if p not in used
    ]
    candidates.sort(key=lambda x: (x.beauty, -x.price), reverse=True)

    # Не забиваем подборку одним тарифом: максимум 2 номера с одной ценой.
    picked: list[NumberItem] = []
    per_price: Counter[int] = Counter()

    for item in candidates:
        if per_price[item.price] >= 2:
            continue
        picked.append(item)
        per_price[item.price] += 1
        if len(picked) >= limit:
            break

    # Если из-за ограничения не набралось 5 — добираем.
    if len(picked) < limit:
        picked_phones = {x.phone for x in picked}
        for item in candidates:
            if item.phone in picked_phones:
                continue
            picked.append(item)
            if len(picked) >= limit:
                break

    return picked


# ---------------------------
# Состояние
# ---------------------------

DEFAULT_STATE = {
    "catalog": [],
    "used": [],
    "autopilot": True,
    "draft": [],
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
        logging.exception("Не удалось прочитать состояние")
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
    state["draft_created_at"] = None
    save_state(state)


def get_catalog(state: dict) -> list[tuple[str, int]]:
    return [
        (str(x["phone"]), int(x["price"]))
        for x in state.get("catalog", [])
    ]


# ---------------------------
# Рендер фирменных карточек
# ---------------------------

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


def font_path(candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("Не найден шрифт DejaVu Sans")


FONT_BOLD = font_path(FONT_BOLD_CANDIDATES)
FONT_REGULAR = font_path(FONT_REGULAR_CANDIDATES)
FONT_MONO = font_path(FONT_MONO_CANDIDATES)


def font(path: str, size: int):
    return ImageFont.truetype(path, size=size)



def gradient_background(width: int, height: int) -> Image.Image:
    # Более чистый фирменный фон в стиле первых макетов:
    # тёмно-синий градиент + мягкие неоновые засветки.
    img = Image.new("RGBA", (width, height), (5, 13, 34, 255))
    bg = ImageDraw.Draw(img)

    for y in range(height):
        t = y / max(height - 1, 1)
        color = (
            int(5 + 6 * (1 - t)),
            int(13 + 18 * (1 - t)),
            int(34 + 34 * (1 - t)),
            255,
        )
        bg.line((0, y, width, y), fill=color)

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        (width * 0.52, -height * 0.20, width * 1.15, height * 0.32),
        fill=(0, 150, 255, 110),
    )
    gd.ellipse(
        (-width * 0.15, height * 0.60, width * 0.45, height * 1.05),
        fill=(0, 94, 255, 70),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(int(width * 0.12)))
    img.alpha_composite(glow)
    return img


def fit_font(draw: ImageDraw.ImageDraw, text: str, font_path: str, max_size: int, min_size: int, max_width: int):
    for size in range(max_size, min_size - 1, -2):
        f = font(font_path, size)
        box = draw.textbbox((0, 0), text, font=f)
        if box[2] - box[0] <= max_width:
            return f
    return font(font_path, min_size)


def draw_logo(draw: ImageDraw.ImageDraw, width: int, y: int, scale: float = 1.0):
    f1 = font(FONT_BOLD, int(62 * scale))
    f2 = font(FONT_BOLD, int(74 * scale))
    small = font(FONT_REGULAR, int(18 * scale))

    left = "НОМЕРА"
    right = "96"
    b1 = draw.textbbox((0, 0), left, font=f1)
    b2 = draw.textbbox((0, 0), right, font=f2)
    total_w = (b1[2] - b1[0]) + int(16 * scale) + (b2[2] - b2[0])
    x = (width - total_w) // 2

    draw.text((x, y), left, font=f1, fill=(248, 251, 255))
    draw.text(
        (x + (b1[2] - b1[0]) + int(16 * scale), y - int(4 * scale)),
        right,
        font=f2,
        fill=(34, 176, 255),
    )

    tagline = "красивые номера"
    tb = draw.textbbox((0, 0), tagline, font=small)
    draw.text(
        ((width - (tb[2] - tb[0])) // 2, y + int(68 * scale)),
        tagline,
        font=small,
        fill=(148, 205, 255),
    )


def draw_glow_panel(base: Image.Image, box: tuple[int, int, int, int], radius: int):
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle(
        box,
        radius=radius,
        outline=(18, 145, 255, 145),
        width=10,
    )
    layer = layer.filter(ImageFilter.GaussianBlur(16))
    base.alpha_composite(layer)

    d = ImageDraw.Draw(base)
    d.rounded_rectangle(
        box,
        radius=radius,
        fill=(8, 24, 58, 232),
        outline=(46, 173, 255, 255),
        width=3,
    )


def draw_price_badge(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, story: bool):
    f = font(FONT_BOLD, 26 if not story else 34)
    box = draw.textbbox((0, 0), text, font=f)
    pad_x = 22 if not story else 28
    pad_y = 10 if not story else 13
    w = (box[2] - box[0]) + pad_x * 2
    h = (box[3] - box[1]) + pad_y * 2 + 4
    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=h // 2,
        fill=(17, 118, 255),
        outline=(123, 220, 255),
        width=2,
    )
    draw.text((x + pad_x, y + pad_y), text, font=f, fill=(255, 255, 255))
    return w, h


def render_draft(
    items: list[NumberItem],
    kind: str,
    target: Path,
) -> None:
    story = kind == "story"
    width, height = (1080, 1920) if story else (1080, 1080)
    img = gradient_background(width, height)
    d = ImageDraw.Draw(img)

    top = 92 if story else 52
    scale = 1.12 if story else 1.0
    draw_logo(d, width, top, scale)

    subtitle = "ТОП НОМЕРОВ НА СЕГОДНЯ"
    sf = font(FONT_BOLD, 40 if story else 30)
    sb = d.textbbox((0, 0), subtitle, font=sf)
    subtitle_y = top + (126 if story else 98)
    d.text(
        ((width - (sb[2] - sb[0])) // 2, subtitle_y),
        subtitle,
        font=sf,
        fill=(188, 227, 255),
    )

    small = "готовая подборка от Номера96"
    small_font = font(FONT_REGULAR, 22 if story else 17)
    sbox = d.textbbox((0, 0), small, font=small_font)
    d.text(
        ((width - (sbox[2] - sbox[0])) // 2, subtitle_y + (52 if story else 40)),
        small,
        font=small_font,
        fill=(120, 182, 240),
    )

    list_top = 360 if story else 220
    bottom_reserved = 280 if story else 170
    gap = 22 if story else 16
    available_h = height - list_top - bottom_reserved
    card_h = int((available_h - gap * (len(items) - 1)) / max(len(items), 1))
    card_h = max(card_h, 104 if not story else 150)
    x1, x2 = 74, width - 74

    for idx, item in enumerate(items):
        y1 = list_top + idx * (card_h + gap)
        y2 = y1 + card_h

        draw_glow_panel(img, (x1, y1, x2, y2), 30 if not story else 34)
        d = ImageDraw.Draw(img)

        phone_text = format_phone(item.phone)
        max_num_width = (x2 - x1) - 270
        num_font = fit_font(
            d,
            phone_text,
            FONT_MONO,
            54 if not story else 66,
            34 if not story else 44,
            max_num_width,
        )
        num_y = y1 + 20 if not story else y1 + 28
        d.text(
            (x1 + 34, num_y),
            phone_text,
            font=num_font,
            fill=(248, 252, 255),
        )

        price_text = f"{item.price:,}".replace(",", " ") + " ₽/мес"
        badge_x = x2 - (214 if not story else 260)
        badge_y = y2 - (58 if not story else 72)
        draw_price_badge(d, badge_x, badge_y, price_text, story)

        sub = "красивый номер"
        sub_font = font(FONT_REGULAR, 18 if not story else 24)
        d.text(
            (x1 + 36, y2 - (42 if not story else 54)),
            sub,
            font=sub_font,
            fill=(129, 196, 248),
        )

    benefit = "НОМЕР БЕСПЛАТНО • ОПЛАТА ТОЛЬКО ЗА ТАРИФ"
    bf = font(FONT_BOLD, 28 if story else 21)
    bb = d.textbbox((0, 0), benefit, font=bf)
    benefit_y = height - (205 if story else 120)
    d.text(
        ((width - (bb[2] - bb[0])) // 2, benefit_y),
        benefit,
        font=bf,
        fill=(140, 206, 255),
    )

    cta = CALL_TO_ACTION.upper()
    cf = fit_font(
        d,
        cta,
        FONT_BOLD,
        42 if story else 30,
        28 if story else 20,
        width - 180,
    )
    cb = d.textbbox((0, 0), cta, font=cf)
    cw = (cb[2] - cb[0]) + 72
    ch = 88 if story else 66
    cx = (width - cw) // 2
    cy = height - (122 if story else 74)
    d.rounded_rectangle(
        (cx, cy, cx + cw, cy + ch),
        radius=ch // 2,
        fill=(18, 116, 252),
        outline=(119, 218, 255),
        width=3,
    )
    d.text(
        (cx + 36, cy + (19 if story else 14)),
        cta,
        font=cf,
        fill=(255, 255, 255),
    )

    img.convert("RGB").save(target, "PNG", optimize=True)


def make_caption(items: list[NumberItem]) -> str:
    lines = [
        "🔥 ТОП красивых номеров на сегодня",
        "",
    ]
    for item in items:
        lines.append(
            f"📱 {format_phone(item.phone)} — "
            f"{item.price:,}".replace(",", " ") + " ₽/мес"
        )
    lines += [
        "",
        "✅ Номер бесплатно",
        "💳 Платите только за выбранный тариф",
        "✅ Оформление онлайн",
        "",
        "Понравился номер? Пиши «НОМЕР» в Direct 📩",
        "",
        "#номера96 #красивыеномера #красивыйномер #тарифы #симкарта",
    ]
    return "\n".join(lines)


def render_bundle(items: list[NumberItem], tag: str) -> tuple[Path, Path, str]:
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    folder = OUTPUT_DIR / f"{tag}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)

    post = folder / "post.png"
    story = folder / "story.png"
    render_draft(items, "post", post)
    render_draft(items, "story", story)
    return post, story, make_caption(items)


# ---------------------------
# Telegram UI
# ---------------------------

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✨ ТОП-5 на сегодня")],
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
    input_field_placeholder="Вставь свежий список номеров",
)


def draft_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data="draft:approve",
                ),
                InlineKeyboardButton(
                    text="🔄 Другие",
                    callback_data="draft:next",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data="draft:skip",
                )
            ],
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


async def create_and_send_draft(
    bot: Bot,
    chat_id: int,
    *,
    rotate: bool = False,
) -> bool:
    state = load_state()
    catalog = get_catalog(state)

    if not catalog:
        await bot.send_message(
            chat_id,
            "Сначала загрузи каталог: нажми «📥 Обновить каталог» "
            "и вставь список тарифов с номерами.",
        )
        return False

    used = set(state.get("used", []))

    if rotate and state.get("draft"):
        for row in state["draft"]:
            used.add(row["phone"])

    items = select_best(catalog, used, 5)

    if not items:
        # Все номера уже были показаны — начинаем круг заново.
        used = set()
        items = select_best(catalog, used, 5)

    if not items:
        await bot.send_message(chat_id, "В каталоге нет доступных номеров.")
        return False

    state["draft"] = [
        {"phone": x.phone, "price": x.price, "beauty": x.beauty}
        for x in items
    ]
    state["draft_created_at"] = datetime.now(TZ).isoformat()
    save_state(state)

    post, _story, caption = await asyncio.to_thread(
        render_bundle,
        items,
        "draft",
    )

    await bot.send_photo(
        chat_id,
        FSInputFile(post),
        caption=(
            "🤖 Автопилот подготовил публикацию.\n\n"
            "Нажми «Одобрить», «Другие» или «Пропустить»."
        ),
        reply_markup=draft_keyboard(),
    )
    return True


# ---------------------------
# Хендлеры
# ---------------------------

@router.message(CommandStart())
async def start(message: Message):
    if await deny(message):
        return

    state = load_state()
    total, available = catalog_stats(state)
    await message.answer(
        "Номера96 Автопилот v2 ✅\n\n"
        f"Каталог: {total} номеров\n"
        f"Ещё не использовано: {available}\n\n"
        "Загрузи свежий список один раз — дальше я сам выбираю "
        "красивые номера и готовлю контент.",
        reply_markup=MENU,
    )


@router.message(Command("status"))
@router.message(F.text == "📊 Статус")
async def status(message: Message):
    if await deny(message):
        return
    state = load_state()
    total, available = catalog_stats(state)
    auto = "ВКЛ ✅" if state.get("autopilot", True) else "ВЫКЛ ⛔"
    await message.answer(
        f"📊 Номера96 Автопилот\n\n"
        f"Всего номеров: {total}\n"
        f"Не использовано: {available}\n"
        f"Автопилот: {auto}\n"
        f"Автоподборка: каждый день около "
        f"{AUTOPILOT_HOUR:02d}:{AUTOPILOT_MINUTE:02d} по Москве"
    )


@router.message(F.text == "📥 Обновить каталог")
async def update_catalog_prompt(message: Message):
    if await deny(message):
        return
    await message.answer(
        "Вставь сюда свежий список целиком, например:\n\n"
        "Тариф: 1600 руб/мес\n"
        "9003366888\n"
        "9011163333\n\n"
        "Тариф: 950 руб/мес\n"
        "9010777477\n"
        "9305559255"
    )


@router.message(F.text == "✨ ТОП-5 на сегодня")
async def top5(message: Message, bot: Bot):
    if await deny(message):
        return
    await create_and_send_draft(bot, message.chat.id)


@router.message(F.text == "🤖 Автопилот")
async def toggle_autopilot(message: Message):
    if await deny(message):
        return
    state = load_state()
    state["autopilot"] = not bool(state.get("autopilot", True))
    save_state(state)
    status_text = "ВКЛЮЧЁН ✅" if state["autopilot"] else "ВЫКЛЮЧЕН ⛔"
    await message.answer(
        f"🤖 Автопилот {status_text}\n\n"
        "Когда включён, бот сам готовит ежедневную подборку и "
        "присылает её тебе на одобрение."
    )


@router.message(F.text == "🔗 Суперссылка")
async def super_link(message: Message):
    if await deny(message):
        return
    await message.answer(f"🔗 Твоя суперссылка:\n{STORE_LINK}")


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
    if not raw:
        await callback.answer("Черновик уже устарел", show_alert=True)
        return

    items = [
        NumberItem(
            phone=str(x["phone"]),
            price=int(x["price"]),
            beauty=int(x.get("beauty", 0)),
        )
        for x in raw
    ]

    post, story, caption = await asyncio.to_thread(
        render_bundle,
        items,
        "approved",
    )

    used = set(state.get("used", []))
    used.update(x.phone for x in items)
    state["used"] = sorted(used)
    state["draft"] = []
    state["draft_created_at"] = None
    save_state(state)

    await callback.answer("Одобрено ✅")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await bot.send_photo(
        chat_id,
        FSInputFile(post),
        caption="✅ ГОТОВЫЙ ПОСТ",
    )
    await bot.send_document(
        chat_id,
        FSInputFile(story),
        caption="✅ СТОРИС 9:16 без сжатия",
    )
    await bot.send_message(
        chat_id,
        "📝 Описание для публикации:\n\n" + caption,
    )
    await bot.send_message(
        chat_id,
        "🔗 Ссылка для клиента:\n" + STORE_LINK,
    )


@router.callback_query(F.data == "draft:next")
async def next_draft(callback: CallbackQuery, bot: Bot):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("Выбираю другие номера…")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await create_and_send_draft(bot, callback.message.chat.id, rotate=True)


@router.callback_query(F.data == "draft:skip")
async def skip_draft(callback: CallbackQuery):
    if not callback.from_user or not claim_or_check_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    state = load_state()
    # Пропуск не помечает номера использованными — они могут попасть позже.
    state["draft"] = []
    state["draft_created_at"] = None
    save_state(state)

    await callback.answer("Пропущено")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                "⏭ Подборка пропущена. Номера остались в каталоге."
            )
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

    scored = sorted(
        (
            NumberItem(p, price, sales_rank(p, price))
            for p, price in rows
        ),
        key=lambda x: x.beauty,
        reverse=True,
    )

    await message.answer(
        f"Каталог обновлён ✅\n\n"
        f"Загружено номеров: {len(rows)}\n"
        f"Самый красивый по оценке бота:\n"
        f"{format_phone(scored[0].phone)} — "
        f"{scored[0].price:,}".replace(",", " ") + " ₽/мес\n\n"
        "Сейчас сразу подготовлю ТОП-5."
    )
    await create_and_send_draft(bot, message.chat.id)


# ---------------------------
# Ежедневный автопилот
# ---------------------------

async def autopilot_loop(bot: Bot):
    while True:
        try:
            await asyncio.sleep(20)
            owner = get_owner_id()
            if owner is None:
                continue

            state = load_state()
            if not state.get("autopilot", True):
                continue
            if not state.get("catalog"):
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

            # Если сервис перезапустился позже утром — отправляем всё равно,
            # но только до 13:00, чтобы не прилетало вечером.
            if target <= now <= target + timedelta(hours=4):
                sent = await create_and_send_draft(bot, owner)
                if sent:
                    state = load_state()
                    state["last_auto_date"] = today
                    save_state(state)

        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка ежедневного автопилота")
            await asyncio.sleep(60)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в Railway Variables")

    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    auto_task = asyncio.create_task(autopilot_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        auto_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
