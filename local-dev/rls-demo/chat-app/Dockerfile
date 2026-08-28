FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY chat_app.py .

CMD ["uv", "run", "chat_app.py"]
