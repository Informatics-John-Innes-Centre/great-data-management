FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INSTITUTE_DIR=/institute

WORKDIR /app

COPY requirements.txt .
# requests/beautifulsoup4 are only for the Diskover crawler (diskover_complex.py),
# so it can run from this image too.
RUN pip install --no-cache-dir -r requirements.txt requests beautifulsoup4

COPY . .

EXPOSE 5000

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "app:app"]
