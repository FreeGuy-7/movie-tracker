FROM python:3.12-slim

WORKDIR /app
COPY app.py settings.py web.py ./
CMD ["python3", "web.py"]
