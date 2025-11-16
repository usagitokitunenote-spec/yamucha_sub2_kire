# bot_orevideo.py — orevideo 用（ロジックは bot.py と同じ、goxplorer2 を使うだけ）

import json, os, re, time
from datetime import datetime, timezone, timedelta
from dateutil import tz
import tweepy
from playwright.sync_api import sync_playwright

from goxplorer2 import collect_fresh_gofile_urls  # ← ここだけ違う

AFFILIATE_URL = "https://amzn.to/3Kq0QGm"
STATE_FILE = "state.json"
DAILY_LIMIT = 16
JST = tz.gettz("Asia/Tokyo")
TWEET_LIMIT = 280
TCO_URL_LEN = 23
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

ZWSP = "\u200B"; ZWNJ = "\u200C"; INVISIBLES = [ZWSP, ZWNJ]

def _env_int(key, default): 
    try: return int(os.getenv(key, str(default)))
    except: return default

WANT_POST = _env_int("WANT_POST", 5)   # ← 揃い次第 その場で投稿（YMLから渡す）
MIN_POST  = _env_int("MIN_POST", 3)    # ← 足りないときはスキップ
HARD_LIMIT_SEC = _env_int("HARD_LIMIT_SEC", 600)
USE_API_TIMELINE = _env_int("USE_API_TIMELINE", 0)

def _default_state():
    return {"posted_urls": [], "last_post_date": None, "posts_today": 0,
            "recent_urls_24h": [], "line_seq": 1}

def load_state():
    if not os.path.exists(STATE_FILE): return _default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = _default_state()
    for k, v in _default_state().items():
        if k not in data: data[k] = v
    return data

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def reset_if_new_day(state, now_jst):
    today = now_jst.date().isoformat()
    if state.get("last_post_date") != today:
        state["last_post_date"] = today
        state["posts_today"] = 0

def purge_recent_12h(state, now_utc):
    cutoff = now_utc - timedelta(hours=12)
    buf = []
    for item in state.get("recent_urls_24h", []):
        try:
            ts = datetime.fromisoformat(item.get("ts"))
            if ts >= cutoff: buf.append(item)
        except: pass
    state["recent_urls_24h"] = buf

def normalize_url(u):
    if not u: return u
    u = u.strip()
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    return u.rstrip("/")

def build_seen_set_from_state(state):
    seen = set()
    for u in state.get("posted_urls", []): seen.add(normalize_url(u))
    for it in state.get("recent_urls_24h", []): seen.add(normalize_url(it.get("url")))
    return seen

def estimate_tweet_len_tco(text: str) -> int:
    def repl(m): return "U" * TCO_URL_LEN
    return len(re.sub(r"https?://\S+", repl, text))

def compose_fixed5_text(gofile_urls, start_seq: int, salt_idx: int = 0, add_sig: bool = True):
    invis = INVISIBLES[salt_idx % len(INVISIBLES)]
    lines, seq = [], start_seq
    take = min(WANT_POST, len(gofile_urls))
    for i, u in enumerate(gofile_urls[:take]):
        lines.append(f"{seq}{invis}. {u}")
        if i < take - 1: lines.append(AFFILIATE_URL)
        seq += 1
    text = "\n".join(lines)
    if add_sig:
        seed = (start_seq * 1315423911) ^ int(time.time() // 60)
        sig = "".join(INVISIBLES[(seed >> i) & 1] for i in range(16))
        text += sig
    return text, take

def get_client():
    return tweepy.Client(
        bearer_token=None,
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=bool(_env_int("WAIT_ON_RATE_LIMIT", 0)),
    )

def fetch_recent_urls_via_web(username: str, scrolls: int = 1, wait_ms: int = 800) -> set:
    # 既定では使わない（USE_API_TIMELINE=1の場合などで使う）
    if not username: return set()
    url = f"https://x.com/{username}"
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/123.0.0.0"),
            locale="ja-JP")
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(wait_ms)
        for _ in range(scrolls):
            page.mouse.wheel(0, 1800); page.wait_for_timeout(wait_ms)
        html = page.content()
        ctx.close(); browser.close()
    for m in GOFILE_RE.findall(html):
        seen.add(normalize_url(m))
    return seen

def post_to_x_v2(client, text: str):
    return client.create_tweet(text=text)

def main():
    start_ts = time.monotonic()
    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)

    state = load_state()
    purge_recent_12h(state, now_utc)
    reset_if_new_day(state, now_jst)

    if state.get("posts_today", 0) >= DAILY_LIMIT:
        print("Daily limit reached; skip."); return

    already_seen = build_seen_set_from_state(state)

    # 既出チェックは state を主に使用。必要時だけTLを見る（既定OFF）。
    if USE_API_TIMELINE:
        try:
            client = get_client()
            me = client.get_me(user_auth=True)
            user = me.data if me and me.data else None
            username = getattr(user, "username", None)
        except Exception:
            username = os.getenv("X_SCREEN_NAME", None)
        web_seen = fetch_recent_urls_via_web(username, scrolls=1, wait_ms=800) if username else set()
        if web_seen:
            already_seen |= web_seen
        print(f"[info] recent timeline gofiles via WEB (opt): {len(web_seen)} (user={username})")
    else:
        print("[info] timeline check skipped (USE_API_TIMELINE=0)")

    if (time.monotonic() - start_ts) > HARD_LIMIT_SEC:
        print("[warn] time budget exceeded before collection; abort."); return

    # 締切
    try:
        deadline_env = os.getenv("SCRAPE_TIMEOUT_SEC")
        deadline_sec = int(deadline_env) if deadline_env else None
    except Exception:
        deadline_sec = None

    # ★ orevideo 用 collect_fresh_gofile_urls を呼ぶ
    urls = collect_fresh_gofile_urls(
        already_seen=already_seen,
        want=WANT_POST,
        num_pages=int(os.getenv("NUM_PAGES", "50")),
        deadline_sec=deadline_sec
    )
    print(f"[info] collected alive urls: {len(urls)}")
    if len(urls) < MIN_POST:
        print("Not enough alive URLs; skip.")
        return

    # そのまま投稿
    start_seq = int(state.get("line_seq", 1))
    salt = (now_jst.hour + now_jst.minute) % len(INVISIBLES)
    status_text, taken = compose_fixed5_text(urls, start_seq=start_seq, salt_idx=salt, add_sig=True)

    if estimate_tweet_len_tco(status_text) > TWEET_LIMIT:
        status_text = status_text.replace(". https://", ".https://")
    while estimate_tweet_len_tco(status_text) > TWEET_LIMIT:
        status_text = status_text.rstrip(ZWSP + ZWNJ)

    client = get_client()
    resp = post_to_x_v2(client, status_text)
    tweet_id = resp.data.get("id") if resp and resp.data else None
    print(f"[info] tweeted id={tweet_id}")

    # state 更新
    for u in urls[:WANT_POST]:
        if u not in state["posted_urls"]: state["posted_urls"].append(u)
        state["recent_urls_24h"].append({"url": u, "ts": now_utc.isoformat()})
    state["posts_today"] = state.get("posts_today", 0) + 1
    state["line_seq"] = start_seq + min(WANT_POST, len(urls))
    save_state(state)
    print(f"Posted ({min(WANT_POST,len(urls))} gofiles + {min(WANT_POST,len(urls))-1} amazon):", status_text)

if __name__ == "__main__":
    main()
