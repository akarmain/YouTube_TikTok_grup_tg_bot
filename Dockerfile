FROM python:3.12-slim

# ffmpeg для пост-обработки mp4 + deno для yt-dlp EJS (n-challenge)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        unzip && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local/deno sh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV DENO_INSTALL=/usr/local/deno
ENV PATH="${DENO_INSTALL}/bin:${PATH}"
# Значения по умолчанию для n-challenge solver; можно переопределить через .env
ENV YOUTUBE_JS_RUNTIMES=deno,node,quickjs,bun
ENV YOUTUBE_REMOTE_COMPONENTS=ejs:github

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN uv pip install --system -r requirements.txt
COPY . /app
CMD ["python3", "./run.py"]
