"""
대만 게임 커뮤니티 (forum.gamer.com.tw) 리니지W 자유게시판 크롤러
- 원글 + 댓글까지 수집 -> 한국어 번역(인게임 용어 보정) -> HTML 대시보드 생성

사용법:
  python crawler.py              # 전체 최신 글 수집
  python crawler.py --test       # 05-03 게시글만 수집 (테스트)
  python crawler.py --date 05-06 # 특정 날짜만 수집 (MM-DD 형식)
"""

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
import json, time, sys, io, re, urllib3
from datetime import datetime
from pathlib import Path

# Windows 콘솔 UTF-8 출력
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 사내 프록시 환경 SSL 우회
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_orig_req = requests.Session.request
def _no_ssl_verify(self, method, url, **kwargs):
    kwargs.setdefault("verify", False)
    return _orig_req(self, method, url, **kwargs)
requests.Session.request = _no_ssl_verify

# ── 설정 ──────────────────────────────────────────────────────────────────────
BOARD_URL     = "https://forum.gamer.com.tw/B.php?bsn=71905"
BASE_URL      = "https://forum.gamer.com.tw"
DATA_FILE     = Path(__file__).parent / "posts.json"
TERMS_FILE    = Path(__file__).parent / "terms.json"
OUTPUT_HTML   = Path(__file__).parent / "dashboard.html"
REQUEST_DELAY = 2.0    # 요청 간 딜레이(초)
MAX_PAGES     = 5      # 목록 최대 페이지 수
MAX_BODY_LEN  = 4000   # 번역할 본문 최대 글자 수
MAX_COMMENTS  = 20     # 수집할 댓글 최대 수

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": "https://forum.gamer.com.tw/",
}

translator = GoogleTranslator(source="zh-TW", target="ko")


# ── 인게임 용어 사전 ────────────────────────────────────────────────────────────
# 2~3글자 핵심 게임 용어만 플레이스홀더 허용 (일반 한자 제외)
_CORE_SHORT = {
    "天堂", "黑妖", "弓手", "精靈", "龍騎", "妖精", "君主", "法師", "術士", "羅剎",
    "槍手", "技能", "紅技", "紫技", "藍技", "被動", "主動", "冷卻", "覺醒", "二覺",
    "三覺", "轉職", "副本", "地城", "攻城", "圍城", "強化", "精煉", "附魔", "裝備",
    "武器", "防具", "屬性", "傷害", "血盟", "公會", "八開", "巴哈",
    "龍騎士", "黑暗精靈", "黑妖精", "幻術師", "奈米課", "精靈弓手",
}

def load_terms() -> dict[str, str]:
    """
    terms.json 로드.
    - 4글자 이상: 고유명사(스킬명·NPC명·던전명 등), 전부 사용
    - 2~3글자: _CORE_SHORT 화이트리스트만 사용 (일반 한자 과치환 방지)
    """
    if not TERMS_FILE.exists():
        return {}
    raw = json.loads(TERMS_FILE.read_text(encoding="utf-8"))
    return {
        zh: kr
        for zh, kr in raw.items()
        if not zh.startswith("_") and "──" not in kr
        and (len(zh) >= 4 or zh in _CORE_SHORT)
    }


def apply_placeholders(text: str, terms: dict[str, str]) -> tuple[str, dict[str, str]]:
    """번역 전: 인게임 용어를 플레이스홀더 토큰으로 교체."""
    placeholder_map: dict[str, str] = {}
    for i, (zh, kr) in enumerate(terms.items()):
        if zh in text:
            token = f"ZZT{i:04d}ZZ"
            text = text.replace(zh, token)
            placeholder_map[token] = kr
    return text, placeholder_map


def restore_placeholders(text: str, placeholder_map: dict[str, str]) -> str:
    """번역 후: 플레이스홀더를 한국어 게임 용어로 복원."""
    for token, kr in placeholder_map.items():
        text = text.replace(token, kr)
    return text


# ── 번역 ───────────────────────────────────────────────────────────────────────
def translate(text: str, terms: dict[str, str]) -> str:
    """인게임 용어 보정 후 Google 번역. 플레이스홀더 번역 실패 시 원문으로 재시도."""
    if not text:
        return ""
    chunk = text[:MAX_BODY_LEN] + ("..." if len(text) > MAX_BODY_LEN else "")
    chunk_tok, ph_map = apply_placeholders(chunk, terms)
    try:
        result = translator.translate(chunk_tok)
        return restore_placeholders(result, ph_map)
    except Exception:
        # 토큰 과밀로 실패 → 원문으로 폴백 번역
        try:
            return translator.translate(chunk)
        except Exception as e:
            print(f"  [오류] 번역 실패: {e}")
            return chunk


# ── 크롤링: 목록 ───────────────────────────────────────────────────────────────
def fetch_board(target_date: str | None = None,
                from_date: str | None = None) -> list[dict]:
    """
    게시판 목록에서 게시글 기본 정보 수집.
    날짜 형식 예시: '05-03 06:16' (MM-DD HH:MM)
    target_date : 'MM-DD' 정확히 일치하는 날짜만 수집
    from_date   : 'MM-DD' 이후(포함) 게시글 전부 수집
    """
    posts: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        url = f"{BOARD_URL}&page={page}"
        print(f"  [목록] {page}페이지 요청...")

        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"  [오류] 목록 요청 실패: {e}")
            break

        soup = BeautifulSoup(r.text, "lxml")
        rows = soup.select("tr.b-list__row")

        if not rows:
            print("  [경고] 게시글 행을 찾지 못했습니다. debug_board.html 저장")
            (Path(__file__).parent / "debug_board.html").write_text(r.text, encoding="utf-8")
            break

        page_has_target = False
        page_past_target = False

        for row in rows:
            # 제목 없는 행(공지·이벤트 특수 행) 스킵
            title_el = row.select_one("p.b-list__main__title")
            if not title_el:
                continue

            title = title_el.text.strip()

            # 게시글 URL: td.b-list__main 안의 첫 번째 <a>
            link_el  = row.select_one("td.b-list__main > a")
            href     = link_el.get("href", "") if link_el else ""
            post_url = BASE_URL + "/" + href.lstrip("/") if href else ""

            # 마지막 활동 날짜 텍스트
            date_el  = row.select_one(".b-list__time__edittime a")
            date_str = date_el.text.strip() if date_el else ""

            # ── 날짜 필터 처리 ──────────────────────────────────────────────
            is_mm_dd = date_str and len(date_str) >= 5 and "-" in date_str[:5]

            if from_date:
                # from_date 이후(포함): date < from_date 이면 수집 중단
                if is_mm_dd:
                    if date_str[:5] < from_date:
                        page_past_target = True
                        break
                elif not date_str:
                    continue  # 날짜 없으면 스킵
                page_has_target = True

            elif target_date:
                if target_date not in date_str:
                    if is_mm_dd and date_str[:5] < target_date and posts:
                        page_past_target = True
                        break
                    continue
                page_has_target = True

            # 작성자
            author_el = row.select_one(".b-list__count__user a")
            author    = author_el.text.strip() if author_el else "-"

            # 상호작용 수 (댓글/인기)
            count_el  = row.select_one(".b-list__count__number")
            count_str = count_el.get_text(" ", strip=True) if count_el else ""

            posts.append({
                "title":   title,
                "url":     post_url,
                "author":  author,
                "date":    date_str,
                "count":   count_str,
            })

        if page_past_target:
            break

        # 다음 페이지 버튼
        next_btn = (
            soup.select_one("a.next-page")
            or soup.select_one("a[class*='next']")
            or soup.select_one("a[title*='下一頁']")
        )
        if not next_btn:
            break

        time.sleep(REQUEST_DELAY)

    return posts


# ── 크롤링: 원글 + 댓글 ────────────────────────────────────────────────────────
def _extract_post_author(post_el) -> str:
    """포스트 요소에서 사이트 표시 닉네임(한자 포함) 추출."""
    # home.gamer.com.tw 링크가 두 개 있을 때 첫 번째 = 표시명, 두 번째 = 계정 ID
    links = post_el.select("a[href*='home.gamer.com.tw']")
    if links:
        return links[0].get_text(strip=True) or "-"
    return "-"


def _extract_post_date(post_el) -> str:
    """포스트 요소에서 날짜 추출."""
    info = post_el.select_one(".c-post__header__info")
    if info:
        return info.get_text(strip=True)
    return ""


def _extract_post_floor(post_el) -> int:
    """포스트 요소에서 층수 추출. 원글은 1층."""
    header = post_el.select_one(".c-post__header__author")
    if header:
        txt = header.get_text(strip=True)
        m = re.search(r"^(\d+)\s*樓", txt)
        if m:
            return int(m.group(1))
        if "樓主" in txt:
            return 1
    return 0


MAX_POST_PAGES = 3  # 포스트 1개당 최대 tnum 페이지 수
MAX_SUB_REPLIES = 30  # 서브 리플라이 최대 수집 수


def _fetch_soup(url: str):
    """URL을 요청해 BeautifulSoup 반환. 실패 시 None."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print(f"  [오류] 요청 실패 ({url[-40:]}): {e}")
        return None


def _get_snB(post_el) -> int | None:
    """포스트 요소의 .more-reply onclick에서 snB 추출."""
    el = post_el.select_one(".more-reply[onclick]")
    if el:
        m = re.search(r"extendComment\(\s*\d+\s*,\s*(\d+)\s*\)", el.get("onclick", ""))
        if m:
            return int(m.group(1))
    return None


def _fetch_all_sub_replies(bsn: int, snA: int, snB: int) -> list[dict]:
    """moreCommend AJAX API로 접힌 서브 리플라이 전체 로드."""
    url = (f"https://forum.gamer.com.tw/ajax/moreCommend.php"
           f"?bsn={bsn}&snA={snA}&snB={snB}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()
    except Exception:
        return []
    replies = []
    for key, item in data.items():
        if key == "next_snC" or not isinstance(item, dict):
            continue
        if item.get("state") == "exist":
            replies.append({
                "author":  item.get("nick") or item.get("userid", "-"),
                "date":    item.get("wtime", ""),
                "content": item.get("content") or item.get("comment", ""),
            })
    return sorted(replies, key=lambda x: x["date"])[:MAX_SUB_REPLIES]


def _parse_sub_replies_html(post_el) -> list[dict]:
    """HTML에 이미 렌더된 서브 리플라이 수집 (닉네임 포함)."""
    items = []
    for reply_el in post_el.select(".c-reply__item"):
        art = reply_el.select_one(".reply-content__article")
        content = art.get_text(separator="\n", strip=True) if art else ""
        if not content:
            continue
        user = reply_el.select_one(".reply-content__user")
        author = user.get_text(strip=True) if user else "-"
        # footer: "B17 2026-05-03 00:40:44 …"
        footer = reply_el.select_one(".reply-content__footer")
        date = ""
        if footer:
            ft = footer.get_text(" ", strip=True)
            dm = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", ft)
            if dm:
                date = dm.group()
        items.append({"author": author, "date": date, "content": content})
    return items


def _parse_comments(post_els, result: dict, seen_floors: set):
    """post_els 리스트에서 댓글 추출해 result['comments']에 추가."""
    for post_el in post_els:
        if len(result["comments"]) >= MAX_COMMENTS:
            break
        body_el = post_el.select_one(".c-article__content")
        content = body_el.get_text(separator="\n", strip=True) if body_el else ""
        if not content:
            continue

        floor  = _extract_post_floor(post_el)
        if floor in seen_floors:
            continue
        seen_floors.add(floor)

        author = _extract_post_author(post_el)
        date   = _extract_post_date(post_el)

        sub_replies = _parse_sub_replies_html(post_el)

        result["comments"].append({
            "floor":       floor,
            "author":      author,
            "date":        date,
            "content":     content,
            "sub_replies": sub_replies,
        })


def fetch_post_page(url: str) -> dict:
    """
    포스트 페이지에서 원글 본문 + 댓글 수집.
    - tnum=1: 원글 본문 취득
    - 게시판 URL의 tnum(최신 층): 최신 댓글 취득
    - 필요 시 추가 페이지까지 순방향 수집
    반환: {"body": str, "comments": [...]}
    """
    base_url = re.sub(r"&tnum=\d+", "", url)

    # 게시판에서 가져온 tnum (= 해당 글의 최신 층 번호)
    m = re.search(r"tnum=(\d+)", url)
    board_tnum = int(m.group(1)) if m else 1

    result     = {"body": "", "comments": []}
    seen_floors = set()

    # ── 1) tnum=1: 원글 본문 + 앞쪽 댓글 ─────────────────────────────────────
    soup1 = _fetch_soup(base_url + "&tnum=1")
    if not soup1:
        return result

    post_els1 = soup1.select(".c-post")
    if not post_els1:
        (Path(__file__).parent / "debug_post.html").write_text(
            soup1.prettify(), encoding="utf-8"
        )
        print("  [경고] 포스트 선택자 미매칭 -> debug_post.html 저장")
        return result

    # 첫 번째 .c-post = 원글
    orig_post = post_els1[0]
    body_el = orig_post.select_one(".c-article__content")
    result["body"] = body_el.get_text(separator="\n", strip=True) if body_el else ""
    seen_floors.add(1)

    # 원글에 달린 서브 리플라이(短留言)를 AJAX로 전체 수집
    bsn_m  = re.search(r"bsn=(\d+)", url)
    snA_m  = re.search(r"snA=(\d+)", url)
    bsn_v  = int(bsn_m.group(1)) if bsn_m else 0
    snA_v  = int(snA_m.group(1)) if snA_m else 0
    snB_v  = _get_snB(orig_post)
    if snB_v:
        time.sleep(REQUEST_DELAY)
        ajax_subs = _fetch_all_sub_replies(bsn_v, snA_v, snB_v)
        if ajax_subs:
            result["orig_sub_replies"] = ajax_subs
            print(f"         원글 서브 리플라이 {len(ajax_subs)}개 수집")
    else:
        # 접힌 단문 댓글 없음 → HTML에 직접 렌더된 단문 댓글 수집
        html_subs = _parse_sub_replies_html(orig_post)
        if html_subs:
            result["orig_sub_replies"] = html_subs
            print(f"         원글 서브 리플라이 (HTML) {len(html_subs)}개 수집")

    _parse_comments(post_els1[1:], result, seen_floors)

    # ── 2) board_tnum 페이지: 최신 댓글 (tnum=1 페이지와 다를 때만) ──────────
    if board_tnum > 1 and len(result["comments"]) < MAX_COMMENTS:
        time.sleep(REQUEST_DELAY)
        soup_b = _fetch_soup(base_url + f"&tnum={board_tnum}")
        if soup_b:
            post_els_b = soup_b.select(".c-post")
            _parse_comments(post_els_b, result, seen_floors)

            # ── 3) board_tnum 이후 추가 페이지 (페이지네이션 탐색) ──────────
            for _ in range(MAX_POST_PAGES - 1):
                if len(result["comments"]) >= MAX_COMMENTS:
                    break
                next_a = (
                    soup_b.select_one("a.next-page")
                    or soup_b.select_one("a[title='下一頁']")
                    or soup_b.select_one(".BH-pagebtnA a:last-child")
                )
                if not next_a:
                    break
                next_href = next_a.get("href", "")
                if "tnum=" not in next_href:
                    break
                time.sleep(REQUEST_DELAY)
                next_url = ("https://forum.gamer.com.tw/" + next_href.lstrip("/")
                            if next_href.startswith("/") else next_href)
                soup_b = _fetch_soup(next_url)
                if not soup_b:
                    break
                _parse_comments(soup_b.select(".c-post"), result, seen_floors)

    return result


# ── 데이터 저장/로드 ────────────────────────────────────────────────────────────
def load_posts() -> list[dict]:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []


def save_posts(posts: list[dict]):
    DATA_FILE.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")


# ── HTML 대시보드 생성 ──────────────────────────────────────────────────────────
def _safe(text: str) -> str:
    """HTML 특수문자 이스케이프."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_date(date_str: str) -> str:
    """중국어 날짜 표현 → 한국어 변환."""
    s = re.sub(r'(\d+)\s*分前', r'\1분 전', date_str)
    s = re.sub(r'(\d+)\s*小時前', r'\1시간 전', s)
    s = re.sub(r'昨天', '어제', s)
    s = re.sub(r'前天', '그저께', s)
    s = re.sub(r'剛才', '방금', s)
    return s


def _parse_reply_count(count_str: str) -> int:
    """'23 / 2258' 형태에서 댓글 수 추출."""
    m = re.match(r'(\d+)', (count_str or "").strip())
    return int(m.group(1)) if m else 0


def _sub_reply_html(sr_list: list) -> str:
    """서브 리플라이 목록을 HTML로 변환. 항목은 dict 또는 str 허용."""
    html = ""
    for sr in sr_list:
        if isinstance(sr, dict):
            nick    = _safe(sr.get("author", ""))
            date_sr = _safe(sr.get("date", ""))
            body_sr = _safe(sr.get("content", ""))
            meta    = f'<span class="subreply-nick">{nick}</span>'
            if date_sr:
                meta += f' <span class="subreply-date">· {date_sr}</span>'
            html += f'<div class="subreply">{meta}<span class="subreply-body"> {body_sr}</span></div>'
        else:
            html += f'<div class="subreply">{_safe(sr)}</div>'
    return html


def _build_comment_html(post: dict) -> str:
    comments     = post.get("comments", [])
    orig_subs    = post.get("orig_sub_replies_kr", post.get("orig_sub_replies", []))
    total = len(comments) + len(orig_subs)
    if total == 0:
        return ""

    items = ""

    # ── 원글 서브 리플라이 (短留言) ──────────────────────────────────────────
    if orig_subs:
        items += f"""
        <div class="orig-sub-section">
          <div class="orig-sub-title">&#128172; 원글 단문 댓글 {len(orig_subs)}개</div>
          {_sub_reply_html(orig_subs)}
        </div>"""

    # ── 층 댓글 ──────────────────────────────────────────────────────────────
    for c in comments:
        author   = _safe(c.get("author", "-"))
        date     = _safe(c.get("date", ""))
        body     = _safe(c.get("content_kr", c.get("content", "")))
        sub_html = _sub_reply_html(c.get("sub_replies_kr", c.get("sub_replies", [])))

        items += f"""
        <div class="comment">
          <div class="comment-meta"><span class="comment-nick">{author}</span> &nbsp;·&nbsp; {date}</div>
          <p class="comment-body">{body}</p>
          {sub_html}
        </div>"""

    return f'<div class="comment-section">{items}</div>'


def generate_html(posts: list[dict]):
    today_mm_dd = datetime.now().strftime("%m-%d")
    today_date  = datetime.now().strftime("%Y-%m-%d")
    current_ym  = datetime.now().strftime("%Y-%m")
    current_mm  = datetime.now().strftime("%m-")
    now_str     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _is_today(ds: str) -> bool:
        return bool(ds) and ("前" in ds or "剛才" in ds or ds.startswith(today_mm_dd))

    def _is_updated(p: dict) -> bool:
        u = p.get("updated_at", "")
        return bool(u) and u[:10] == today_date

    def _is_this_month(p: dict) -> bool:
        ds = p.get("date", "")
        if "前" in ds or "剛才" in ds or ds.startswith(current_mm):
            return True
        if p.get("crawled_at", "")[:7] == current_ym:
            return True
        if p.get("updated_at", "")[:7] == current_ym:
            return True
        return False

    def _sort_key(p: dict) -> int:
        if _is_today(p.get("date", "")): return 0
        if _is_updated(p): return 1
        return 2

    # 이번 달 게시글만 표시 (posts.json 전체 누적 데이터 중 필터)
    display_posts = [p for p in sorted(posts, key=_sort_key) if _is_this_month(p)]

    # 댓글 HTML을 JS 배열로 저장 → 클릭 시에만 DOM 삽입 (초기 렌더 속도 개선)
    cmt_list = [_build_comment_html(p) for p in display_posts]
    cmt_json = json.dumps(cmt_list, ensure_ascii=False)

    items_html = ""
    for i, p in enumerate(display_posts):
        date_str = p.get("date", "")
        body     = _safe(p.get("body_kr", ""))
        preview  = (body[:200] + "...") if len(body) > 200 else body
        title_kr = _safe(p.get("title_kr", p.get("title", "")))
        date_kr  = _safe(_format_date(date_str))

        today_f   = _is_today(date_str)
        updated_f = _is_updated(p)
        cls = "post-item" + (" today" if today_f else "") + (" updated" if updated_f else "")

        badge = ""
        if today_f:
            badge = '<span class="badge badge-new">NEW</span> '
        elif updated_f:
            badge = '<span class="badge badge-upd">UPDATE</span> '

        n_total = (len(p.get("comments", [])) +
                   len(p.get("orig_sub_replies_kr", p.get("orig_sub_replies", []))))

        cmt_btn = drawer = ""
        if n_total > 0 and cmt_list[i]:
            lbl     = f"&#128172; 댓글 {n_total}개"
            cmt_btn = (f'<button class="comment-btn" onclick="toggleDrw(this)"'
                       f' data-label="{lbl}">{lbl}</button>')
            drawer  = '<div class="comment-drawer" hidden></div>'

        url = p.get("url", "#")
        items_html += f"""
<article class="{cls}" data-idx="{i}">
  <div class="post-row">
    <div class="post-left">
      <div class="title-row">{badge}<a class="post-title" href="{url}" target="_blank" rel="noopener">{title_kr}</a></div>
      <div class="post-meta">{_safe(p.get('author','-'))} &nbsp;·&nbsp; {date_kr} &nbsp;·&nbsp; {_safe(p.get('count',''))}</div>
      <p class="post-preview">{preview or '(본문 없음)'}</p>
    </div>
    <div class="post-right">
      {cmt_btn}
      <a class="view-btn" href="{url}" target="_blank" rel="noopener">원문 &rarr;</a>
    </div>
  </div>
  {drawer}
</article>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>리니지W 대만 커뮤니티 모니터링</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Malgun Gothic', Arial, sans-serif; background: #f0f2f5; color: #222; min-height: 100vh; }}
.page-header {{
  background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
  color: #fff; padding: 14px 28px;
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px;
  position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 8px rgba(0,0,0,.35);
}}
.page-header h1 {{ font-size: 1rem; font-weight: 700; }}
.page-header .info {{ color: #90caf9; font-size: .72rem; margin-top: 3px; }}
.post-list {{ max-width: 1100px; margin: 16px auto; padding: 0 16px 40px; display: flex; flex-direction: column; gap: 5px; }}
.post-item {{
  background: #fff; border-radius: 10px;
  box-shadow: 0 1px 4px rgba(0,0,0,.07);
  border-left: 4px solid transparent; overflow: hidden;
  transition: box-shadow .15s;
}}
.post-item:hover {{ box-shadow: 0 2px 12px rgba(0,0,0,.12); }}
.post-item.today   {{ border-left-color: #f59e0b; background: #fffef5; }}
.post-item.updated {{ border-left-color: #22c55e; background: #f0fff4; }}
.post-row {{ display: flex; align-items: flex-start; padding: 12px 16px; gap: 14px; }}
.post-left {{ flex: 1; display: flex; flex-direction: column; gap: 4px; min-width: 0; }}
.title-row {{ display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }}
.badge {{ display: inline-block; padding: 1px 6px; border-radius: 8px; font-size: .64rem; font-weight: 700; flex-shrink: 0; }}
.badge-new {{ background: #fef3c7; color: #d97706; }}
.badge-upd {{ background: #dcfce7; color: #16a34a; }}
.post-title {{ font-size: .92rem; font-weight: 600; color: #1a1a2e; text-decoration: none; line-height: 1.4; }}
.post-title:hover {{ color: #4a7fd4; text-decoration: underline; }}
.post-meta {{ color: #999; font-size: .70rem; }}
.post-preview {{ color: #666; font-size: .80rem; line-height: 1.5; white-space: pre-wrap; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
.post-right {{ display: flex; flex-direction: column; align-items: flex-end; gap: 6px; flex-shrink: 0; }}
.comment-btn {{
  background: #4a7fd4; color: #fff; border: none; border-radius: 6px;
  padding: 5px 12px; font-size: .73rem; cursor: pointer; font-family: inherit;
  white-space: nowrap; transition: background .15s;
}}
.comment-btn:hover {{ background: #3568b5; }}
.comment-btn.open  {{ background: #64748b; }}
.view-btn {{ color: #aaa; font-size: .71rem; text-decoration: none; white-space: nowrap; }}
.view-btn:hover {{ color: #4a7fd4; }}
.comment-drawer {{ border-top: 1px solid #eef1f6; padding: 12px 16px 14px; background: #f9fafb; }}
.comment-section {{ display: flex; flex-direction: column; gap: 8px; }}
.comment {{ background: #fff; border-radius: 8px; padding: 9px 12px; font-size: .80rem; }}
.comment-meta {{ color: #aaa; font-size: .70rem; margin-bottom: 3px; }}
.comment-nick {{ color: #4a7fd4; font-weight: 600; }}
.comment-body {{ color: #444; line-height: 1.55; white-space: pre-wrap; }}
.orig-sub-section {{ padding-bottom: 8px; margin-bottom: 6px; border-bottom: 1px dashed #d0d8e8; }}
.orig-sub-title {{ font-size: .70rem; font-weight: 700; color: #7a9fd4; margin-bottom: 5px; }}
.subreply {{ margin-top: 5px; padding: 5px 10px; background: #edf2fb; border-left: 3px solid #4a7fd4; font-size: .77rem; border-radius: 0 6px 6px 0; }}
.subreply-nick {{ color: #5a7fc4; font-weight: 600; }}
.subreply-date {{ color: #bbb; font-size: .68rem; }}
.subreply-body {{ color: #555; }}
.empty {{ text-align: center; color: #999; padding: 80px; }}
</style>
</head>
<body>
<div class="page-header">
  <div>
    <h1>&#9876; 리니지W 대만 커뮤니티 모니터링</h1>
    <div class="info">forum.gamer.com.tw &middot; BSN 71905 &middot; 이번 달 {len(display_posts)}개 게시글 &middot; 업데이트: {now_str}</div>
  </div>
</div>
<div class="post-list">
{items_html if items_html.strip() else '  <div class="empty">수집된 게시글이 없습니다.</div>'}
</div>
<script>
var CMT={cmt_json};
function toggleDrw(btn) {{
  var item = btn.closest('.post-item');
  var drawer = item.querySelector('.comment-drawer');
  if (!drawer) return;
  if (!drawer.dataset.loaded) {{
    drawer.innerHTML = CMT[+item.dataset.idx] || '';
    drawer.dataset.loaded = '1';
  }}
  var hidden = drawer.hidden;
  drawer.hidden = !hidden;
  if (!hidden) {{ btn.textContent = btn.dataset.label; btn.classList.remove('open'); }}
  else         {{ btn.textContent = '✕ 닫기';          btn.classList.add('open');    }}
}}
</script>
</body>
</html>"""

    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"[완료] 대시보드 생성: {OUTPUT_HTML}")


# ── 메인 실행 ───────────────────────────────────────────────────────────────────

# ── 메인 실행 ───────────────────────────────────────────────────────────────────
def _translate_post(post: dict, page_data: dict, terms: dict) -> dict:
    """원글+댓글 번역을 post dict에 반영하고 반환."""
    orig_subs = page_data.get("orig_sub_replies", [])
    post["orig_sub_replies_kr"] = [
        {"author": sr["author"], "date": sr["date"],
         "content": translate(sr["content"], terms)}
        for sr in orig_subs
    ]
    translated_comments = []
    for j, comment in enumerate(page_data["comments"], 1):
        print(f"         댓글 {j}/{len(page_data['comments'])} 번역 중...")
        comment["content_kr"] = translate(comment["content"], terms)
        comment["sub_replies_kr"] = [
            {"author": sr["author"], "date": sr["date"],
             "content": translate(sr["content"], terms)}
            for sr in comment.get("sub_replies", [])
        ]
        translated_comments.append(comment)
    post["comments"] = translated_comments
    return post


def run(target_date: str | None = None, from_date: str | None = None):
    # 날짜 필터 미지정 시 이번 달 1일 이후만 수집
    if not target_date and not from_date:
        from_date = datetime.now().strftime("%m-01")

    print(f"\n{'=' * 55}")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 크롤링 시작")
    if from_date:
        print(f"날짜 필터: {from_date} 이후 전체")
    elif target_date:
        print(f"날짜 필터: {target_date}")

    terms    = load_terms()
    print(f"인게임 용어 사전: {len(terms)}개 로드")

    existing = load_posts()
    # snA 기반 중복 확인 (tnum 변경에도 동일 게시글 인식)
    def _sna(url: str) -> str:
        m = re.search(r'snA=(\d+)', url)
        return m.group(1) if m else url
    seen_sna = {_sna(p["url"]): p for p in existing}
    print(f"기존 저장 게시글: {len(existing)}개")

    raw_posts = fetch_board(target_date=target_date, from_date=from_date)
    print(f"목록 수집: {len(raw_posts)}개")

    new_posts: list[dict] = []
    updated_count = 0

    for idx, post in enumerate(raw_posts, 1):
        sna = _sna(post["url"])
        existing_post = seen_sna.get(sna)

        if existing_post:
            old_n = _parse_reply_count(existing_post.get("count", ""))
            new_n = _parse_reply_count(post.get("count", ""))
            if new_n > old_n:
                print(f"  [{idx}/{len(raw_posts)}] 댓글 업데이트 ({old_n}→{new_n}개): {post['title'][:40]}")
                page_data = fetch_post_page(post["url"])
                time.sleep(REQUEST_DELAY)
                _translate_post(existing_post, page_data, terms)
                existing_post["count"]      = post["count"]
                existing_post["url"]        = post["url"]
                existing_post["updated_at"] = datetime.now().isoformat()
                updated_count += 1
            else:
                print(f"  [{idx}/{len(raw_posts)}] 스킵 (변경없음): {post['title'][:40]}")
            continue

        print(f"  [{idx}/{len(raw_posts)}] 원글+댓글 수집: {post['title'][:40]}")
        page_data = fetch_post_page(post["url"])
        time.sleep(REQUEST_DELAY)

        print(f"         원글 번역 중...")
        post["title_kr"] = translate(post["title"], terms)
        post["body_kr"]  = translate(page_data["body"], terms)
        _translate_post(post, page_data, terms)
        post["crawled_at"] = datetime.now().isoformat()
        new_posts.append(post)

    all_posts = new_posts + existing
    save_posts(all_posts)
    generate_html(all_posts)

    print(f"\n신규 {len(new_posts)}개 추가 / 댓글 업데이트 {updated_count}개 / 전체 {len(all_posts)}개")
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--test" in args:
        run(target_date="05-03")
    elif "--from-date" in args:
        idx = args.index("--from-date")
        run(from_date=args[idx + 1] if idx + 1 < len(args) else None)
    elif "--date" in args:
        idx = args.index("--date")
        run(target_date=args[idx + 1] if idx + 1 < len(args) else None)
    else:
        run()
