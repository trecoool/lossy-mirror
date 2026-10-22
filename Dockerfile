FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg inotify-tools && pip install --no-cache-dir mutagen==1.47.0
COPY app/ /app/
WORKDIR /app
ENV PYTHONUNBUFFERED=1
VOLUME ["/config"]
ENTRYPOINT ["python3", "/app/watch.py"]
