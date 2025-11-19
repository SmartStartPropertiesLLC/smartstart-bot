# bot.py
import os
import re
import asyncio
import uuid
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo
)
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.middlewares.base import BaseMiddleware

# ================== LOGGING ===============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("smartstart-bot")
# =============================================

# ================== SETTINGS =================
# Prefer setting via environment variables in production.
BOT_TOKEN  = os.getenv("BOT_TOKEN")  or "7532283206:AAEby_p_TajuYzFvwdvqHUyn3e2h_xZmxBs"
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or -1002492331025)  # numeric id like -100...
ADMIN_IDS  = {int(x) for x in (os.getenv("ADMIN_IDS") or "290746735").split(",") if x.strip()}
# =============================================

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()

# In-memory submission storage
SUBMISSIONS: Dict[str, Dict[str, Any]] = {}

# ------------- Helpers: contact parsing -------------

def extract_phone(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r'(\+?\d[\d \-\(\)]{6,}\d)', text)
    return m.group(1).strip() if m else None

def extract_contact_line(text: str) -> Optional[str]:
    """
    Look for an explicit contact line like:
    Contact:/Contacts:/Agent:/WhatsApp:
    If not found, try to detect @username / t.me/... / phone in free text.
    """
    if not text:
        return None
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip().lower() in {
                "contact", "contacts", "agent", "whatsapp",
                "контакт", "контакты", "агент"
            }:
                return v.strip()
    if re.search(r'(@[A-Za-z0-9_]{5,}|t\.me/[A-Za-z0-9_]{5,})', text, re.I) or extract_phone(text):
        return text
    return None

def parse_contact_target(contact_line: str) -> dict:
    """Return {'tg_username': ...} / {'phone': ...} / {'both': (user, phone)}."""
    if not contact_line:
        return {}
    res = {}
    u1 = re.search(r'@([A-Za-z0-9_]{5,})', contact_line)
    u2 = re.search(r't\.me/([A-Za-z0-9_]{5,})', contact_line, re.I)
    username = (u1.group(1) if u1 else (u2.group(1) if u2 else None))
    phone = extract_phone(contact_line)
    if username and phone:
        res["both"] = (username, re.sub(r'[^+\d]', '', phone))
    elif username:
        res["tg_username"] = username
    elif phone:
        res["phone"] = re.sub(r'[^+\d]', '', phone)
    return res

def build_contact_kb_or_none(source_text: str) -> Optional[InlineKeyboardMarkup]:
    """
    Build contact keyboard if a contact is present.
    Uses https://t.me/<user> and https://wa.me/<digits>.
    """
    contact_line = extract_contact_line(source_text or "")
    if not contact_line:
        return None

    target = parse_contact_target(contact_line)
    buttons: List[List[InlineKeyboardButton]] = []

    # Telegram
    if "both" in target or "tg_username" in target:
        username = target["both"][0] if "both" in target else target["tg_username"]
        buttons.append([InlineKeyboardButton(text="💬 Contact on Telegram", url=f"https://t.me/{username}")])

    # WhatsApp from phone
    phone_raw = target.get("phone") or (target.get("both")[1] if "both" in target else None)
    if phone_raw:
        digits = re.sub(r"\D", "", phone_raw)
        if len(digits) >= 9:
            buttons.append([InlineKeyboardButton(text="🟢 Message on WhatsApp", url=f"https://wa.me/{digits}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

def strip_contacts_from_text(text: str) -> str:
    """Remove explicit contact lines and inline @user/t.me/phone from the body text."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip().lower() in {
                "contact", "contacts", "agent", "whatsapp",
                "контакт", "контакты", "агент"
            }:
                continue
        lines.append(line)

    clean = "\n".join(lines)
    clean = re.sub(r'@[\w]{5,}', '', clean)
    clean = re.sub(r'https?://t\.me/[\w]{5,}', '', clean, flags=re.I)
    clean = re.sub(r'\bt\.me/[\w]{5,}', '', clean, flags=re.I)
    clean = re.sub(r'(\+?\d[\d \-\(\)]{6,}\d)', '', clean)
    clean = re.sub(r'[ \t]{2,}', ' ', clean)
    clean = re.sub(r'\n{3,}', '\n\n', clean).strip()
    return clean

# ------------- Helpers: price/kv parsing -------------

def parse_aed_amount(s: str) -> Optional[int]:
    """Return integer AED amount from strings like 'AED 3,050,000' or '3 050 000'."""
    if not s:
        return None
    m = re.search(r'(\d[\d\., ]*)', s)
    if not m:
        return None
    digits = re.sub(r'[^\d]', '', m.group(1))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None

def format_price_aed(amount: Optional[int]) -> str:
    """Format 3050000 -> 'AED 3 050 000' (spaces as thousands separator)."""
    if not amount:
        return ""
    return "AED " + f"{amount:,}".replace(",", " ")

# ------------- Listing template (EN, with Project & Payment plan, no Description) -------------

def render_listing(author_name: str, raw_text: str) -> str:
    """
    Renders a clean listing text in English.
    Accepts free text and/or key:value lines.
    Has Project and Payment plan fields, no Description block.
    """
    src_original = (raw_text or "").strip()
    one_line = " ".join(src_original.split())

    # Parse key:value lines into dict with lowercased keys
    def parse_kv():
        m: Dict[str, str] = {}
        for line in src_original.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                m[k.strip().lower()] = v.strip()
        return m

    data = parse_kv()

    def pick(*keys, default=""):
        for k in keys:
            if k in data and data[k]:
                return data[k]
        return default

    def rx(p, flags=re.I):
        m = re.search(p, one_line, flags)
        return m.group(1).strip() if m else ""

    # Fields
    location  = pick("location","район","community","district","локация") or rx(
        r'\b(dubai marina|jvc|downtown|business bay|diera|creek|palm|dubai hills|jbr|jvt|mudon|mirdif|sobha hartland|arabian ranches)\b'
    )
    project   = pick("project","name","title","заголовок","проект","object")
    bedrooms  = pick("bedrooms","bedroom","br","спальни") or rx(r'(\d+)\s*(br|bed|bedroom|bedrooms|спальн)')
    bathrooms = pick("bathrooms","baths","wc","санузлы") or rx(r'(\d+)\s*(bath|baths|wc|сануз)')
    area      = pick("area","size","площадь") or rx(r'(\d[\d\., ]{2,})\s*(sq\.?ft|sqft|sqm|m2)')
    if area and not re.search(r'(sq\.?ft|sqft|sqm|m2)', area, re.I):
        area = f"{area} sqft"

    price_src = pick("price","цена","стоимость") or rx(r'(\d[\d\., ]{2,})\s*(aed|dirham|د\.إ|dh)?')
    price_amount    = parse_aed_amount(price_src or one_line)
    price_formatted = format_price_aed(price_amount) if price_amount else ""

    status    = pick("status","статус") or (
        "Off-plan" if re.search(r'off-?plan', one_line, re.I)
        else ("Vacant" if re.search(r'vacant|ready', one_line, re.I) else "")
    )
    parking     = pick("parking","парковка") or rx(r'parking[:\s]*([0-9]+|yes|no)')
    furnishing  = pick("furnishing","furnished","мебель") or rx(r'(furnished|unfurnished|partly furnished)')
    view        = pick("view","вид")
    floor       = pick("floor","этаж")
    handover    = pick("handover") or rx(r'(q[1-4]\s*\d{4})', re.I)
    payment_plan= pick("payment plan","payment","installment","installments","рассрочка","платежный план")

    # Headline: Location — Project (if project is present)
    headline_left  = location or "Dubai"
    headline_right = project.strip() if project else ""
    headline = f"{headline_left} — {headline_right}" if headline_right else headline_left

    # Keyline
    keyline_parts: List[str] = []
    if bedrooms:
        keyline_parts.append(
            bedrooms if str(bedrooms).lower() == "studio" else f"{bedrooms} BR"
        )
    if area:
        keyline_parts.append(area.replace("sq ft", "sqft"))
    if price_formatted:
        keyline_parts.append(price_formatted)
    keyline_txt = " | ".join(keyline_parts) if keyline_parts else ""

    # Property bullets (Project + Payment plan)
    props: List[str] = []
    if location:       props.append(f"📍 <b>Location:</b> {location}")
    if project:        props.append(f"🏢 <b>Project:</b> {project}")
    if price_formatted:props.append(f"💰 <b>Price:</b> {price_formatted}")
    if area:           props.append(f"📐 <b>Area:</b> {area}")
    if bedrooms:       props.append(f"🛏️ <b>Bedrooms:</b> {bedrooms}")
    if bathrooms:      props.append(f"🛁 <b>Bathrooms:</b> {bathrooms}")
    if status:         props.append(f"🏗️ <b>Status:</b> {status}")
    if handover:       props.append(f"⏳ <b>Handover:</b> {handover}")
    if parking:        props.append(f"🅿️ <b>Parking:</b> {parking}")
    if furnishing:     props.append(f"🧺 <b>Furnishing:</b> {furnishing}")
    if view:           props.append(f"🌇 <b>View:</b> {view}")
    if floor:          props.append(f"⬆️ <b>Floor:</b> {floor}")
    if payment_plan:   props.append(f"💳 <b>Payment plan:</b> {payment_plan}")

    parts: List[str] = [
        f"🏢 <b>{headline}</b>",
        keyline_txt,
        "",
        "\n".join(props) if props else "",
    ]
    return "\n".join([p for p in parts if p.strip()])

# ------------- Album middleware (dedupe) -------------

class AlbumMiddleware(BaseMiddleware):
    """
    Collects media group messages and ensures single handler call per group.
    """
    def __init__(self, delay: float = 1.0):
        super().__init__()
        self.delay = delay
        self._buckets: Dict[str, Dict[str, Any]] = {}

    async def __call__(self, handler, event, data):
        if not (isinstance(event, types.Message) and event.media_group_id):
            return await handler(event, data)

        key = f"{event.chat.id}:{event.media_group_id}"
        bucket = self._buckets.get(key)
        if not bucket:
            bucket = {
                "messages": [],
                "until": datetime.now() + timedelta(seconds=self.delay),
                "processed": False
            }
            self._buckets[key] = bucket

        bucket["messages"].append(event)
        await asyncio.sleep(self.delay)

        if bucket.get("processed"):
            return
        if datetime.now() < bucket["until"]:
            return

        bucket["processed"] = True
        msgs = list(bucket["messages"])
        self._buckets.pop(key, None)

        data["album_messages"] = msgs
        return await handler(event, data)

dp.message.middleware(AlbumMiddleware())

# ------------- Submission build -------------

def _uniq_keep_order(items: List[str]) -> List[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

async def make_submission(author: types.User, text: str, media: List[types.Message]) -> str:
    submission_id = uuid.uuid4().hex
    photos: List[str] = []
    videos: List[str] = []
    for msg in media:
        if msg.photo:
            photos.append(msg.photo[-1].file_id)
        elif msg.video:
            videos.append(msg.video.file_id)
    photos = _uniq_keep_order(photos)
    videos = _uniq_keep_order(videos)

    SUBMISSIONS[submission_id] = {
        "author_id":   author.id,
        "author_name": author.full_name,
        "text":        text or "",
        "photos":      photos,
        "videos":      videos,
    }
    return submission_id

# ------------- Moderation flow -------------

async def send_to_moderators(submission_id: str):
    data = SUBMISSIONS.get(submission_id)
    if not data:
        return
    author = data["author_name"]
    text   = data["text"]
    html_preview = render_listing(author, text)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"approve:{submission_id}"),
        InlineKeyboardButton(text="❌ Reject",  callback_data=f"reject:{submission_id}")
    ]])

    medias: List[types.InputMedia] = []
    for p in data["photos"]:
        medias.append(InputMediaPhoto(media=p))
    for v in data["videos"]:
        medias.append(InputMediaVideo(media=v))

    for admin_id in ADMIN_IDS:
        if len(medias) > 1:
            await bot.send_media_group(admin_id, medias)
        elif len(medias) == 1:
            m0 = medias[0]
            if isinstance(m0, InputMediaPhoto):
                await bot.send_photo(admin_id, m0.media)
            else:
                await bot.send_video(admin_id, m0.media)

        await bot.send_message(admin_id, f"📝 <b>New submission</b>\n\n{html_preview}", reply_markup=kb)

        # Show preview of contact buttons (if any)
        kb_contact = build_contact_kb_or_none(text)
        if kb_contact:
            await bot.send_message(
                admin_id,
                "🔗 Contact buttons that will appear in the channel:",
                reply_markup=kb_contact,
                disable_web_page_preview=True
            )

async def publish_to_channel(submission_id: str) -> bool:
    data = SUBMISSIONS.get(submission_id)
    if not data:
        return False

    text   = data["text"]
    author = data["author_name"]

    # 1) media
    medias: List[types.InputMedia] = []
    for p in data["photos"]:
        medias.append(InputMediaPhoto(media=p))
    for v in data["videos"]:
        medias.append(InputMediaVideo(media=v))

    if len(medias) > 1:
        await bot.send_media_group(CHANNEL_ID, medias)
    elif len(medias) == 1:
        m0 = medias[0]
        if isinstance(m0, InputMediaPhoto):
            await bot.send_photo(CHANNEL_ID, m0.media)
        else:
            await bot.send_video(CHANNEL_ID, m0.media)

    # 2) text + optional contact KB
    html = render_listing(author, text)
    kb   = build_contact_kb_or_none(text)
    if kb:
        await bot.send_message(CHANNEL_ID, html, reply_markup=kb, disable_web_page_preview=True)
    else:
        await bot.send_message(CHANNEL_ID, html, disable_web_page_preview=True)
    return True

# ------------- Commands -------------

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer(
        "👋 Welcome!\n"
        "Send me TEXT and up to 10 PHOTOS (can be an album). I’ll format a listing (with Project & Payment plan fields) "
        "and send it for moderation.\n\n"
        "To show a “Contact” button, include a contact line, e.g.:\n"
        "Contact: @username or +971 50 123 45 67"
    )

@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await m.answer(
        "Format:\n"
        "• Free text AND/OR lines like 'Key: Value' (see /template)\n"
        "• Photos: 1–10 (album is OK)\n"
        "• “Contact” button appears only if you include a contact.\n"
        "• Use Project: … and Payment plan: … if needed."
    )

@dp.message(Command("template"))
async def cmd_template(m: types.Message):
    example = (
        "📋 Example (with Project & Payment plan):\n\n"
        "Title: 2BR in Marina\n"
        "Location: Dubai Marina\n"
        "Project: Marina Gate\n"
        "Bedrooms: 2\n"
        "Bathrooms: 2\n"
        "Area: 1,210 sqft\n"
        "Price: AED 3 050 000\n"
        "Status: Vacant\n"
        "Parking: 1\n"
        "Furnishing: Unfurnished\n"
        "View: Sea\n"
        "Floor: High\n"
        "Handover: Q4 2025\n"
        "Payment plan: 70/30 on handover\n"
        "Contact: @broker_name, +971 50 123 45 67"
    )
    await m.answer(example)

@dp.message(Command("ping"))
async def cmd_ping(m: types.Message):
    await m.answer("pong ✅")

# ------------- Message handlers -------------

@dp.message(F.media_group_id, flags={"allow_album": True})
async def handle_album(m: types.Message, album_messages: List[types.Message]):
    caption = next((msg.caption for msg in album_messages if msg.caption), "")
    submission_id = await make_submission(m.from_user, caption, album_messages)
    await send_to_moderators(submission_id)
    await m.answer("✅ Sent for moderation. Please wait for approval.")

@dp.message((F.photo | F.video) & ~F.media_group_id)
async def handle_single_media(m: types.Message):
    if m.media_group_id:  # extra guard
        return
    submission_id = await make_submission(m.from_user, m.caption or "", [m])
    await send_to_moderators(submission_id)
    await m.answer("✅ Sent for moderation. Please wait for approval.")

@dp.message(F.text)
async def handle_text(m: types.Message):
    submission_id = await make_submission(m.from_user, m.text, [])
    await send_to_moderators(submission_id)
    await m.answer("✅ Text sent for moderation.")

@dp.message()
async def handle_other(m: types.Message):
    await m.answer("Please send text and/or photos/videos (albums supported). See /template.")

# ------------- Callbacks -------------

@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(c: types.CallbackQuery):
    submission_id = c.data.split(":", 1)[1]
    ok = await publish_to_channel(submission_id)
    if ok:
        await c.message.edit_text(c.message.html_text + "\n\n✅ Published.")
        data = SUBMISSIONS.pop(submission_id, None)
        if data:
            try:
                await bot.send_message(data["author_id"], "🎉 Your listing has been published.")
            except Exception as e:
                log.warning(f"Notify author failed: {e}")
    else:
        await c.answer("Error: submission not found.", show_alert=True)

@dp.callback_query(F.data.startswith("reject:"))
async def cb_reject(c: types.CallbackQuery):
    submission_id = c.data.split(":", 1)[1]
    data = SUBMISSIONS.pop(submission_id, None)
    await c.message.edit_text(c.message.html_text + "\n\n❌ Rejected.")
    if data:
        try:
            await bot.send_message(data["author_id"], "❌ Your listing was rejected.")
        except Exception as e:
            log.warning(f"Notify author failed: {e}")

# ------------- Runner -------------

async def main():
    log.info("✅ Bot starting…")
    log.info(f"CHANNEL_ID: {CHANNEL_ID}, ADMIN_IDS: {ADMIN_IDS}")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (if was set).")
    except Exception as e:
        log.warning(f"delete_webhook warning: {e}")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("🛑 Bot stopped")

