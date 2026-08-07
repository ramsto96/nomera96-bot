from __future__ import annotations

\
import re
from dataclasses import dataclass


TARIFF_RE = re.compile(
    r"тариф\s*:\s*([\d\s]+)\s*(?:руб(?:\.|лей)?|₽)?\s*(?:/|в)?\s*мес",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TariffGroup:
    price: int
    numbers: tuple[str, ...]


class ParseError(ValueError):
    pass


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)

    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = digits[1:]

    if len(digits) != 10 or not digits.startswith("9"):
        return None

    return digits


def format_phone(number: str) -> str:
    return f"+7 ({number[:3]}) {number[3:6]}-{number[6:8]}-{number[8:]}"


def parse_tariffs(text: str) -> list[TariffGroup]:
    current_price: int | None = None
    groups: dict[int, list[str]] = {}
    invalid_lines: list[str] = []

    for original_line in text.splitlines():
        line = original_line.strip()
        if not line:
            continue

        tariff_match = TARIFF_RE.search(line)
        if tariff_match:
            current_price = int(re.sub(r"\D", "", tariff_match.group(1)))
            groups.setdefault(current_price, [])
            continue

        phone = normalize_phone(line)
        if phone:
            if current_price is None:
                invalid_lines.append(original_line)
                continue
            if phone not in groups[current_price]:
                groups[current_price].append(phone)
            continue

        if any(ch.isdigit() for ch in line):
            invalid_lines.append(original_line)

    parsed = [
        TariffGroup(price=price, numbers=tuple(numbers))
        for price, numbers in groups.items()
        if numbers
    ]

    if not parsed:
        raise ParseError(
            "Не удалось найти тарифы и номера. Проверь формат сообщения."
        )

    parsed.sort(key=lambda item: item.price, reverse=True)
    return parsed


def count_numbers(groups: list[TariffGroup]) -> int:
    return sum(len(group.numbers) for group in groups)


\
import math
import os
import shutil
import textwrap
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont



PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = PROJECT_ROOT / "output"
OUTPUT_ROOT.mkdir(exist_ok=True)

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


def _font_path(candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("Не найден шрифт DejaVu Sans.")


FONT_BOLD = _font_path(FONT_BOLD_CANDIDATES)
FONT_REGULAR = _font_path(FONT_REGULAR_CANDIDATES)
FONT_MONO = _font_path(FONT_MONO_CANDIDATES)


@dataclass(frozen=True)
class RenderResult:
    directory: Path
    post_files: tuple[Path, ...]
    story_files: tuple[Path, ...]
    description_file: Path
    numbers_file: Path
    archive_file: Path


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size)


def _gradient_background(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height))
    px = image.load()
    for y in range(height):
        y_ratio = y / max(height - 1, 1)
        for x in range(width):
            x_ratio = x / max(width - 1, 1)
            glow = max(0.0, 1.0 - math.hypot(x_ratio - 0.78, y_ratio - 0.12) / 0.85)
            r = int(4 + 3 * glow)
            g = int(13 + 34 * glow)
            b = int(35 + 82 * glow)
            px[x, y] = (r, g, b)
    return image


def _rounded_glow(
    base: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, int, int, int],
    outline: tuple[int, int, int, int],
    glow: tuple[int, int, int, int],
    glow_width: int = 18,
) -> None:
    width, height = base.size
    glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.rounded_rectangle(box, radius=radius, outline=glow, width=glow_width)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_width))
    base.alpha_composite(glow_layer)

    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=3)
    base.alpha_composite(panel)


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    max_size: int,
    min_size: int,
    max_width: int,
) -> ImageFont.FreeTypeFont:
    for size in range(max_size, min_size - 1, -2):
        font = _font(font_path, size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
    return _font(font_path, min_size)


def _draw_logo(draw: ImageDraw.ImageDraw, width: int, top: int) -> int:
    title_left = "НОМЕРА"
    title_right = "96"
    left_font = _font(FONT_BOLD, 64 if width <= 1080 else 70)
    right_font = _font(FONT_BOLD, 72 if width <= 1080 else 80)
    left_box = draw.textbbox((0, 0), title_left, font=left_font)
    right_box = draw.textbbox((0, 0), title_right, font=right_font)
    total_w = (left_box[2] - left_box[0]) + 18 + (right_box[2] - right_box[0])
    x = (width - total_w) // 2
    draw.text((x, top), title_left, font=left_font, fill=(247, 250, 255))
    x += (left_box[2] - left_box[0]) + 18
    draw.text((x, top - 5), title_right, font=right_font, fill=(33, 172, 255))
    return top + 88


def _chunks(values: tuple[str, ...], size: int) -> list[tuple[str, ...]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def _render_card(
    tariff: TariffGroup,
    numbers: tuple[str, ...],
    page_index: int,
    page_total: int,
    width: int,
    height: int,
    path: Path,
    call_to_action: str,
    is_story: bool,
) -> None:
    image = _gradient_background(width, height).convert("RGBA")
    draw = ImageDraw.Draw(image)

    y = 58 if not is_story else 105
    y = _draw_logo(draw, width, y)

    subtitle = "КРАСИВЫЕ НОМЕРА В НАЛИЧИИ"
    subtitle_font = _font(FONT_BOLD, 34 if not is_story else 45)
    subtitle_box = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    draw.text(
        ((width - (subtitle_box[2] - subtitle_box[0])) // 2, y + 8),
        subtitle,
        font=subtitle_font,
        fill=(184, 221, 255),
    )
    y += 66 if not is_story else 85

    panel_margin = 70 if not is_story else 82
    panel_top = y + 20
    panel_bottom = height - (190 if not is_story else 275)
    panel_box = (panel_margin, panel_top, width - panel_margin, panel_bottom)
    _rounded_glow(
        image,
        panel_box,
        radius=34,
        fill=(7, 25, 61, 225),
        outline=(35, 161, 255, 255),
        glow=(0, 127, 255, 125),
        glow_width=18,
    )
    draw = ImageDraw.Draw(image)

    badge_text = f"{tariff.price:,}".replace(",", " ") + " ₽/МЕС"
    badge_font = _font(FONT_BOLD, 43 if not is_story else 56)
    badge_box = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w = badge_box[2] - badge_box[0] + 68
    badge_h = 78 if not is_story else 96
    badge_x = (width - badge_w) // 2
    badge_y = panel_top - badge_h // 2
    draw.rounded_rectangle(
        (badge_x, badge_y, badge_x + badge_w, badge_y + badge_h),
        radius=badge_h // 2,
        fill=(22, 119, 255),
        outline=(114, 210, 255),
        width=3,
    )
    draw.text(
        (badge_x + 34, badge_y + (13 if not is_story else 18)),
        badge_text,
        font=badge_font,
        fill="white",
    )

    list_top = badge_y + badge_h + (28 if not is_story else 55)
    list_bottom = panel_bottom - 35
    available_h = max(200, list_bottom - list_top)
    line_h = available_h // max(len(numbers), 1)

    max_font = 58 if not is_story else 76
    min_font = 37 if not is_story else 48
    number_font = _fit_font(
        draw,
        "+7 (999) 999-99-99",
        FONT_MONO,
        min(max_font, int(line_h * 0.68)),
        min_font,
        width - panel_margin * 2 - 90,
    )

    for idx, number in enumerate(numbers):
        text = format_phone(number)
        bbox = draw.textbbox((0, 0), text, font=number_font)
        text_w = bbox[2] - bbox[0]
        item_y = list_top + idx * line_h + max(0, (line_h - (bbox[3] - bbox[1])) // 2 - 6)

        if idx % 2 == 0:
            row_y1 = list_top + idx * line_h
            row_y2 = min(list_bottom, row_y1 + line_h - 4)
            draw.rounded_rectangle(
                (panel_margin + 26, row_y1, width - panel_margin - 26, row_y2),
                radius=17,
                fill=(17, 48, 93, 150),
            )

        draw.text(
            ((width - text_w) // 2, item_y),
            text,
            font=number_font,
            fill=(248, 252, 255),
        )

    benefit = "НОМЕР БЕСПЛАТНО • ОПЛАТА ТОЛЬКО ЗА ТАРИФ"
    benefit_font = _font(FONT_BOLD, 25 if not is_story else 34)
    benefit_bbox = draw.textbbox((0, 0), benefit, font=benefit_font)
    draw.text(
        ((width - (benefit_bbox[2] - benefit_bbox[0])) // 2, panel_bottom + 43),
        benefit,
        font=benefit_font,
        fill=(139, 204, 255),
    )

    cta_font = _fit_font(
        draw,
        call_to_action.upper(),
        FONT_BOLD,
        31 if not is_story else 43,
        24 if not is_story else 33,
        width - 140,
    )
    cta_bbox = draw.textbbox((0, 0), call_to_action.upper(), font=cta_font)
    cta_w = cta_bbox[2] - cta_bbox[0] + 60
    cta_h = 67 if not is_story else 88
    cta_x = (width - cta_w) // 2
    cta_y = height - (95 if not is_story else 135)
    draw.rounded_rectangle(
        (cta_x, cta_y, cta_x + cta_w, cta_y + cta_h),
        radius=cta_h // 2,
        fill=(18, 116, 252),
        outline=(119, 218, 255),
        width=3,
    )
    draw.text(
        (cta_x + 30, cta_y + (14 if not is_story else 20)),
        call_to_action.upper(),
        font=cta_font,
        fill="white",
    )

    page_text = f"{page_index}/{page_total}"
    page_font = _font(FONT_BOLD, 22 if not is_story else 28)
    draw.text(
        (width - 105, height - 50 if not is_story else height - 67),
        page_text,
        font=page_font,
        fill=(130, 193, 245),
    )

    image.convert("RGB").save(path, "PNG", optimize=True)


def _description(groups: list[TariffGroup]) -> str:
    prices = ", ".join(
        f"{group.price:,}".replace(",", " ") + " ₽"
        for group in groups
    )
    return (
        "Красивые номера в наличии 🔥\n\n"
        "📱 Номер предоставляется бесплатно\n"
        "💳 Оплачивается только выбранный тариф\n"
        "✅ Оформление онлайн\n\n"
        f"Доступные тарифы: {prices} в месяц.\n\n"
        "Понравился номер? Пиши в Direct 📩\n"
        "Проверим наличие и поможем с оформлением.\n\n"
        "#номера96 #красивыеномера #красивыйномер "
        "#симкарта #тарифы"
    )


def render_all(
    groups: list[TariffGroup],
    user_id: int,
    call_to_action: str,
) -> RenderResult:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = OUTPUT_ROOT / f"{user_id}_{timestamp}"
    posts_dir = work_dir / "posts"
    stories_dir = work_dir / "stories"
    posts_dir.mkdir(parents=True)
    stories_dir.mkdir(parents=True)

    post_jobs: list[tuple[TariffGroup, tuple[str, ...]]] = []
    story_jobs: list[tuple[TariffGroup, tuple[str, ...]]] = []

    for group in groups:
        post_jobs.extend((group, chunk) for chunk in _chunks(group.numbers, 8))
        story_jobs.extend((group, chunk) for chunk in _chunks(group.numbers, 11))

    post_files: list[Path] = []
    for index, (group, chunk) in enumerate(post_jobs, start=1):
        path = posts_dir / f"post_{index:02d}_{group.price}.png"
        _render_card(
            group, chunk, index, len(post_jobs), 1080, 1080, path,
            call_to_action, False
        )
        post_files.append(path)

    story_files: list[Path] = []
    for index, (group, chunk) in enumerate(story_jobs, start=1):
        path = stories_dir / f"story_{index:02d}_{group.price}.png"
        _render_card(
            group, chunk, index, len(story_jobs), 1080, 1920, path,
            call_to_action, True
        )
        story_files.append(path)

    description_file = work_dir / "description.txt"
    description_file.write_text(_description(groups), encoding="utf-8")

    numbers_file = work_dir / "numbers.txt"
    lines: list[str] = []
    for group in groups:
        lines.append(f"Тариф: {group.price} руб/мес")
        lines.extend(format_phone(n) for n in group.numbers)
        lines.append("")
    numbers_file.write_text("\n".join(lines), encoding="utf-8")

    archive_file = work_dir / "nomera96_materials.zip"
    with zipfile.ZipFile(archive_file, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in [*post_files, *story_files, description_file, numbers_file]:
            archive.write(file_path, file_path.relative_to(work_dir))

    return RenderResult(
        directory=work_dir,
        post_files=tuple(post_files),
        story_files=tuple(story_files),
        description_file=description_file,
        numbers_file=numbers_file,
        archive_file=archive_file,
    )


\
import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv



load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CALL_TO_ACTION = (
    os.getenv("CALL_TO_ACTION", "Понравился номер? Пиши в Direct").strip()
    or "Понравился номер? Пиши в Direct"
)
OWNER_ID_ENV = os.getenv("OWNER_ID", "").strip()

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
OWNER_FILE = DATA_DIR / "owner_id.txt"

router = Router()

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Создать подборку")],
        [
            KeyboardButton(text="🧾 Формат списка"),
            KeyboardButton(text="🔗 Суперссылка"),
        ],
    ],
    resize_keyboard=True,
    input_field_placeholder="Вставь список тарифов и номеров",
)

FORMAT_EXAMPLE = """\
Отправь список одним сообщением:

Тариф: 1600 руб/мес
9003366888
9011163333

Тариф: 950 руб/мес
9010777477
9305559255
"""

STORE_LINK = "https://l.bezlimit.ru/store/659787"


def get_owner_id() -> int | None:
    if OWNER_ID_ENV.isdigit():
        return int(OWNER_ID_ENV)

    if OWNER_FILE.exists():
        value = OWNER_FILE.read_text(encoding="utf-8").strip()
        if value.isdigit():
            return int(value)

    return None


def claim_or_check_owner(user_id: int) -> bool:
    owner_id = get_owner_id()

    if owner_id is None:
        OWNER_FILE.write_text(str(user_id), encoding="utf-8")
        return True

    return owner_id == user_id


async def deny_if_not_owner(message: Message) -> bool:
    if message.from_user is None:
        return True

    if not claim_or_check_owner(message.from_user.id):
        await message.answer("⛔ Этот бот закрыт и используется владельцем Номера96.")
        return True

    return False


@router.message(CommandStart())
async def start(message: Message) -> None:
    if await deny_if_not_owner(message):
        return

    await message.answer(
        "Готово ✅\n\n"
        "Я создаю фирменные посты и сторис Номера96.\n"
        "Нажми «Создать подборку» и вставь список тарифов с номерами.",
        reply_markup=MENU,
    )


@router.message(Command("myid"))
async def my_id(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")


@router.message(Command("create"))
@router.message(F.text == "➕ Создать подборку")
async def create_prompt(message: Message) -> None:
    if await deny_if_not_owner(message):
        return
    await message.answer(FORMAT_EXAMPLE)


@router.message(Command("format"))
@router.message(F.text == "🧾 Формат списка")
async def format_prompt(message: Message) -> None:
    if await deny_if_not_owner(message):
        return
    await message.answer(FORMAT_EXAMPLE)


@router.message(F.text == "🔗 Суперссылка")
async def store_link(message: Message) -> None:
    if await deny_if_not_owner(message):
        return
    await message.answer(
        "Твоя суперссылка для клиентов:\n"
        f"{STORE_LINK}"
    )


@router.message(F.text)
async def handle_list(message: Message) -> None:
    if await deny_if_not_owner(message):
        return

    text = message.text or ""
    if "тариф" not in text.casefold():
        await message.answer(
            "Не вижу строки «Тариф: ... руб/мес».\n\n" + FORMAT_EXAMPLE
        )
        return

    try:
        groups = parse_tariffs(text)
    except ParseError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    total = count_numbers(groups)
    status = await message.answer(
        f"Нашёл тарифов: {len(groups)}\n"
        f"Нашёл номеров: {total}\n\n"
        "Создаю посты и сторис…"
    )

    try:
        result = await asyncio.to_thread(
            render_all,
            groups,
            message.from_user.id if message.from_user else 0,
            CALL_TO_ACTION,
        )

        await status.edit_text(
            f"Готово ✅\n\n"
            f"Постов: {len(result.post_files)}\n"
            f"Сторис: {len(result.story_files)}\n"
            f"Номеров: {total}"
        )

        # Превью первого поста
        await message.answer_photo(
            FSInputFile(result.post_files[0]),
            caption="Превью первой карточки. Все материалы — в архиве ниже.",
        )

        await message.answer_document(
            FSInputFile(result.archive_file),
            caption=(
                "📦 Готовый архив без потери качества:\n"
                "• квадратные посты;\n"
                "• сторис;\n"
                "• описание;\n"
                "• список номеров."
            ),
        )

        description = result.description_file.read_text(encoding="utf-8")
        await message.answer(
            "Готовое описание для публикации:\n\n" + description
        )

    except Exception:
        logging.exception("Ошибка генерации материалов")
        await status.edit_text(
            "Не удалось создать материалы. Проверь список и попробуй ещё раз."
        )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Скопируй .env.example в .env и вставь токен BotFather."
        )

    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
