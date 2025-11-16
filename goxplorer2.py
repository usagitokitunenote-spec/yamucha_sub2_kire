# goxplorer2.py — orevideo 専用スクレイパ
#
# ・https://orevideo.pythonanywhere.com/?sort=newest&page=N から
#   - https://video.twimg.com/...mp4?tag=xx  （twimg）
#   - https://gofile.io/d/XXXXXX             （gofile）
#   を収集
# ・gofile はページ 1〜10 を優先（それ以降は控え）
# ・collect_fresh_gofile_urls() で
#   - gofile 最大 3 本
#   - 残り twimg で埋めて WANT_POST 本（デフォ5）
# ・twimg だけ v.gd で短縮、gofile は生URLのまま
# ・state.json の posted_urls / recent_urls_24h を使って重複除外

import os
import re
import time
from typing import List, Set, Optional, Tuple

import requests

# =========================
#   設定
# =========================

BASE_ORIGIN = os.getenv("OREVIDEO_BASE", "https://orevideo.pythonanywhere.com").rstrip("/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/123.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_ORIGIN,
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "200"))  # orevideo 用は 200 で十分
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "80"))

# gofile を何本狙うか（デフォ 3）
GOFILE_TARGET = int(os.getenv("GOFILE_TARGET", "3"))

# gofile を「優先」する最大ページ
GOFILE_PRIORITY_MAX_PAGE = int(os.getenv("GOFILE_PRIORITY_MAX_PAGE", "10"))

# twimg / gofile 抽出用
TWIMG_RE  = re.compile(r"https?://video\.twimg\.com/[^\s\"']+?\.mp4\?tag=\d+", re.I)
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)


def _now() -> float:
    return time.monotonic()


def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts


def _normalize_url(u: str) -> str:
    if not u:
        return u
    u = u.strip()
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    return u.rstrip("/")


# =========================
#   v.gd 短縮（twimg 用）
# =========================

def shorten_via_vgd(long_url: str) -> str:
    """
    v.gd の API で URL を短縮。
    失敗したら元 URL をそのまま返す。
    """
    try:
        r = requests.get(
            "https://v.gd/create.php",
            params={"format": "simple", "url": long_url},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        short = (r.text or "").strip()
        if short.startswith("http"):
            return short
    except Exception as e:
        print(f"[warn] v.gd shorten failed for {long_url}: {e}")
    return long_url


# =========================
#   HTML からリンク抽出
# =========================

def extract_links_from_html(html: str) -> Tuple[List[str], List[str]]:
    """
    orevideo のページ HTML から
      - twimg mp4
      - gofile
    を抜き出す。
    戻り値: (twimg_list, gofile_list)
    """
    if not html:
        return [], []

    tw = TWIMG_RE.findall(html)
    gf = GOFILE_RE.findall(html)

    # ページ内での重複排除（順序維持）
    def unique(seq: List[str]) -> List[str]:
        seen = set()
        out = []
        for s in seq:
            s = s.strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    tw_u = unique(tw)
    gf_u = unique(gf)

    print(f"[debug] extract_links_from_html: twimg={len(tw_u)}, gofile={len(gf_u)}")
    return tw_u, gf_u


# =========================
#   orevideo からリンク収集
# =========================

def _collect_orevideo_links(
    num_pages: int,
    deadline_ts: Optional[float],
) -> Tuple[List[str], List[str], List[str]]:
    """
    orevideo のページを 1..num_pages まで巡回してリンクを集める。
    戻り値: (twimg_all, gofile_early, gofile_late)
      - gofile_early … page <= GOFILE_PRIORITY_MAX_PAGE の gofile
      - gofile_late  … page >  GOFILE_PRIORITY_MAX_PAGE の gofile
    """
    twimg_all: List[str] = []
    gofile_early: List[str] = []
    gofile_late: List[str] = []

    total_raw = 0

    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] orevideo deadline at page={p}; stop.")
            break

        if p == 1:
            url = f"{BASE_ORIGIN}/?sort=newest&page=1"
        else:
            url = f"{BASE_ORIGIN}/?page={p}&sort=newest"

        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
        except Exception as e:
            print(f"[warn] orevideo request failed: {url} ({e})")
            continue

        if resp.status_code != 200:
            print(f"[warn] orevideo status {resp.status_code}: {url}")
            continue

        html = resp.text
        tw_list, gf_list = extract_links_from_html(html)
        print(f"[info] orevideo list {url}: twimg={len(tw_list)}, gofile={len(gf_list)}")

        twimg_all.extend(tw_list)

        if p <= GOFILE_PRIORITY_MAX_PAGE:
            gofile_early.extend(gf_list)
        else:
            gofile_late.extend(gf_list)

        total_raw = len(twimg_all) + len(gofile_early) + len(gofile_late)
        if total_raw >= RAW_LIMIT:
            print(f"[info] orevideo early stop at RAW_LIMIT={RAW_LIMIT}")
            break

        time.sleep(0.3)

    return twimg_all, gofile_early, gofile_late


# =========================
#   fetch_listing_pages（互換用／実際はあまり使わない）
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None,
) -> List[str]:
    """
    bot.py 互換用のダミー実装。
    実際の URL 選別は collect_fresh_gofile_urls 側で行うため、
    ここでは twimg + gofile を全部まとめて返すだけ。
    """
    tw, gf_early, gf_late = _collect_orevideo_links(num_pages=num_pages, deadline_ts=deadline_ts)
    all_urls = tw + gf_early + gf_late
    return all_urls[:RAW_LIMIT]


# =========================
#   collect_fresh_gofile_urls（bot.py から呼ばれるメイン）
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 5,
    num_pages: int = 50,
    deadline_sec: Optional[int] = None,
) -> List[str]:
    """
    orevideo 用の URL 選別ロジック。

    - orevideo から twimg / gofile を収集
    - gofile はページ 1〜GOFILE_PRIORITY_MAX_PAGE のものを優先
    - 1ツイートあたり:
        gofile : 最大 GOFILE_TARGET 本
        twimg  : 残りを埋めて合計 want 本
    - twimg だけ v.gd で短縮、gofile は生URLのまま
    - already_seen / このrun内の seen_now で重複を避ける
    - MIN_POST 未満なら [] を返す（bot.py 側でツイートしない）
    """

    # MIN_POST を環境変数から取得（パースできなければ 1）
    try:
        min_post = int(os.getenv("MIN_POST", "1"))
    except ValueError:
        min_post = 1

    # デッドライン設定
    if deadline_sec is None:
        env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if env:
                deadline_sec = int(env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    # orevideo から raw リンク収集
    tw_all, gf_early, gf_late = _collect_orevideo_links(num_pages=num_pages, deadline_ts=deadline_ts)

    # 目標本数
    go_target = min(GOFILE_TARGET, want)
    # 残りは twimg で埋める
    tw_target = max(0, want - go_target)

    results: List[str] = []
    selected_gofile: List[str] = []
    selected_twimg: List[str] = []
    seen_now: Set[str] = set()

    def pick_url(raw_url: str, kind: str) -> str | None:
        """
        kind = "gofile" or "twimg"
        - gofile: 生URLをそのまま使う
        - twimg:  v.gd で短縮した URL を使う
        """
        if not raw_url:
            return None

        raw_norm = _normalize_url(raw_url)

        # gofile は state.json にも生URLで保存されるので、そのままチェック
        # twimg は短縮後のURLで state.json に保存されるので、
        #   - 生URLがたまたま入っているケースも考えて一応見る
        if raw_norm in already_seen:
            return None

        if kind == "twimg":
            final = shorten_via_vgd(raw_url)
        else:
            final = raw_url

        final_norm = _normalize_url(final)

        # この run 内の重複 + state.json の重複
        if final_norm in seen_now or final_norm in already_seen:
            return None

        seen_now.add(final_norm)
        return final

    # 1) gofile (優先ページ 1〜GOFILE_PRIORITY_MAX_PAGE)
    for url in gf_early:
        if len(selected_gofile) >= go_target:
            break
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during gofile-early selection; stop.")
            break
        pick = pick_url(url, kind="gofile")
        if pick:
            selected_gofile.append(pick)

    # 2) gofile (残りはそれ以降のページから)
    if len(selected_gofile) < go_target:
        for url in gf_late:
            if len(selected_gofile) >= go_target:
                break
            if _deadline_passed(deadline_ts):
                print("[info] deadline reached during gofile-late selection; stop.")
                break
            pick = pick_url(url, kind="gofile")
            if pick:
                selected_gofile.append(pick)

    # 現時点での gofile 本数
    current_go = len(selected_gofile)

    # twimg で埋めるべき残り本数
    remaining = max(0, want - current_go)

    # 3) twimg （全ページから）
    for url in tw_all:
        if len(selected_twimg) >= remaining:
            break
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during twimg selection; stop.")
            break
        pick = pick_url(url, kind="twimg")
        if pick:
            selected_twimg.append(pick)

    results = selected_gofile + selected_twimg

    print(
        f"[info] orevideo selected: gofile={len(selected_gofile)}, "
        f"twimg={len(selected_twimg)}, total={len(results)} (target={want})"
    )

    # MIN_POST 未満なら「何も無かった扱い」
    if len(results) < min_post:
        print(f"[info] only {len(results)} urls collected (< MIN_POST={min_post}); return [].")
        return []

    return results[:want]
