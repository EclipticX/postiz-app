FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core librsvg2-bin tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 studio \
    && install -d -o studio -g studio -m 0700 /studio/data

WORKDIR /studio
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY niches ./niches
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STUDIO_HOST=0.0.0.0
ENV STUDIO_WHISPER_MODEL_PATH=/studio/data/whisper-model
USER studio
EXPOSE 8090
CMD ["python", "-m", "app.server"]
