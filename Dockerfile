# ---------- stage 1: build telegram-bot-api (Local Bot API Server → upload تا 2GB) ----------
FROM debian:trixie-slim AS tgapi
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates make cmake g++ gperf zlib1g-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --recursive --depth 1 --shallow-submodules https://github.com/tdlib/telegram-bot-api.git /src \
    && mkdir /src/build && cd /src/build \
    && cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH=/usr/local .. \
    && cmake --build . --target install -j"$(nproc)" \
    && strip /usr/local/bin/telegram-bot-api

# ---------- stage 2: bot ----------
FROM python:3.12-slim-trixie
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# Cloudflare WARP رایگان: wgcf + wireproxy (WireGuard در سطح کاربر → SOCKS5، بدون نیاز به NET_ADMIN)
# اگر دانلود ناموفق بود، بیلد نمی‌شکند؛ بات بدون WARP اجرا می‌شود.
RUN A=$(dpkg --print-architecture); \
    for V in 2.2.29 2.2.30; do \
      curl -fsSL -o /usr/local/bin/wgcf "https://github.com/ViRb3/wgcf/releases/download/v${V}/wgcf_${V}_linux_${A}" && break; \
    done; chmod +x /usr/local/bin/wgcf 2>/dev/null || true; \
    curl -fsSL "https://github.com/pufferffish/wireproxy/releases/download/v1.0.9/wireproxy_linux_${A}.tar.gz" \
      | tar -xz -C /usr/local/bin wireproxy || echo "wireproxy download failed"; \
    chmod +x /usr/local/bin/wireproxy 2>/dev/null || true
COPY --from=tgapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN chmod +x start.sh && mkdir -p /app/data
CMD ["./start.sh"]
