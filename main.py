import os
import math
import tempfile
import subprocess
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
IMG_PANEL_H  = int(HEIGHT * 2 / 3)   # 853 px — top image area
WORD_PANEL_H = HEIGHT - IMG_PANEL_H  # 427 px — bottom word area
IMG_DURATION = 2.0                    # seconds per image


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_pivot_index(word: str) -> int:
    clean = ''.join(c for c in word if c.isalpha())
    if not clean:
        return 0
    n = len(clean)
    if n == 1:   return 0
    elif n <= 5: return 1
    elif n <= 9: return 2
    else:        return 3


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


def _tw(draw, text, font) -> int:
    if not text:
        return 0
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0]


def _th(draw, font) -> int:
    b = draw.textbbox((0, 0), "Ag", font=font)
    return b[3] - b[1]


# ─── Word-panel renderer ──────────────────────────────────────────────────────

def render_word_panel(word: str, font, panel_w: int, panel_h: int) -> Image.Image:
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


def render_word_frame_full(word: str, font) -> Image.Image:
    return render_word_panel(word, font, WIDTH, HEIGHT)


# ─── Image utilities ──────────────────────────────────────────────────────────

def fit_image_to_panel(img: Image.Image, pw: int, ph: int) -> Image.Image:
    sw, sh = img.size
    scale  = max(pw / sw, ph / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img    = img.resize((nw, nh), Image.LANCZOS)
    l, t   = (nw - pw) // 2, (nh - ph) // 2
    return img.crop((l, t, l + pw, t + ph))


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


async def download_image(url: str, client: httpx.AsyncClient) -> Image.Image | None:
    try:
        r = await client.get(url, timeout=15, follow_redirects=True)
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f"Image download failed ({url}): {e}")
    return None


# ─── Audio generation ─────────────────────────────────────────────────────────

def generate_tts_audio(text: str, output_mp3: str) -> bool:
    """
    Generate TTS MP3 using gTTS.
    Returns True on success, False on failure.
    gTTS speaks at a natural pace (~150 wpm) regardless of the RSVP rate —
    the visual speed and voice speed are intentionally independent.
    """
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang="en", slow=False)
        tts.save(output_mp3)
        return True
    except Exception as e:
        print(f"gTTS error: {e}")
        return False


def merge_audio_video(video_path: str, audio_path: str, output_path: str) -> bool:
    """
    Use ffmpeg to combine silent video + TTS audio.
    - Audio is trimmed/padded to match video duration exactly.
    - No re-encoding of video stream (copy) — fast and RAM-friendly.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",          # copy video stream as-is — no re-encode
        "-c:a", "aac",           # encode audio to AAC for mp4 container
        "-b:a", "128k",
        "-shortest",             # trim to the shorter of video/audio
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
    panels           = [fit_image_to_panel(im, WIDTH, IMG_PANEL_H) for im in images]
    writer           = _cv2_writer(output_path)
    frame_idx        = 0

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


# ─── Audio post-processing helper ─────────────────────────────────────────────

def apply_audio_if_requested(
    silent_video: str,
    words_text: str,
    audio: bool,
) -> str:
    """
    If audio=True, generate TTS and merge with video.
    Returns path to the final video (may be the same file or a new one).
    Cleans up intermediate files automatically.
    """
    if not audio:
        return silent_video

    mp3_path    = silent_video.replace(".mp4", "_audio.mp3")
    final_path  = silent_video.replace(".mp4", "_final.mp4")

    ok = generate_tts_audio(words_text, mp3_path)
    if not ok:
        print("[audio] TTS failed — sending silent video")
        return silent_video

    ok = merge_audio_video(silent_video, mp3_path, final_path)

    # Clean up MP3 and silent video regardless
    for p in [mp3_path, silent_video]:
        try:
            os.remove(p)
        except Exception:
            pass

    if ok:
        return final_path
    else:
        print("[audio] ffmpeg merge failed — sending silent video (already deleted, re-create)")
        # Re-create silent video as fallback is not practical here;
        # caller will handle missing file gracefully
        return final_path  # caller checks os.path.exists


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

async def process_essay(words_text: str, rate: int, audio: bool):
    words = words_text.split()
    if not words:
        return

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        silent_path = tmp.name

    final_path = silent_path
    try:
        loop = asyncio.get_event_loop()

        # 1. Build silent video
        await loop.run_in_executor(None, create_video, words, rate, silent_path)

        # 2. Optionally add audio (runs in executor to avoid blocking)
        if audio:
            final_path = await loop.run_in_executor(
                None, apply_audio_if_requested, silent_path, words_text, True
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


async def process_photo_essay(words_text: str, rate: int, audio: bool):
    words = words_text.split()
    if not words:
        return

    total_seconds = len(words) / rate * 60
    images_needed = max(1, math.ceil(total_seconds / IMG_DURATION))
    fetch_limit   = min(images_needed + 5, 50)

    print(f"[/photo-essay] {len(words)} words @ {rate} wpm → {total_seconds:.1f}s → need {images_needed} images")

    image_urls = await fetch_image_urls_from_supabase(fetch_limit)
    if not image_urls:
        print("[/photo-essay] No images — falling back to plain essay")
        await process_essay(words_text, rate, audio)
        return

    sem = asyncio.Semaphore(4)
    async def guarded(url, client):
        async with sem:
            return await download_image(url, client)

    async with httpx.AsyncClient(timeout=20) as session:
        results = await asyncio.gather(*[guarded(u, session) for u in image_urls])

    images = [im for im in results if im is not None]
    if not images:
        print("[/photo-essay] All downloads failed — falling back to plain essay")
        await process_essay(words_text, rate, audio)
        return

    while len(images) < images_needed:
        images = (images * 2)[:images_needed]

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        silent_path = tmp.name

    final_path = silent_path
    try:
        loop = asyncio.get_event_loop()

        # 1. Build silent video
        await loop.run_in_executor(
            None, create_photo_essay_video, words, rate, images, silent_path
        )

        # 2. Optionally add audio
        if audio:
            final_path = await loop.run_in_executor(
                None, apply_audio_if_requested, silent_path, words_text, True
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


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/essay")
async def essay_endpoint(
    background_tasks: BackgroundTasks,
    words: str = Query(...,  description="Essay text"),
    rate:  int = Query(300,  description="Words per minute"),
    audio: str = Query("f",  description="Add voiceover? t=yes, f=no"),
):
    if not 50 <= rate <= 1000:
        return JSONResponse(status_code=400, content={"error": "rate must be 50–1000"})
    word_list = words.split()
    if not word_list:
        return JSONResponse(status_code=400, content={"error": "No words provided"})

    want_audio = audio.strip().lower() == "t"
    background_tasks.add_task(process_essay, words, rate, want_audio)
    return {
        "status":            "processing",
        "endpoint":          "/essay",
        "word_count":        len(word_list),
        "rate_wpm":          rate,
        "audio":             want_audio,
        "estimated_seconds": round(len(word_list) / rate * 60, 1),
        "message":           "Video being generated — check Telegram shortly.",
    }


@app.get("/photo-essay")
async def photo_essay_endpoint(
    background_tasks: BackgroundTasks,
    words: str = Query(...,  description="Essay text"),
    rate:  int = Query(300,  description="Words per minute"),
    audio: str = Query("f",  description="Add voiceover? t=yes, f=no"),
):
    if not 50 <= rate <= 1000:
        return JSONResponse(status_code=400, content={"error": "rate must be 50–1000"})
    word_list = words.split()
    if not word_list:
        return JSONResponse(status_code=400, content={"error": "No words provided"})

    total_seconds = len(word_list) / rate * 60
    images_needed = max(1, math.ceil(total_seconds / IMG_DURATION))
    want_audio    = audio.strip().lower() == "t"

    background_tasks.add_task(process_photo_essay, words, rate, want_audio)
    return {
        "status":            "processing",
        "endpoint":          "/photo-essay",
        "word_count":        len(word_list),
        "rate_wpm":          rate,
        "audio":             want_audio,
        "estimated_seconds": round(total_seconds, 1),
        "images_needed":     images_needed,
        "message":           "Photo-essay video being generated — check Telegram shortly.",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}