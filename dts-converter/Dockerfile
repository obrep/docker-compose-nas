FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY convert.py /app/convert.py
ENTRYPOINT ["python3", "-u", "/app/convert.py"]
