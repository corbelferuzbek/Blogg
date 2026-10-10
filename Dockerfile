FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8000
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd -m app && mkdir -p /data && chown -R app /data /app
USER app

VOLUME /data
EXPOSE 8000
CMD ["gunicorn", "wsgi:app", "-c", "gunicorn.conf.py"]
