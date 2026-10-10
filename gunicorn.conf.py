import os

# PaaS (Render, Railway, Heroku) PORT beradi; VPS'da nginx 127.0.0.1:8000 ga yo'naltiradi
bind = os.environ.get("BIND", f"0.0.0.0:{os.environ.get('PORT', '8000')}")

workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
worker_class = "gthread"
threads = int(os.environ.get("THREADS", "4"))
timeout = 30
graceful_timeout = 20
keepalive = 5

# Bazani va maxfiy kalitni bir marta tayyorlash uchun
preload_app = True

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
