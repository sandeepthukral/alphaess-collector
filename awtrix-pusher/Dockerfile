FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pusher.py .

RUN useradd --create-home pusher
USER pusher

CMD ["python", "-u", "pusher.py"]
