import csv, io, os, secrets, threading
from functools import wraps

from flask import Flask, Response, redirect, render_template, request, url_for

import crawler

app = Flask(__name__)
USER = os.environ.get("APP_USER", "admin")
PASS = os.environ.get("APP_PASS", "")  # 비어 있으면 모든 접근을 거부한다(fail closed)
_running = threading.Event()

STATUS_LABEL = {
    "ok": "수집 성공", "blocked_robots": "robots.txt 차단", "blocked_bot": "봇 차단",
    "login_required": "로그인 필요", "no_products": "공개 상품 없음", "error": "오류",
}


def require_auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        auth = request.authorization
        ok = (PASS and auth and secrets.compare_digest(auth.username, USER)
              and secrets.compare_digest(auth.password or "", PASS))
        if not ok:
            return Response("인증이 필요합니다.", 401, {"WWW-Authenticate": 'Basic realm="private"'})
        return f(*a, **kw)
    return wrapper


@app.after_request
def private_headers(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.get("/healthz")
def healthz():
    return "ok"


def latest_products(c, where="", args=()):
    return c.execute(f"""
      SELECT p.* FROM products p
      JOIN (SELECT site, url, MAX(fetched_at) m FROM products GROUP BY site, url) l
        ON p.site=l.site AND p.url=l.url AND p.fetched_at=l.m {where}
      ORDER BY p.site, p.price""", args).fetchall()


@app.get("/")
@require_auth
def index():
    c = crawler.db()
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "")
    where, args = "WHERE 1=1", []
    if q:
        where += " AND p.name LIKE ?"; args.append(f"%{q}%")
    if cat:
        where += " AND p.category LIKE ?"; args.append(f"%{cat}%")
    products = latest_products(c, where, args)
    statuses = c.execute("SELECT * FROM site_status ORDER BY status, name").fetchall()
    counts = {}
    for s in statuses:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    # kg 단위 상품만 kg당 단가 계산해 카테고리·업체별 비교
    perkg = c.execute("""
      SELECT category, site, COUNT(*) n, ROUND(AVG(price/unit_value)) avg_kg, MIN(ROUND(price/unit_value)) min_kg, MAX(ROUND(price/unit_value)) max_kg
      FROM products WHERE unit='kg' AND unit_value>0 GROUP BY category, site ORDER BY category, avg_kg""").fetchall()
    c.close()
    return render_template("index.html", products=products, statuses=statuses, counts=counts,
                           labels=STATUS_LABEL, perkg=perkg, q=q, cat=cat, running=_running.is_set())


@app.post("/run")
@require_auth
def run():
    if not _running.is_set():
        _running.set()
        def job():
            try:
                crawler.run_all()
            finally:
                _running.clear()
        threading.Thread(target=job, daemon=True).start()
    return redirect(url_for("index"))


@app.get("/export.csv")
@require_auth
def export():
    c = crawler.db()
    rows = latest_products(c)
    c.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["업체", "카테고리", "상품명", "가격(원)", "규격", "URL", "수집시각"])
    for r in rows:
        w.writerow([r["site"], r["category"], r["name"], r["price"],
                    f'{r["unit_value"] or ""}{r["unit"] or ""}', r["url"], r["fetched_at"]])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=products.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
