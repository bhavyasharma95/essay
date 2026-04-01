FROM python:3.11-slim

# Install system dependencies: ffmpeg for audio merging, fonts for Hindi/Devanagari
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-core \
    fonts-noto-extra \
    fonts-lohit-deva \
    libgl1 \
    libglib2.0-0 \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port $PORT"]