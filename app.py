import hashlib
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Ma'lumotlar (blog.db, uploads/, .secret_key) saqlanadigan papka. Hostda doimiy diskni ko'rsating.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)


def _writable_dir(path):
    os.makedirs(path, exist_ok=True)
    if not os.access(path, os.W_OK):
        raise PermissionError(path)
    return path


try:
    _writable_dir(os.path.join(DATA_DIR, "uploads"))
    _writable_dir(os.path.join(DATA_DIR, "files"))
except OSError:
    # Masalan Render'da /var/lib/blog ga yozib bo'lmaydi. Yiqilish o'rniga ilova papkasiga o'tamiz.
    _fallback = os.path.join(BASE_DIR, "data")
    print(f"OGOHLANTIRISH: DATA_DIR={DATA_DIR} ga yozib bo'lmaydi, {_fallback} ishlatiladi. "
          "Doimiy disk ulanmagan bo'lsa, ma'lumotlar har deployda o'chib ketadi.",
          file=sys.stderr, flush=True)
    DATA_DIR = _fallback
    _writable_dir(os.path.join(DATA_DIR, "uploads"))
    _writable_dir(os.path.join(DATA_DIR, "files"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")   # post muqova rasmlari
FILES_DIR = os.path.join(DATA_DIR, "files")      # fayl menejeri (tasodifiy nom bilan)
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
COVER_MAX_BYTES = 6 * 1024 * 1024
DB_PATH = os.path.join(DATA_DIR, "blog.db")
# DEV=1 faqat kompyuteringizda sinash uchun (standart parol va http cookie)
DEV = os.environ.get("DEV", "0") == "1"

SITE_NAME = os.environ.get("SITE_NAME", "Notes")
SITE_TAGLINE = os.environ.get("SITE_TAGLINE", "Fikrlar, kitoblar va kundalik kuzatuvlar.")
PER_PAGE = 9


def _secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(DATA_DIR, ".secret_key")
    try:
        with open(path, "x") as f:  # faqat birinchi marta yaratiladi
            key = secrets.token_hex(32)
            f.write(key)
        os.chmod(path, 0o600)
        return key
    except FileExistsError:
        with open(path) as f:
            return f.read().strip()


def _admin_hash():
    if os.environ.get("ADMIN_PASS_HASH"):
        return os.environ["ADMIN_PASS_HASH"]
    if os.environ.get("ADMIN_PASS"):
        return generate_password_hash(os.environ["ADMIN_PASS"])
    if DEV:
        return generate_password_hash("admin123")
    raise RuntimeError(
        "ADMIN_PASS (yoki ADMIN_PASS_HASH) o'rnatilmagan. "
        ".env.example faylini ko'ring. Faqat sinash uchun DEV=1 qo'ying."
    )


app = Flask(__name__)
app.secret_key = _secret_key()
# Nginx / hosting proksisi ortida to'g'ri IP, sxema (https) va host olish uchun
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # HTTPS bo'lmasa COOKIE_SECURE=0 qo'ying (aks holda login ishlamaydi)
    SESSION_COOKIE_SECURE=(os.environ.get("COOKIE_SECURE", "1") == "1" and not DEV),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024 + 65536,  # bitta so'rovdagi jami hajm
)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS_HASH = _admin_hash()


# ---------- Ma'lumotlar bazasi ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.execute("PRAGMA journal_mode=WAL")  # bir nechta worker uchun barqarorroq
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Umumiy',
                cover TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                vid TEXT NOT NULL,
                post_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_visits_day ON visits(day);
            CREATE INDEX IF NOT EXISTS idx_visits_post ON visits(post_id);
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER,
                name TEXT NOT NULL,
                public INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER,
                name TEXT NOT NULL,
                stored TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                public INTEGER NOT NULL DEFAULT 1,
                downloads INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
            CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);
            """
        )
        # Eski versiyadan ko'chirish (ustunlar yo'q bo'lsa qo'shiladi)
        cols = {r[1] for r in db.execute("PRAGMA table_info(posts)")}
        for col, ddl in (("summary", "TEXT NOT NULL DEFAULT ''"),
                         ("category", "TEXT NOT NULL DEFAULT 'Umumiy'"),
                         ("cover", "TEXT")):
            if col not in cols:
                db.execute(f"ALTER TABLE posts ADD COLUMN {col} {ddl}")
        # Eski tashrif yozuvlarini tozalash (13 oydan eski)
        db.execute("DELETE FROM visits WHERE day < date('now','-400 day')")


# ---------- Xavfsizlik ----------
def csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf"] = csrf


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        saved = session.get("csrf", "")
        if not saved or not secrets.compare_digest(sent, saved):
            abort(400)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/admin"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin"))
        return view(*a, **kw)
    return wrapped


# ---------- Anonim tashrif hisobi ----------
BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|curl|wget|python-requests|headless|monitor|uptime|preview|facebookexternalhit",
    re.I,
)


@app.after_request
def track_visit(resp):
    """IP va brauzer ma'lumoti faqat bir kunlik bir tomonlama hash sifatida saqlanadi."""
    try:
        if (request.method == "GET" and resp.status_code == 200
                and request.endpoint in ("index", "post", "files_public") and not session.get("admin")):
            ua = request.headers.get("User-Agent", "")
            if ua and not BOT_RE.search(ua):
                today = date.today().isoformat()
                raw = f"{app.secret_key}|{today}|{request.remote_addr}|{ua}"
                vid = hashlib.sha256(raw.encode()).hexdigest()[:16]
                pid = (request.view_args or {}).get("post_id") if request.endpoint == "post" else None
                db = get_db()
                db.execute("INSERT INTO visits (day, vid, post_id) VALUES (?,?,?)", (today, vid, pid))
                db.commit()
    except Exception:  # statistika sahifani hech qachon buzmasin
        app.logger.exception("tashrifni yozib bo'lmadi")
    return resp


def visit_stats():
    db, today = get_db(), date.today()

    def span(a, b):
        v = db.execute("SELECT COUNT(*) FROM visits WHERE day BETWEEN ? AND ?",
                       (a.isoformat(), b.isoformat())).fetchone()[0]
        u = db.execute("SELECT COUNT(*) FROM (SELECT DISTINCT day, vid FROM visits "
                       "WHERE day BETWEEN ? AND ?)", (a.isoformat(), b.isoformat())).fetchone()[0]
        return v, u

    out = []
    for title, sub, n, prev in (("Kunlik", "bugun", 1, "kecha"),
                                ("Haftalik", "so'nggi 7 kun", 7, "oldingi 7 kun"),
                                ("Oylik", "so'nggi 30 kun", 30, "oldingi 30 kun")):
        a = today - timedelta(days=n - 1)
        v, u = span(a, today)
        pv, _ = span(a - timedelta(days=n), a - timedelta(days=1))
        delta = None if pv == 0 else round((v - pv) / pv * 100)
        out.append({"title": title, "sub": sub, "views": v, "uniq": u,
                    "delta": delta, "prev": prev})
    return out


def visit_series(n=14):
    db, today = get_db(), date.today()
    start = today - timedelta(days=n - 1)
    rows = db.execute(
        "SELECT day, COUNT(*) AS v, COUNT(DISTINCT vid) AS u FROM visits "
        "WHERE day BETWEEN ? AND ? GROUP BY day", (start.isoformat(), today.isoformat())
    ).fetchall()
    m = {r["day"]: (r["v"], r["u"]) for r in rows}
    out = []
    for i in range(n):
        d = start + timedelta(days=i)
        v, u = m.get(d.isoformat(), (0, 0))
        out.append({"label": d.strftime("%d.%m"), "views": v, "uniq": u})
    mx = max([x["views"] for x in out] + [1])
    for x in out:
        x["vh"] = round(x["views"] / mx * 100)
        x["uh"] = round(x["uniq"] / mx * 100)
    return out


# ---------- Matn yordamchilari ----------
MONTHS = ["yan", "fev", "mar", "apr", "may", "iyn", "iyl", "avg", "sen", "okt", "noy", "dek"]


@app.template_filter("kun")
def kun(value):
    try:
        d = datetime.fromisoformat(value)
        return f"{d.day} {MONTHS[d.month - 1]}, {d.year}"
    except (TypeError, ValueError):
        return value


@app.template_filter("num")
def num(value):
    try:
        return f"{int(round(value)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


@app.template_filter("fsize")
def fsize(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


@app.template_filter("ext")
def ext(name):
    return (os.path.splitext(name or "")[1].lstrip(".")[:4].upper()) or "FAYL"


@app.template_filter("is_image")
def is_image(name):
    return (mimetypes.guess_type(name or "")[0] or "") in IMAGE_TYPES


def slugify(s):
    s = s.lower()
    for ch in ("ʻ", "ʼ", "'", "’", "‘", "`"):
        s = s.replace(ch, "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "post"


def plain(text, n):
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text or "")
    text = re.sub(r"^\s*(#{1,3}\s+|>\s+|-\s+)", "", text, flags=re.M)
    text = re.sub(r"[*`]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


_INLINE = (
    # rasm: faqat fayl menejeridagi fayllar (/f/ID)
    (re.compile(r"!\[([^\]]*)\]\((/f/\d+)(?:/[^\s)]*)?\)"),
     r'<img src="\2" alt="\1" loading="lazy">'),
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"<em>\1</em>"),
    # havola: https://... yoki fayl menejeridagi fayl (/f/ID)
    (re.compile(r"\[([^\]]+)\]\(((?:https?://|/f/)[^\s)]+)\)"),
     r'<a href="\2" rel="noopener nofollow" target="_blank">\1</a>'),
)


def _inline(s):
    for rx, rep in _INLINE:
        s = rx.sub(rep, s)
    return s


@app.template_filter("rich")
def rich(text):
    """Xavfsiz mini-markdown: ## sarlavha, > iqtibos, - ro'yxat, **qalin**, *kursiv*, [matn](https://...)."""
    out = []
    for block in re.split(r"\n{2,}", (text or "").replace("\r\n", "\n").strip()):
        b = str(escape(block.strip()))  # avval hammasi escape qilinadi
        if not b:
            continue
        lines = b.split("\n")
        if b.startswith("### "):
            out.append(f"<h3>{_inline(b[4:])}</h3>")
        elif b.startswith("## "):
            out.append(f"<h2>{_inline(b[3:])}</h2>")
        elif all(l.startswith("&gt; ") or l == "&gt;" for l in lines):
            inner = "<br>".join(_inline(l[5:]) for l in lines)
            out.append(f"<blockquote><p>{inner}</p></blockquote>")
        elif all(l.startswith("- ") for l in lines):
            out.append("<ul>" + "".join(f"<li>{_inline(l[2:])}</li>" for l in lines) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(_inline(l) for l in lines) + "</p>")
    return Markup("".join(out))


LIST_COLS = ("id, title, summary, category, cover, created_at, "
             "substr(body,1,400) AS body_head, length(body) AS blen")


def decorate(row):
    p = dict(row)
    if p.get("body") is not None:
        p["blen"], p["body_head"] = len(p["body"]), p["body"][:400]
    p["slug"] = slugify(p["title"])
    p["url"] = url_for("post", post_id=p["id"], slug=p["slug"])
    p["excerpt"] = (p.get("summary") or "").strip() or plain(p.get("body_head"), 170)
    p["minutes"] = max(1, round((p.get("blen") or 0) / 1100))
    return p


@app.context_processor
def inject_site():
    cats, has_files = [], False
    try:
        db = get_db()
        cats = [r[0] for r in db.execute(
            "SELECT category FROM posts GROUP BY category "
            "ORDER BY COUNT(*) DESC, category LIMIT 5")]
        has_files = bool(db.execute(
            "SELECT 1 FROM files WHERE public = 1 AND (folder_id IS NULL OR folder_id IN "
            "(SELECT id FROM folders WHERE public = 1)) LIMIT 1").fetchone())
    except sqlite3.Error:
        pass
    return {"site": {"name": SITE_NAME, "tagline": SITE_TAGLINE},
            "nav_categories": cats, "has_files": has_files}


# ---------- Rasm yuklash ----------
def _sniff(head):
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_cover(file):
    if not file or not file.filename:
        return None
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size > COVER_MAX_BYTES:
        raise ValueError("Muqova rasmi 6 MB dan oshmasligi kerak.")
    ext = _sniff(file.stream.read(16))  # kengaytma emas, fayl mazmuni tekshiriladi
    file.stream.seek(0)
    if not ext:
        raise ValueError("Rasm formati JPG, PNG, WEBP yoki GIF bo'lishi kerak.")
    name = f"{secrets.token_hex(12)}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, name))
    return name


def delete_cover(name):
    if name and re.fullmatch(r"[0-9a-f]{24}\.(jpg|png|gif|webp)", name):
        try:
            os.remove(os.path.join(UPLOAD_DIR, name))
        except OSError:
            pass


@app.get("/media/<name>")
def media(name):
    if not re.fullmatch(r"[0-9a-f]{24}\.(jpg|png|gif|webp)", name):
        abort(404)
    return send_from_directory(UPLOAD_DIR, name, max_age=31536000)


# ---------- Ommaviy sahifalar ----------
@app.route("/")
def index():
    q = request.args.get("q", "").strip()[:80]
    cat = request.args.get("c", "").strip()[:40]
    page = max(1, request.args.get("page", 1, type=int))
    where, args = [], []
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    if cat:
        where.append("category = ?")
        args.append(cat)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    rows = get_db().execute(
        f"SELECT {LIST_COLS} FROM posts {w} ORDER BY created_at DESC, id DESC", args
    ).fetchall()

    filtered = bool(q or cat)
    featured = decorate(rows[0]) if (rows and not filtered and page == 1) else None
    start = (0 if filtered else 1) + (page - 1) * PER_PAGE
    grid = [decorate(r) for r in rows[start:start + PER_PAGE]]
    return render_template(
        "index.html", featured=featured, grid=grid, q=q, cat=cat, page=page,
        total=len(rows), has_next=start + PER_PAGE < len(rows),
    )


@app.route("/post/<int:post_id>")
@app.route("/post/<int:post_id>/<slug>")
def post(post_id, slug=None):
    db = get_db()
    row = db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        abort(404)
    p = decorate(row)
    if slug != p["slug"]:
        return redirect(p["url"], 301)
    related = [decorate(r) for r in db.execute(
        f"SELECT {LIST_COLS} FROM posts WHERE id != ? "
        "ORDER BY (category = ?) DESC, created_at DESC LIMIT 3", (post_id, p["category"]))]
    return render_template("post.html", p=p, related=related)


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin\n", mimetype="text/plain")


@app.get("/healthz")
def healthz():
    return "ok"


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(413)
def too_large(_):
    flash(f"Yuklanayotgan fayllar jami {MAX_UPLOAD_MB} MB dan oshmasligi kerak.", "err")
    ref = request.referrer or ""
    return redirect(ref if ref.startswith(request.host_url) else url_for("index"))


# ---------- Admin ----------
def categories():
    return [r[0] for r in get_db().execute(
        "SELECT category FROM posts GROUP BY category ORDER BY COUNT(*) DESC, category")]


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        user = request.form.get("user", "")
        pw = request.form.get("password", "")
        if user == ADMIN_USER and check_password_hash(ADMIN_PASS_HASH, pw):
            session.clear()
            session["admin"] = True
            session.permanent = True
            return redirect(url_for("admin"))
        time.sleep(1)  # sekinlashtirish (brute-force'ga qarshi)
        flash("Login yoki parol noto'g'ri.", "err")
        return redirect(url_for("admin"))

    if not session.get("admin"):
        return render_template("login.html")

    db, today = get_db(), date.today()
    since = (today - timedelta(days=29)).isoformat()
    posts = db.execute(
        "SELECT p.id, p.title, p.category, p.created_at, "
        "(SELECT COUNT(*) FROM visits v WHERE v.post_id = p.id) AS views "
        "FROM posts p ORDER BY p.created_at DESC, p.id DESC").fetchall()
    top = db.execute(
        "SELECT p.id, p.title, COUNT(*) AS v FROM visits s JOIN posts p ON p.id = s.post_id "
        "WHERE s.day >= ? GROUP BY p.id ORDER BY v DESC LIMIT 5", (since,)).fetchall()
    total_views = db.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    return render_template(
        "admin.html", stats=visit_stats(), series=visit_series(),
        posts=posts, top=top, total_views=total_views,
    )


def _form_data():
    return {
        "title": request.form.get("title", "").strip()[:200],
        "category": (request.form.get("category", "").strip() or "Umumiy")[:40],
        "summary": request.form.get("summary", "").strip()[:300],
        "body": request.form.get("body", "").strip(),
    }


@app.route("/admin/post/new", methods=["GET", "POST"])
@login_required
def new_post():
    if request.method == "POST":
        data = _form_data()
        if not data["title"] or not data["body"]:
            flash("Sarlavha va matn kerak.", "err")
            return render_template("editor.html", p=data, cats=categories(), is_new=True)
        try:
            cover = save_cover(request.files.get("cover"))
        except ValueError as e:
            flash(str(e), "err")
            return render_template("editor.html", p=data, cats=categories(), is_new=True)
        db = get_db()
        db.execute(
            "INSERT INTO posts (title, summary, body, category, cover, created_at) VALUES (?,?,?,?,?,?)",
            (data["title"], data["summary"], data["body"], data["category"], cover,
             datetime.now().isoformat(timespec="minutes")))
        db.commit()
        flash("Post chop etildi.", "ok")
        return redirect(url_for("admin"))
    return render_template("editor.html", p={}, cats=categories(), is_new=True)


@app.route("/admin/post/<int:post_id>/edit", methods=["GET", "POST"])
@login_required
def edit_post(post_id):
    db = get_db()
    row = db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        abort(404)
    if request.method == "POST":
        data = _form_data()
        data["id"], data["cover"] = post_id, row["cover"]
        if not data["title"] or not data["body"]:
            flash("Sarlavha va matn kerak.", "err")
            return render_template("editor.html", p=data, cats=categories(), is_new=False)
        cover = row["cover"]
        try:
            new_cover = save_cover(request.files.get("cover"))
        except ValueError as e:
            flash(str(e), "err")
            return render_template("editor.html", p=data, cats=categories(), is_new=False)
        if new_cover:
            delete_cover(cover)
            cover = new_cover
        elif request.form.get("remove_cover"):
            delete_cover(cover)
            cover = None
        db.execute(
            "UPDATE posts SET title=?, summary=?, body=?, category=?, cover=? WHERE id=?",
            (data["title"], data["summary"], data["body"], data["category"], cover, post_id))
        db.commit()
        flash("Post yangilandi.", "ok")
        return redirect(url_for("admin"))
    return render_template("editor.html", p=dict(row), cats=categories(), is_new=False)


@app.post("/admin/post/<int:post_id>/delete")
@login_required
def delete_post(post_id):
    db = get_db()
    row = db.execute("SELECT cover FROM posts WHERE id=?", (post_id,)).fetchone()
    if row:
        delete_cover(row["cover"])
        db.execute("DELETE FROM posts WHERE id=?", (post_id,))
        db.execute("DELETE FROM visits WHERE post_id=?", (post_id,))
        db.commit()
        flash("Post o'chirildi.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    # Faqat sinash uchun. Real hostda: gunicorn wsgi:app
    app.run(debug=False, host="127.0.0.1", port=5000)
