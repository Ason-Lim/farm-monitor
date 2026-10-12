"""robots.txt를 준수하는 저속 크롤러.

원칙
- robots.txt가 막으면 수집하지 않고 'blocked_robots'로 기록한다.
- 403/429/봇 탐지/캡차를 만나면 즉시 중단하고 'blocked_bot'으로 기록한다. 우회·재시도하지 않는다.
- 로그인해야 가격이 보이는 곳은 'login_required'로 기록한다. 로그인하지 않는다.
- 사이트당 요청 수와 간격을 제한한다.
"""
import json, os, re, sqlite3, time, urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))
UA = os.environ.get("CRAWLER_UA", "FarmMonitorBot/1.0 (private market research)")
MAX_PAGES = int(os.environ.get("MAX_PAGES_PER_SITE", "25"))
DELAY = float(os.environ.get("CRAWL_DELAY_SEC", "3"))
TIMEOUT = 15

PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{3,7})\s*원")
UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|Kg|KG|g|G|ml|L|l)\b")
ITEM_HINT = re.compile(r"(item|goods|product|detail|view|shop/)", re.I)
BOT_HINT = re.compile(r"(captcha|access denied|cf-challenge|are you a robot|비정상적인 접근|자동 접근)", re.I)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS site_status(
      name TEXT PRIMARY KEY, category TEXT, url TEXT, status TEXT, detail TEXT, pages INTEGER, checked_at TEXT);
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT, category TEXT, name TEXT,
      price INTEGER, unit_value REAL, unit TEXT, url TEXT, fetched_at TEXT, UNIQUE(site, url, fetched_at));
    """)
    return c


def set_status(c, s, status, detail="", pages=0):
    c.execute("REPLACE INTO site_status VALUES(?,?,?,?,?,?,?)",
              (s["name"], s["category"], s["url"], status, detail, pages, now()))
    c.commit()


def parse_product(html, url):
    """JSON-LD Product > og 태그 > title 순으로 상품명, 본문 정규식으로 가격 추출."""
    soup = BeautifulSoup(html, "html.parser")
    name = price = None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("@type") == "Product":
                name = d.get("name")
                offers = d.get("offers") or {}
                offers = offers[0] if isinstance(offers, list) and offers else offers
                p = str(offers.get("price", "")) if isinstance(offers, dict) else ""
                if p.replace(".", "").isdigit():
                    price = int(float(p))
    if not name:
        og = soup.find("meta", property="og:title")
        name = (og.get("content") if og else None) or (soup.title.string if soup.title else None)
    if price is None:
        m = PRICE_RE.search(soup.get_text(" ", strip=True))
        if m:
            price = int(m.group(1).replace(",", ""))
    if not name or price is None:
        return None
    name = re.sub(r"\s+", " ", name).strip()[:200]
    u = UNIT_RE.search(name)
    return {"name": name, "price": price,
            "unit_value": float(u.group(1)) if u else None, "unit": u.group(2).lower() if u else None}


def crawl_site(c, s, session):
    base = s["url"]
    host = urlparse(base).netloc
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = session.get(urljoin(base, "/robots.txt"), timeout=TIMEOUT)
        if r.status_code in (401, 403):
            return set_status(c, s, "blocked_robots", f"robots.txt {r.status_code}")
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except requests.RequestException as e:
        return set_status(c, s, "error", f"robots.txt 조회 실패: {type(e).__name__}")
    if not rp.can_fetch(UA, base):
        return set_status(c, s, "blocked_robots", "robots.txt가 수집을 허용하지 않음")

    queue, seen, saved, fetched = [base], set(), 0, 0
    login_hits = 0
    while queue and fetched < MAX_PAGES:
        url = queue.pop(0)
        if url in seen or not rp.can_fetch(UA, url):
            continue
        seen.add(url)
        time.sleep(DELAY)
        try:
            r = session.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            return set_status(c, s, "error", f"{type(e).__name__}", fetched)
        fetched += 1
        if r.status_code in (403, 429, 503) or BOT_HINT.search(r.text[:5000]):
            return set_status(c, s, "blocked_bot", f"HTTP {r.status_code}, 봇 차단 감지 후 중단", fetched)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            continue
        r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
        item = parse_product(r.text, url) if url != base and ITEM_HINT.search(url) else None
        if item:
            c.execute("INSERT OR IGNORE INTO products(site,category,name,price,unit_value,unit,url,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                      (s["name"], s["category"], item["name"], item["price"], item["unit_value"], item["unit"], url, now()))
            saved += 1
        elif url != base and re.search(r"(로그인 후|회원(만|전용)|사업자 회원.*가입)", r.text[:20000]):
            login_hits += 1
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"]).split("#")[0]
            if urlparse(link).netloc == host and link not in seen and ITEM_HINT.search(link) and len(queue) < 200:
                queue.append(link)
    c.commit()
    if saved:
        return set_status(c, s, "ok", f"상품 {saved}건 저장", fetched)
    if login_hits:
        return set_status(c, s, "login_required", "가격이 회원 로그인 뒤에만 보이는 것으로 추정", fetched)
    return set_status(c, s, "no_products", "공개 상품 페이지를 찾지 못함(JS 렌더링·구조 상이 가능)", fetched)


def run_all(sites_path=None):
    sites = json.load(open(sites_path or os.path.join(os.path.dirname(__file__), "sites.json"), encoding="utf-8"))
    c = db()
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    for s in sites:
        try:
            crawl_site(c, s, session)
        except Exception as e:  # 한 사이트의 오류가 전체를 멈추지 않게 한다
            set_status(c, s, "error", f"{type(e).__name__}: {e}"[:200])
    c.close()


if __name__ == "__main__":
    run_all()
