FROM alpine:3.19 as ffmpeg
RUN apk add --no-cache ffmpeg

FROM python:3.11-slim
WORKDIR /app
COPY --from=ffmpeg /usr/bin/ffmpeg /usr/bin/ffmpeg
COPY --from=ffmpeg /usr/bin/ffprobe /usr/bin/ffprobe
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "app/main.py"]

