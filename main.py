import os
import math
import tempfile
import httpx
import asyncio
from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw, ImageFont

from config import settings

app = FastAPI()

# Video settings — kept minimal for low RAM usage
WIDTH = 720
HEIGHT = 1280
FPS = 30
BG_COLOR = (0, 0, 0)
TEXT_COLOR = (255, 255, 255)
RED_COLOR = (220, 50, 50)
FONT_SIZE = 72

# Optimal pivot (ORP) letter index — ~30% into the word
def get_pivot_index(word: str) -> int:
    clean = ''.join(c for c in word if c.isalpha())
    if not clean:
        return 0
    length = len(clean)
    if length == 1:
        return 0
    elif length <= 5:
        return 1
    elif length <= 9:
        return 2
    else:
        return 3

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

def render_word_frame(word: str, font: ImageFont.FreeTypeFont) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    pivot_idx = get_pivot_index(word)

    # Split word into 3 parts: before, pivot letter, after
    # Find actual character position (including non-alpha chars)
    alpha_count = 0
    pivot_char_idx = 0
    for i, ch in enumerate(word):
        if ch.isalpha():
            if alpha_count == pivot_idx:
                pivot_char_idx = i
                break
            alpha_count += 1

    before = word[:pivot_char_idx]
    pivot_char = word[pivot_char_idx] if pivot_char_idx < len(word) else ""
    after = word[pivot_char_idx + 1:] if pivot_char_idx + 1 < len(word) else ""

    # Measure each part
    dummy = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(dummy)

    def text_width(text):
        if not text:
            return 0
        bbox = d.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    def text_height(text):
        if not text:
            bbox = d.textbbox((0, 0), "Ag", font=font)
        else:
            bbox = d.textbbox((0, 0), text, font=font)
        return bbox[3] - bbox[1]

    w_before = text_width(before)
    w_pivot = text_width(pivot_char) if pivot_char else 0
    w_after = text_width(after)
    total_w = w_before + w_pivot + w_after
    h = text_height("Ag")

    # Center the whole word, aligning pivot letter at the visual center
    start_x = (WIDTH - total_w) // 2
    y = (HEIGHT - h) // 2

    # Draw parts
    x = start_x
    if before:
        draw.text((x, y), before, font=font, fill=TEXT_COLOR)
        x += w_before
    if pivot_char:
        draw.text((x, y), pivot_char, font=font, fill=RED_COLOR)
        x += w_pivot
    if after:
        draw.text((x, y), after, font=font, fill=TEXT_COLOR)

    # Red tick marks top and bottom center (like in reference images)
    cx = WIDTH // 2
    tick_color = (180, 30, 30)
    tick_h = 18
    tick_w = 3
    draw.rectangle([cx - tick_w//2, 8, cx + tick_w//2, 8 + tick_h], fill=tick_color)
    draw.rectangle([cx - tick_w//2, HEIGHT - 8 - tick_h, cx + tick_w//2, HEIGHT - 8], fill=tick_color)

    return img

def create_video(words: list[str], wpm: int, output_path: str):
    try:
        import cv2
        import numpy as np
        use_cv2 = True
    except ImportError:
        use_cv2 = False

    font = find_font(FONT_SIZE)
    seconds_per_word = 60.0 / wpm
    frames_per_word = max(1, int(FPS * seconds_per_word))

    if use_cv2:
        import cv2
        import numpy as np
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(output_path, fourcc, FPS, (WIDTH, HEIGHT))

        for word in words:
            frame_img = render_word_frame(word, font)
            frame_np = np.array(frame_img)
            frame_bgr = cv2.cvtColor(frame_np, cv2.COLOR_RGB2BGR)
            for _ in range(frames_per_word):
                writer.write(frame_bgr)

        writer.release()
    else:
        # Fallback: use imageio if opencv not available
        import imageio
        frames = []
        for word in words:
            frame_img = render_word_frame(word, font)
            arr = __import__("numpy").array(frame_img, dtype=__import__("numpy").uint8)
            for _ in range(frames_per_word):
                frames.append(arr)

        imageio.mimwrite(output_path, frames, fps=FPS, codec="libx264",
                         output_params=["-pix_fmt", "yuv420p"])

async def send_video_to_telegram(video_path: str):
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_TOKEN}/sendVideo"
    async with httpx.AsyncClient(timeout=120) as client:
        with open(video_path, "rb") as f:
            resp = await client.post(
                url,
                data={"chat_id": settings.TELEGRAM_CHAT_ID},
                files={"video": ("essay.mp4", f, "video/mp4")},
            )
    return resp.status_code, resp.text

async def process_essay(words_text: str, rate: int):
    words = words_text.split()
    if not words:
        return

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Run blocking video creation in thread pool
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, create_video, words, rate, tmp_path)
        status, body = await send_video_to_telegram(tmp_path)
        print(f"Telegram response: {status} — {body}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.get("/essay")
async def essay_endpoint(
    background_tasks: BackgroundTasks,
    words: str = Query(..., description="The essay text"),
    rate: int = Query(300, description="Words per minute"),
):
    if rate < 50 or rate > 1000:
        return JSONResponse(status_code=400, content={"error": "rate must be between 50 and 1000"})

    word_list = words.split()
    if not word_list:
        return JSONResponse(status_code=400, content={"error": "No words provided"})

    background_tasks.add_task(process_essay, words, rate)

    return {
        "status": "processing",
        "word_count": len(word_list),
        "rate_wpm": rate,
        "estimated_seconds": round(len(word_list) / rate * 60, 1),
        "message": "Video is being generated and will be sent to Telegram shortly.",
    }

@app.get("/health")
async def health():
    return {"status": "ok"}