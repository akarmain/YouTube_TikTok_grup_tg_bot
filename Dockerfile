FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl ca-certificates unzip && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local/deno sh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV DENO_INSTALL=/usr/local/deno
ENV PATH="${DENO_INSTALL}/bin:${PATH}"

WORKDIR /app
COPY . /app
RUN uv pip install --system --no-cache -r requirements.txt
CMD ["python3", "./run.py"]
