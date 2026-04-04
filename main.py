import os
import math
import tempfile
import subprocess
import httpx
import asyncio
import io
import urllib.parse
from datetime import date
from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw, ImageFont

from config import settings

app = FastAPI()

# ─── Video constants ──────────────────────────────────────────────────────────
WIDTH        = 720
HEIGHT       = 1280
FPS          = 30
BG_COLOR     = (0, 0, 0)
TEXT_COLOR   = (255, 255, 255)
RED_COLOR    = (220, 50, 50)
FONT_SIZE    = 72

# Layout for /photo-essay
IMG_PANEL_H  = int(HEIGHT * 2 / 3)
WORD_PANEL_H = HEIGHT - IMG_PANEL_H
IMG_DURATION = 2.0

KB_ZOOM_START = 1.0
KB_ZOOM_END   = 1.08

# ─── /day endpoint constants ──────────────────────────────────────────────────
DAY_YELLOW        = (255, 204, 0)
DAY_BG            = (0, 0, 0)
DAY_REVEAL_SEC    = 3.0          # total seconds for all elements to reveal
DAY_HOLD_SEC      = 1.5          # seconds to hold on completed frame
DAY_REVEAL_FRAMES = int(DAY_REVEAL_SEC * FPS)   # 90 frames
DAY_HOLD_FRAMES   = int(DAY_HOLD_SEC  * FPS)    # 45 frames
DAY_TOTAL_FRAMES  = DAY_REVEAL_FRAMES + DAY_HOLD_FRAMES

# Font sizes for /day
DAY_FS_LABEL      = 52    # "Today is"
DAY_FS_DATE       = 160   # "April 4"
DAY_FS_YEAR       = 110   # "2026"
DAY_FS_TAGLINE    = 72    # tagline lines

# ─── Upstash Redis key ────────────────────────────────────────────────────────
REDIS_SEEN_IMAGES_KEY = "photo_essay:seen_image_urls"

# ─── Supported gTTS language codes ────────────────────────────────────────────
SUPPORTED_AUDIO_LANGS = {
    "en": "English",
    "hi": "Hindi",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ar": "Arabic",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "it": "Italian",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "mr": "Marathi",
    "gu": "Gujarati",
    "ur": "Urdu",
}
DEFAULT_AUDIO_LANG = "en"


# ─── Upstash Redis helpers ────────────────────────────────────────────────────

def _upstash_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }

async def image_url_is_seen(client: httpx.AsyncClient, url: str) -> bool:
    try:
        encoded_url = urllib.parse.quote(url, safe="")
        r = await client.post(
            f"{settings.UPSTASH_REDIS_REST_URL}/sismember/{REDIS_SEEN_IMAGES_KEY}/{encoded_url}",
            headers=_upstash_headers(),
            timeout=5,
        )
        return r.json().get("result") == 1
    except Exception as e:
        print(f"[Redis] sismember error (non-fatal): {e}")
        return False

async def mark_image_urls_seen(client: httpx.AsyncClient, urls: list[str]) -> None:
    if not urls:
        return
    try:
        encoded = "/".join(urllib.parse.quote(u, safe="") for u in urls)
        r = await client.post(
            f"{settings.UPSTASH_REDIS_REST_URL}/sadd/{REDIS_SEEN_IMAGES_KEY}/{encoded}",
            headers=_upstash_headers(),
            timeout=5,
        )
        print(f"[Redis] sadd image URLs result: {r.json()}")
    except Exception as e:
        print(f"[Redis] sadd error (non-fatal): {e}")


# ─── Phrase grouper ───────────────────────────────────────────────────────────

def group_into_phrases(words: list[str], max_words: int = 3) -> list[str]:
    phrases = []
    i = 0
    while i < len(words):
        first = words[i]
        if len(first) <= 3:
            chunk = max_words
        else:
            chunk = min(2, max_words)
        group = words[i : i + chunk]
        phrases.append(" ".join(group))
        i += chunk
    return phrases


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_pivot_index(word: str) -> int:
    first_token = word.split()[0] if " " in word else word
    clean = ''.join(c for c in first_token if c.isalpha())
    if not clean:
        return 0
    n = len(clean)
    if n == 1:   return 0
    elif n <= 5: return 1
    elif n <= 9: return 2
    else:        return 3


def find_font(size: int, lang: str = "en") -> ImageFont.FreeTypeFont:
    indic_langs = {"hi", "mr", "ne", "sa", "mai", "kok"}
    other_indic = {"bn", "gu", "ta", "te", "ur", "pa", "si", "km", "lo", "my"}

    if lang in indic_langs:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    elif lang in other_indic:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        ]

    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def find_fitting_font(
    text: str,
    lang: str,
    panel_w: int,
    max_size: int = FONT_SIZE,
    min_size: int = 24,
    padding: int = 40,
) -> ImageFont.FreeTypeFont:
    available_w = panel_w - padding * 2
    size = max_size
    while size >= min_size:
        font = find_font(size, lang)
        dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        bbox = dummy.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        if text_w <= available_w:
            return font
        size -= 4
    return find_font(min_size, lang)


def _tw(draw, text, font) -> int:
    if not text:
        return 0
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0]


def _th(draw, font) -> int:
    b = draw.textbbox((0, 0), "Ag", font=font)
    return b[3] - b[1]


# ─── Word-panel renderer ──────────────────────────────────────────────────────

def render_word_panel(word: str, font, panel_w: int, panel_h: int, lang: str = "en") -> Image.Image:
    img  = Image.new("RGB", (panel_w, panel_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    is_phrase = " " in word
    if is_phrase:
        font = find_fitting_font(word, lang, panel_w, max_size=font.size if hasattr(font, "size") else FONT_SIZE)

    if is_phrase:
        tokens = word.split()
        first  = tokens[0]
        rest   = " ".join(tokens[1:])

        pivot_idx      = get_pivot_index(first)
        alpha_count    = 0
        pivot_char_idx = 0
        for i, ch in enumerate(first):
            if ch.isalpha():
                if alpha_count == pivot_idx:
                    pivot_char_idx = i
                    break
                alpha_count += 1

        before     = first[:pivot_char_idx]
        pivot_char = first[pivot_char_idx] if pivot_char_idx < len(first) else ""
        after_word = first[pivot_char_idx + 1:] if pivot_char_idx + 1 < len(first) else ""
        after_full = after_word + (" " + rest if rest else "")

        w_b = _tw(draw, before, font)
        w_p = _tw(draw, pivot_char, font) if pivot_char else 0
        w_a = _tw(draw, after_full, font)
        h   = _th(draw, font)

        x = (panel_w - w_b - w_p - w_a) // 2
        y = (panel_h - h) // 2

        if before:
            draw.text((x, y), before, font=font, fill=TEXT_COLOR);  x += w_b
        if pivot_char:
            draw.text((x, y), pivot_char, font=font, fill=RED_COLOR); x += w_p
        if after_full:
            draw.text((x, y), after_full, font=font, fill=TEXT_COLOR)

    else:
        pivot_idx      = get_pivot_index(word)
        alpha_count    = 0
        pivot_char_idx = 0
        for i, ch in enumerate(word):
            if ch.isalpha():
                if alpha_count == pivot_idx:
                    pivot_char_idx = i
                    break
                alpha_count += 1

        before     = word[:pivot_char_idx]
        pivot_char = word[pivot_char_idx] if pivot_char_idx < len(word) else ""
        after      = word[pivot_char_idx + 1:] if pivot_char_idx + 1 < len(word) else ""

        w_b = _tw(draw, before, font)
        w_p = _tw(draw, pivot_char, font) if pivot_char else 0
        w_a = _tw(draw, after, font)
        h   = _th(draw, font)

        x = (panel_w - w_b - w_p - w_a) // 2
        y = (panel_h - h) // 2

        if before:
            draw.text((x, y), before, font=font, fill=TEXT_COLOR);  x += w_b
        if pivot_char:
            draw.text((x, y), pivot_char, font=font, fill=RED_COLOR); x += w_p
        if after:
            draw.text((x, y), after, font=font, fill=TEXT_COLOR)

    cx, tw2 = panel_w // 2, 3
    tick = (180, 30, 30)
    draw.rectangle([cx - tw2//2, 6,            cx + tw2//2, 20],           fill=tick)
    draw.rectangle([cx - tw2//2, panel_h - 20, cx + tw2//2, panel_h - 6], fill=tick)

    return img


def render_word_frame_full(word: str, font, lang: str = "en") -> Image.Image:
    return render_word_panel(word, font, WIDTH, HEIGHT, lang)


# ─── Image utilities ──────────────────────────────────────────────────────────

def fit_image_to_panel(img: Image.Image, pw: int, ph: int) -> Image.Image:
    sw, sh = img.size
    scale  = max(pw / sw, ph / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img    = img.resize((nw, nh), Image.LANCZOS)
    l, t   = (nw - pw) // 2, (nh - ph) // 2
    return img.crop((l, t, l + pw, t + ph))


def apply_ken_burns(
    base_img: Image.Image,
    frame_in_image: int,
    total_frames_for_image: int,
    panel_w: int,
    panel_h: int,
) -> Image.Image:
    if total_frames_for_image <= 1:
        t = 0.0
    else:
        t = frame_in_image / (total_frames_for_image - 1)

    zoom = KB_ZOOM_START + (KB_ZOOM_END - KB_ZOOM_START) * t

    bw, bh = base_img.size
    crop_w = int(panel_w / zoom)
    crop_h = int(panel_h / zoom)

    cx = bw // 2
    cy = bh // 2
    left   = max(0, cx - crop_w // 2)
    top    = max(0, cy - crop_h // 2)
    right  = left + crop_w
    bottom = top  + crop_h

    right  = min(right,  bw)
    bottom = min(bottom, bh)

    cropped = base_img.crop((left, top, right, bottom))
    return cropped.resize((panel_w, panel_h), Image.LANCZOS)


def _prepare_ken_burns_base(img: Image.Image, pw: int, ph: int) -> Image.Image:
    margin = KB_ZOOM_END
    sw, sh = img.size
    scale  = max(pw * margin / sw, ph * margin / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    return img.resize((nw, nh), Image.LANCZOS)


async def fetch_image_urls_from_supabase(limit: int) -> list[str]:
    url = (
        f"{settings.SUPABASE_URL}/rest/v1/news"
        f"?select=image&image=not.is.null&image=neq."
        f"&order=created_at.desc&limit={limit}"
    )
    headers = {
        "apikey":        settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        print(f"Supabase error {resp.status_code}: {resp.text}")
        return []
    return [r["image"] for r in resp.json() if r.get("image")]


async def fetch_fresh_image_urls(images_needed: int) -> list[str]:
    fresh_urls: list[str] = []
    fetch_limit = min(images_needed + 10, 50)

    async with httpx.AsyncClient(timeout=20) as client:
        while len(fresh_urls) < images_needed and fetch_limit <= 200:
            all_urls = await fetch_image_urls_from_supabase(fetch_limit)
            if not all_urls:
                break

            fresh_urls = []
            for url in all_urls:
                if not await image_url_is_seen(client, url):
                    fresh_urls.append(url)
                else:
                    print(f"[Redis] skip (seen image): {url}")

            if len(fresh_urls) >= images_needed:
                break

            if fetch_limit >= len(all_urls):
                break
            fetch_limit = min(fetch_limit * 2, 200)

    print(f"[Redis] {len(fresh_urls)} fresh image URL(s) found")
    return fresh_urls[:images_needed + 5]


async def download_image(url: str, client: httpx.AsyncClient) -> Image.Image | None:
    try:
        r = await client.get(url, timeout=15, follow_redirects=True)
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f"Image download failed ({url}): {e}")
    return None


# ─── Audio generation ─────────────────────────────────────────────────────────

def generate_tts_audio(text: str, output_mp3: str, lang: str = "en") -> bool:
    try:
        from gtts import gTTS, lang as gtts_lang
        available = gtts_lang.tts_langs()
        if lang not in available:
            print(f"[TTS] Language '{lang}' not supported by gTTS — falling back to 'en'")
            lang = "en"
        tts = gTTS(text=text, lang=lang, slow=False)
        tts.save(output_mp3)
        return True
    except Exception as e:
        print(f"gTTS error: {e}")
        return False


def merge_audio_video(video_path: str, audio_path: str, output_path: str) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-map", "0:v:0",
        "-map", "1:a:0",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg merge error:\n{result.stderr}")
        return False
    return True


# ─── Video builders ───────────────────────────────────────────────────────────

def _pil_to_bgr(img: Image.Image):
    import cv2, numpy as np
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _cv2_writer(path: str):
    import cv2
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    return cv2.VideoWriter(path, fourcc, FPS, (WIDTH, HEIGHT))


def create_video(words: list[str], wpm: int, output_path: str, lang: str = "en"):
    font            = find_font(FONT_SIZE, lang)
    frames_per_word = max(1, round(FPS * 60.0 / wpm))
    writer          = _cv2_writer(output_path)
    for word in words:
        bgr = _pil_to_bgr(render_word_frame_full(word, font, lang))
        for _ in range(frames_per_word):
            writer.write(bgr)
    writer.release()


def create_photo_essay_video(
    words: list[str],
    wpm: int,
    images: list[Image.Image],
    output_path: str,
    lang: str = "en",
):
    font             = find_font(FONT_SIZE, lang)
    frames_per_word  = max(1, round(FPS * 60.0 / wpm))
    frames_per_image = round(FPS * IMG_DURATION)

    kb_bases = [_prepare_ken_burns_base(im, WIDTH, IMG_PANEL_H) for im in images]

    writer    = _cv2_writer(output_path)
    frame_idx = 0

    for word in words:
        word_panel = render_word_panel(word, font, WIDTH, WORD_PANEL_H, lang)

        for f in range(frames_per_word):
            abs_frame      = frame_idx + f
            img_idx        = (abs_frame // frames_per_image) % len(kb_bases)
            frame_in_image = abs_frame % frames_per_image

            top_panel = apply_ken_burns(
                kb_bases[img_idx],
                frame_in_image,
                frames_per_image,
                WIDTH,
                IMG_PANEL_H,
            )

            composite = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
            composite.paste(top_panel,  (0, 0))
            composite.paste(word_panel, (0, IMG_PANEL_H))

            writer.write(_pil_to_bgr(composite))

        frame_idx += frames_per_word

    writer.release()


# ─── Audio post-processing helper ─────────────────────────────────────────────

def apply_audio_if_requested(
    silent_video: str,
    words_text: str,
    audio: bool,
    audio_lang: str = "en",
) -> str:
    if not audio:
        return silent_video

    mp3_path   = silent_video.replace(".mp4", "_audio.mp3")
    final_path = silent_video.replace(".mp4", "_final.mp4")

    ok = generate_tts_audio(words_text, mp3_path, lang=audio_lang)
    if not ok:
        print("[audio] TTS failed — sending silent video")
        return silent_video

    ok = merge_audio_video(silent_video, mp3_path, final_path)

    for p in [mp3_path, silent_video]:
        try:
            os.remove(p)
        except Exception:
            pass

    if ok:
        return final_path
    else:
        print("[audio] ffmpeg merge failed")
        return final_path


# ─── Telegram sender ──────────────────────────────────────────────────────────

async def send_video_to_telegram(video_path: str):
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_TOKEN}/sendVideo"
    async with httpx.AsyncClient(timeout=180) as client:
        with open(video_path, "rb") as f:
            resp = await client.post(
                url,
                data={"chat_id": settings.TELEGRAM_CHAT_ID},
                files={"video": ("essay.mp4", f, "video/mp4")},
            )
    return resp.status_code, resp.text


# ─── Background task processors ───────────────────────────────────────────────

async def process_essay(
    words_text: str,
    rate: int,
    audio: bool,
    phrase_mode: bool = False,
    audio_lang: str = "en",
    display_lang: str = "en",
):
    raw_words = words_text.split()
    if not raw_words:
        return

    display_units = group_into_phrases(raw_words) if phrase_mode else raw_words

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        silent_path = tmp.name

    final_path = silent_path
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, create_video, display_units, rate, silent_path, display_lang
        )

        if audio:
            final_path = await loop.run_in_executor(
                None, apply_audio_if_requested, silent_path, words_text, True, audio_lang
            )

        if not os.path.exists(final_path):
            print("[/essay] Final video missing — aborting send")
            return

        status, body = await send_video_to_telegram(final_path)
        print(f"[/essay] Telegram {status}: {body}")
    finally:
        for p in {silent_path, final_path}:
            try:
                os.remove(p)
            except Exception:
                pass


async def process_photo_essay(
    words_text: str,
    rate: int,
    audio: bool,
    phrase_mode: bool = False,
    audio_lang: str = "en",
    display_lang: str = "en",
):
    raw_words = words_text.split()
    if not raw_words:
        return

    display_units = group_into_phrases(raw_words) if phrase_mode else raw_words

    total_seconds = len(raw_words) / rate * 60
    images_needed = max(1, math.ceil(total_seconds / IMG_DURATION))

    print(
        f"[/photo-essay] {len(raw_words)} words @ {rate} wpm → {total_seconds:.1f}s "
        f"→ need {images_needed} images | phrase_mode={phrase_mode} | "
        f"audio_lang={audio_lang} | display_lang={display_lang}"
    )

    image_urls = await fetch_fresh_image_urls(images_needed)

    if not image_urls:
        print("[/photo-essay] No fresh images — falling back to plain essay")
        await process_essay(words_text, rate, audio, phrase_mode, audio_lang, display_lang)
        return

    sem = asyncio.Semaphore(4)
    async def guarded(url, client):
        async with sem:
            return url, await download_image(url, client)

    async with httpx.AsyncClient(timeout=20) as session:
        results = await asyncio.gather(*[guarded(u, session) for u in image_urls])

    images: list[Image.Image] = []
    successfully_downloaded_urls: list[str] = []
    for url, im in results:
        if im is not None:
            images.append(im)
            successfully_downloaded_urls.append(url)

    if not images:
        print("[/photo-essay] All downloads failed — falling back to plain essay")
        await process_essay(words_text, rate, audio, phrase_mode, audio_lang, display_lang)
        return

    try:
        async with httpx.AsyncClient(timeout=10) as redis_client:
            await mark_image_urls_seen(redis_client, successfully_downloaded_urls)
        print(f"[Redis] Marked {len(successfully_downloaded_urls)} image URL(s) as seen")
    except Exception as e:
        print(f"[Redis] mark-seen failed (non-fatal): {e}")

    while len(images) < images_needed:
        images = (images * 2)[:images_needed]

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        silent_path = tmp.name

    final_path = silent_path
    try:
        loop = asyncio.get_event_loop()

        await loop.run_in_executor(
            None, create_photo_essay_video,
            display_units, rate, images, silent_path, display_lang
        )

        if audio:
            final_path = await loop.run_in_executor(
                None, apply_audio_if_requested,
                silent_path, words_text, True, audio_lang
            )

        if not os.path.exists(final_path):
            print("[/photo-essay] Final video missing — aborting send")
            return

        status, body = await send_video_to_telegram(final_path)
        print(f"[/photo-essay] Telegram {status}: {body}")
    finally:
        for p in {silent_path, final_path}:
            try:
                os.remove(p)
            except Exception:
                pass


# ─── /day: layout helpers ─────────────────────────────────────────────────────

def _measure_text(text: str, font) -> tuple[int, int]:
    """Return (width, height) of text rendered with font."""
    dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bb = dummy.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _build_day_layout() -> list[dict]:
    """
    Pre-compute every element's text, font, and final (x, y) position
    for the /day frame.  Returns a list of element dicts ordered top→bottom.

    Elements (matching the reference image):
        0  "Today is"                    — small label
        1  "April 4"  (month + day)      — large
        2  "2026"                        — medium
        3  gap
        4  "And this  daily"             — tagline line 1   (two words)
        5  "market briefing"             — tagline line 2
        6  "for those who"               — tagline line 3
        7  "play to win."                — tagline line 4

    Layout is calculated once and cached so every frame render is cheap.
    """
    today      = date.today()
    month_day  = f"{today.strftime('%B')} {today.day}"   # e.g. "April 4"
    year_str   = str(today.year)

    font_label   = find_fitting_font("Today is",   "en", WIDTH, max_size=DAY_FS_LABEL,   padding=80)
    font_date    = find_fitting_font(month_day,     "en", WIDTH, max_size=DAY_FS_DATE,    padding=60)
    font_year    = find_fitting_font(year_str,      "en", WIDTH, max_size=DAY_FS_YEAR,    padding=80)
    font_tagline = find_fitting_font("market briefing", "en", WIDTH, max_size=DAY_FS_TAGLINE, padding=60)

    tagline_lines = [
        "And this  daily",
        "market briefing",
        "for those who",
        "play to win.",
    ]

    LINE_GAP   = 18   # px between consecutive lines
    BLOCK_GAP  = 70   # px between date-block and tagline-block

    # Measure all elements
    elements = []
    for text, font in [
        ("Today is",  font_label),
        (month_day,   font_date),
        (year_str,    font_year),
    ]:
        w, h = _measure_text(text, font)
        elements.append({"text": text, "font": font, "w": w, "h": h})

    tagline_elems = []
    for line in tagline_lines:
        w, h = _measure_text(line, font_tagline)
        tagline_elems.append({"text": line, "font": font_tagline, "w": w, "h": h})

    # Total height of date block
    date_block_h = sum(e["h"] for e in elements) + LINE_GAP * (len(elements) - 1)
    # Total height of tagline block
    tag_block_h  = sum(e["h"] for e in tagline_elems) + LINE_GAP * (len(tagline_elems) - 1)
    # Grand total content height
    total_h = date_block_h + BLOCK_GAP + tag_block_h

    # Vertically centre the whole block (leave room for logo at bottom)
    LOGO_RESERVE = 200
    usable_h = HEIGHT - LOGO_RESERVE
    y_start  = (usable_h - total_h) // 2

    # Assign y positions to each element
    y = y_start
    for elem in elements:
        elem["x"] = (WIDTH - elem["w"]) // 2
        elem["y"] = y
        y += elem["h"] + LINE_GAP

    y += BLOCK_GAP - LINE_GAP   # replace last LINE_GAP with BLOCK_GAP

    for elem in tagline_elems:
        elem["x"] = (WIDTH - elem["w"]) // 2
        elem["y"] = y
        y += elem["h"] + LINE_GAP

    all_elements = elements + tagline_elems
    return all_elements


# Cache layout per calendar date so we don't rebuild on every frame
_day_layout_cache: dict = {}

def get_day_layout() -> list[dict]:
    today = date.today().isoformat()
    if today not in _day_layout_cache:
        _day_layout_cache.clear()
        _day_layout_cache[today] = _build_day_layout()
    return _day_layout_cache[today]


def render_day_frame_at(visible_count: int) -> Image.Image:
    """
    Render a /day frame showing the first `visible_count` elements.
    visible_count=0 → blank frame, visible_count=len(layout) → fully revealed.
    """
    layout = get_day_layout()
    img    = Image.new("RGB", (WIDTH, HEIGHT), DAY_BG)
    draw   = ImageDraw.Draw(img)

    for i, elem in enumerate(layout):
        if i >= visible_count:
            break
        draw.text((elem["x"], elem["y"]), elem["text"], font=elem["font"], fill=DAY_YELLOW)

    return img


# ─── /day video builder ───────────────────────────────────────────────────────

def create_day_video(output_path: str) -> None:
    """
    Build the /day intro video:
      - 3 seconds of reveal animation (elements pop in top→bottom, evenly timed)
      - 1.5 seconds hold on the fully-revealed frame
    Total ≈ 4.5 seconds.
    """
    layout       = get_day_layout()
    n_elements   = len(layout)
    writer       = _cv2_writer(output_path)

    # Each element gets an equal share of the reveal window
    frames_per_elem = DAY_REVEAL_FRAMES / n_elements   # may be fractional

    for frame_idx in range(DAY_TOTAL_FRAMES):
        if frame_idx < DAY_REVEAL_FRAMES:
            # How many elements should be visible at this frame?
            visible = int(frame_idx / frames_per_elem) + 1
            visible = min(visible, n_elements)
        else:
            visible = n_elements   # hold phase — everything visible

        img = render_day_frame_at(visible)
        writer.write(_pil_to_bgr(img))

    writer.release()


async def process_day():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        video_path = tmp.name

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, create_day_video, video_path)

        if not os.path.exists(video_path):
            print("[/day] Video file missing — aborting send")
            return

        status, body = await send_video_to_telegram(video_path)
        print(f"[/day] Telegram {status}: {body}")
    finally:
        try:
            os.remove(video_path)
        except Exception:
            pass


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/essay")
async def essay_endpoint(
    background_tasks: BackgroundTasks,
    words:       str = Query(...,   description="Essay text (URL-encoded, supports Unicode/Hindi)"),
    rate:        int = Query(300,   description="Words per minute (50–1000)"),
    audio:       str = Query("f",   description="Add voiceover? t=yes, f=no"),
    phrase:      str = Query("f",   description="Phrase mode (2–3 words at a time)? t=yes, f=no"),
    audiochoice: str = Query("en",  description="gTTS language code for voiceover, e.g. en, hi, es, fr"),
    displaylang: str = Query("",    description="Font language for rendering text, e.g. en, hi (defaults to audiochoice)"),
):
    if not 50 <= rate <= 1000:
        return JSONResponse(status_code=400, content={"error": "rate must be 50–1000"})
    word_list = words.split()
    if not word_list:
        return JSONResponse(status_code=400, content={"error": "No words provided"})

    want_audio   = audio.strip().lower()  == "t"
    phrase_mode  = phrase.strip().lower() == "t"
    audio_lang   = audiochoice.strip().lower() or DEFAULT_AUDIO_LANG
    display_lang = displaylang.strip().lower() or audio_lang

    if audio_lang not in SUPPORTED_AUDIO_LANGS:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"audiochoice '{audio_lang}' not in supported list.",
                "supported": SUPPORTED_AUDIO_LANGS,
            },
        )

    background_tasks.add_task(
        process_essay, words, rate, want_audio, phrase_mode, audio_lang, display_lang
    )
    return {
        "status":            "processing",
        "endpoint":          "/essay",
        "word_count":        len(word_list),
        "rate_wpm":          rate,
        "audio":             want_audio,
        "audio_lang":        audio_lang,
        "display_lang":      display_lang,
        "phrase_mode":       phrase_mode,
        "estimated_seconds": round(len(word_list) / rate * 60, 1),
        "message":           "Video being generated — check Telegram shortly.",
    }


@app.get("/photo-essay")
async def photo_essay_endpoint(
    background_tasks: BackgroundTasks,
    words:       str = Query(...,   description="Essay text (URL-encoded, supports Unicode/Hindi)"),
    rate:        int = Query(300,   description="Words per minute (50–1000)"),
    audio:       str = Query("f",   description="Add voiceover? t=yes, f=no"),
    phrase:      str = Query("f",   description="Phrase mode (2–3 words at a time)? t=yes, f=no"),
    audiochoice: str = Query("en",  description="gTTS language code for voiceover, e.g. en, hi, es, fr"),
    displaylang: str = Query("",    description="Font language for rendering text, e.g. en, hi (defaults to audiochoice)"),
):
    if not 50 <= rate <= 1000:
        return JSONResponse(status_code=400, content={"error": "rate must be 50–1000"})
    word_list = words.split()
    if not word_list:
        return JSONResponse(status_code=400, content={"error": "No words provided"})

    want_audio   = audio.strip().lower()  == "t"
    phrase_mode  = phrase.strip().lower() == "t"
    audio_lang   = audiochoice.strip().lower() or DEFAULT_AUDIO_LANG
    display_lang = displaylang.strip().lower() or audio_lang

    if audio_lang not in SUPPORTED_AUDIO_LANGS:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"audiochoice '{audio_lang}' not in supported list.",
                "supported": SUPPORTED_AUDIO_LANGS,
            },
        )

    total_seconds = len(word_list) / rate * 60
    images_needed = max(1, math.ceil(total_seconds / IMG_DURATION))

    background_tasks.add_task(
        process_photo_essay, words, rate, want_audio, phrase_mode, audio_lang, display_lang
    )
    return {
        "status":            "processing",
        "endpoint":          "/photo-essay",
        "word_count":        len(word_list),
        "rate_wpm":          rate,
        "audio":             want_audio,
        "audio_lang":        audio_lang,
        "display_lang":      display_lang,
        "phrase_mode":       phrase_mode,
        "estimated_seconds": round(total_seconds, 1),
        "images_needed":     images_needed,
        "message":           "Photo-essay video being generated — check Telegram shortly.",
    }


@app.get("/day")
async def day_endpoint(background_tasks: BackgroundTasks):
    """
    Generate a daily intro video. Elements reveal top-to-bottom over 3 seconds,
    then hold on the complete frame for 1.5 seconds. Sent to Telegram when ready.
    """
    today = date.today()
    background_tasks.add_task(process_day)
    return {
        "status":           "processing",
        "endpoint":         "/day",
        "date":             today.isoformat(),
        "reveal_seconds":   DAY_REVEAL_SEC,
        "hold_seconds":     DAY_HOLD_SEC,
        "total_seconds":    DAY_REVEAL_SEC + DAY_HOLD_SEC,
        "message":          "Day intro video being generated — check Telegram shortly.",
    }


@app.get("/languages")
async def list_languages():
    """List all supported audiochoice language codes."""
    return {"supported_languages": SUPPORTED_AUDIO_LANGS}


@app.get("/health")
async def health():
    return {"status": "ok"}