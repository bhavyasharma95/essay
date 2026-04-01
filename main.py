import os
import math
import tempfile
import httpx
import asyncio
import io
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
IMG_PANEL_H  = int(HEIGHT * 2 / 3)   # 853 px  — top image area
WORD_PANEL_H = HEIGHT - IMG_PANEL_H  # 427 px  — bottom word area
IMG_DURATION = 2.0                    # seconds each image is shown


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_pivot_index(word: str) -> int:
    clean = ''.join(c for c in word if c.isalpha())
    if not clean:
        return 0
    length = len(clean)
    if length == 1:   return 0
    elif length <= 5: return 1
    elif length <= 9: return 2
    else:             return 3


def find_font(size: int) -> ImageFont.FreeTypeFont:
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


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    if not text:
        return 0
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _text_height(draw: ImageDraw.ImageDraw, font) -> int:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return bbox[3] - bbox[1]


# ─── Word-panel renderer (shared by both endpoints) ───────────────────────────

def render_word_panel(word: str, font, panel_w: int, panel_h: int) -> Image.Image:
    """Returns an RGB image of (panel_w x panel_h) with the RSVP word centred."""
    img  = Image.new("RGB", (panel_w, panel_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

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

    w_before = _text_width(draw, before, font)
    w_pivot  = _text_width(draw, pivot_char, font) if pivot_char else 0
    w_after  = _text_width(draw, after, font)
    total_w  = w_before + w_pivot + w_after
    h        = _text_height(draw, font)

    x = (panel_w - total_w) // 2
    y = (panel_h - h) // 2

    if before:
        draw.text((x, y), before, font=font, fill=TEXT_COLOR)
        x += w_before
    if pivot_char:
        draw.text((x, y), pivot_char, font=font, fill=RED_COLOR)
        x += w_pivot
    if after:
        draw.text((x, y), after, font=font, fill=TEXT_COLOR)

    # Red tick marks top & bottom centre
    cx, tw = panel_w // 2, 3
    tick = (180, 30, 30)
    draw.rectangle([cx - tw//2, 6,            cx + tw//2, 20],           fill=tick)
    draw.rectangle([cx - tw//2, panel_h - 20, cx + tw//2, panel_h - 6], fill=tick)

    return img


# ─── Full-frame renderer for /essay ──────────────────────────────────────────

def render_word_frame_full(word: str, font) -> Image.Image:
    return render_word_panel(word, font, WIDTH, HEIGHT)


# ─── Image utilities ─────────────────────────────────────────────────────────

def fit_image_to_panel(img: Image.Image, panel_w: int, panel_h: int) -> Image.Image:
    """Centre-crop fill without distortion."""
    src_w, src_h = img.size
    scale = max(panel_w / src_w, panel_h / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img  = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - panel_w) // 2
    top  = (new_h - panel_h) // 2
    return img.crop((left, top, left + panel_w, top + panel_h))


async def fetch_image_urls_from_supabase(limit: int) -> list[str]:
    """Fetch recent news rows that have a non-null image URL."""
    url = (
        f"{settings.SUPABASE_URL}/rest/v1/news"
        f"?select=image"
        f"&image=not.is.null"
        f"&image=neq."
        f"&order=created_at.desc"
        f"&limit={limit}"
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


async def download_image(url: str, client: httpx.AsyncClient) -> Image.Image | None:
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code == 200:
            return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        print(f"Image download failed ({url}): {e}")
    return None


# ─── Video builders ───────────────────────────────────────────────────────────

def _pil_to_bgr(img: Image.Image):
    import cv2, numpy as np
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _cv2_writer(path: str):
    import cv2
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    return cv2.VideoWriter(path, fourcc, FPS, (WIDTH, HEIGHT))


def create_video(words: list[str], wpm: int, output_path: str):
    font            = find_font(FONT_SIZE)
    frames_per_word = max(1, round(FPS * 60.0 / wpm))
    writer          = _cv2_writer(output_path)
    for word in words:
        bgr = _pil_to_bgr(render_word_frame_full(word, font))
        for _ in range(frames_per_word):
            writer.write(bgr)
    writer.release()


def create_photo_essay_video(
    words: list[str],
    wpm: int,
    images: list[Image.Image],
    output_path: str,
):
    font             = find_font(FONT_SIZE)
    frames_per_word  = max(1, round(FPS * 60.0 / wpm))
    frames_per_image = round(FPS * IMG_DURATION)

    # Pre-resize all images once to save per-frame work
    panels = [fit_image_to_panel(im, WIDTH, IMG_PANEL_H) for im in images]

    writer    = _cv2_writer(output_path)
    frame_idx = 0

    for word in words:
        word_panel = render_word_panel(word, font, WIDTH, WORD_PANEL_H)
        img_idx    = int(frame_idx / frames_per_image) % len(panels)

        composite = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
        composite.paste(panels[img_idx], (0, 0))
        composite.paste(word_panel,      (0, IMG_PANEL_H))

        bgr = _pil_to_bgr(composite)
        for _ in range(frames_per_word):
            writer.write(bgr)

        frame_idx += frames_per_word

    writer.release()


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

async def process_essay(words_text: str, rate: int):
    words = words_text.split()
    if not words:
        return
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, create_video, words, rate, tmp_path)
        status, body = await send_video_to_telegram(tmp_path)
        print(f"[/essay] Telegram {status}: {body}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def process_photo_essay(words_text: str, rate: int):
    words = words_text.split()
    if not words:
        return

    total_seconds = len(words) / rate * 60
    images_needed = max(1, math.ceil(total_seconds / IMG_DURATION))
    fetch_limit   = min(images_needed + 5, 50)

    print(f"[/photo-essay] {len(words)} words @ {rate} wpm → {total_seconds:.1f}s → need {images_needed} images")

    image_urls = await fetch_image_urls_from_supabase(fetch_limit)
    if not image_urls:
        print("[/photo-essay] No images found — falling back to plain essay")
        await process_essay(words_text, rate)
        return

    # Download concurrently, cap at 4 simultaneous to keep RAM in check
    sem = asyncio.Semaphore(4)
    async def guarded(url, client):
        async with sem:
            return await download_image(url, client)

    async with httpx.AsyncClient(timeout=20) as session:
        results = await asyncio.gather(*[guarded(u, session) for u in image_urls])

    images = [im for im in results if im is not None]
    if not images:
        print("[/photo-essay] All downloads failed — falling back to plain essay")
        await process_essay(words_text, rate)
        return

    # Cycle if fewer images than needed
    while len(images) < images_needed:
        images = (images * 2)[:images_needed]

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, create_photo_essay_video, words, rate, images, tmp_path
        )
        status, body = await send_video_to_telegram(tmp_path)
        print(f"[/photo-essay] Telegram {status}: {body}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/essay")
async def essay_endpoint(
    background_tasks: BackgroundTasks,
    words: str = Query(..., description="Essay text"),
    rate: int  = Query(300, description="Words per minute"),
):
    if not 50 <= rate <= 1000:
        return JSONResponse(status_code=400, content={"error": "rate must be 50–1000"})
    word_list = words.split()
    if not word_list:
        return JSONResponse(status_code=400, content={"error": "No words provided"})
    background_tasks.add_task(process_essay, words, rate)
    return {
        "status":            "processing",
        "endpoint":          "/essay",
        "word_count":        len(word_list),
        "rate_wpm":          rate,
        "estimated_seconds": round(len(word_list) / rate * 60, 1),
        "message":           "Video being generated — check Telegram shortly.",
    }


@app.get("/photo-essay")
async def photo_essay_endpoint(
    background_tasks: BackgroundTasks,
    words: str = Query(..., description="Essay text"),
    rate: int  = Query(300, description="Words per minute"),
):
    if not 50 <= rate <= 1000:
        return JSONResponse(status_code=400, content={"error": "rate must be 50–1000"})
    word_list = words.split()
    if not word_list:
        return JSONResponse(status_code=400, content={"error": "No words provided"})

    total_seconds = len(word_list) / rate * 60
    images_needed = max(1, math.ceil(total_seconds / IMG_DURATION))

    background_tasks.add_task(process_photo_essay, words, rate)
    return {
        "status":            "processing",
        "endpoint":          "/photo-essay",
        "word_count":        len(word_list),
        "rate_wpm":          rate,
        "estimated_seconds": round(total_seconds, 1),
        "images_needed":     images_needed,
        "message":           "Photo-essay video being generated — check Telegram shortly.",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}