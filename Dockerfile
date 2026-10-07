FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# Seeds mock data on first start (skipped if the DB already has snapshots),
# then serves the app.
CMD ["sh", "-c", "python demo/seed_demo_data.py && exec gunicorn -w 2 -b 0.0.0.0:5000 app:app"]
