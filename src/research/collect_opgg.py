"""S3-b. OP.GG 톡 e스포츠 게시판 수집 (https://talk.op.gg/s/lol/esports) — 코드만, 토큰 0.

robots.txt (2026-11-03 확인): 콘텐츠·목록 허용, /api/ 와 검색(?q= ?search=) 금지
  → 사이트 검색을 쓰지 않고, 목록 페이지(?page=N)를 넘기며 글 주소의 제목(slug)으로 선수 글을 고름
  → 고른 글만 본문을 받음. 모든 요청은 PoliteFetcher (robots·간격 1~2초·차단 시 중단)

단계
  discover  목록 페이지를 최신부터 넘김. 글 id·제목·날짜를 모으고, 날짜가 기간 시작보다 오래되면 멈춤
            → research/raw/opgg/list.csv
  fetch     제목에 선수 별칭이 있고 리서치 패치 창 안인 글만, 선수·패치당 fetch_per_patch 개까지 본문 받기
            → research/raw/opgg/posts.jsonl  {post_id, url, title, body, date, players}
  표본은 sample_community.py 가 posts.jsonl 에서 패치별로 뽑음 (source "opgg")

⚠ 페이지 구조(목록의 날짜 위치, 본문 위치)는 실행 1단계 시험에서 확인. 아래 파서는 Next.js 데이터(__NEXT_DATA__)를
  먼저 보고, 없으면 HTML 링크·메타 태그로 대신함.

사용
  python collect_opgg.py discover [--max-pages 50] [--restart]   # 끊기면 다시 실행 = 이어하기
  (fetch 도 끊기면 다시 실행 = 이미 받은 글 건너뜀)
  python collect_opgg.py fetch [--players Doran,Oner,Faker]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import defaultdict
from urllib.parse import unquote

from common import RESEARCH, AliasMatcher, PoliteFetcher, get_logger, load_config, players, read_jsonl, write_csv, targets
from patch_windows import patch_of, windows

log = get_logger("opgg")
BASE = "https://talk.op.gg"
BOARD = "/s/lol/esports"
OUT = RESEARCH / "raw" / "opgg"
_LINK = re.compile(r'href="(/s/lol/esports/(\d+)/([^"?]*))')
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_DATE = re.compile(r"(20\d\d-\d\d-\d\d)")
DATE_KEYS = ("created_at", "createdAt", "published_at", "publishedAt", "written_at", "date")
BODY_KEYS = ("content", "contents", "body", "text")


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def next_data(html: str) -> dict | None:
    m = _NEXT.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def first_date(d: dict) -> str | None:
    for k in DATE_KEYS:
        v = d.get(k)
        if isinstance(v, str) and _DATE.search(v):
            return _DATE.search(v).group(1)  # ⚠ UTC 여부는 시험에서 확인
    return None


def parse_list(html: str) -> list[dict]:
    """목록 페이지 → [{post_id, title, date, url}]. 날짜를 못 찾으면 date=None."""
    rows = {}
    nd = next_data(html)
    if nd:
        for d in walk(nd):
            pid, title = d.get("id"), d.get("title")
            if isinstance(pid, int) and isinstance(title, str):
                rows[str(pid)] = {"post_id": str(pid), "title": title, "date": first_date(d), "url": None}
    for path, pid, slug in _LINK.findall(html):
        r = rows.setdefault(pid, {"post_id": pid, "title": unquote(slug).replace("-", " "), "date": None, "url": None})
        r["url"] = BASE + path
    return [r for r in rows.values() if r["url"]]


def parse_post(html: str) -> tuple[str | None, str | None, str | None]:
    """글 페이지 → (제목, 본문, 날짜)."""
    nd = next_data(html)
    if nd:
        best = None
        for d in walk(nd):
            if isinstance(d.get("title"), str) and any(isinstance(d.get(k), str) for k in BODY_KEYS):
                best = d
                break
        if best:
            body = next(best[k] for k in BODY_KEYS if isinstance(best.get(k), str))
            body = re.sub(r"<[^>]+>", " ", body)
            return best["title"], re.sub(r"\s+", " ", body).strip(), first_date(best)
    import trafilatura
    meta = trafilatura.extract_metadata(html)
    text = trafilatura.extract(html, include_comments=False)
    return (meta.title if meta else None), text, (meta.date if meta and meta.date else None)


LIST_FIELDS = ["post_id", "date", "title", "url"]
STATE = OUT / "state.json"


def cmd_discover(max_pages: int, d_from: str, restart: bool):
    """중단돼도 이어서: 20쪽마다 list.csv·state.json 저장, 다시 실행하면 마지막 쪽 다음부터."""
    f = PoliteFetcher()
    OUT.mkdir(parents=True, exist_ok=True)
    rows, start = [], 1
    if not restart and STATE.exists() and (OUT / "list.csv").exists():
        st = json.loads(STATE.read_text(encoding="utf-8"))
        if st.get("finished"):
            log.info("이미 끝남 (%s) — 처음부터 하려면 --restart", st)
            return
        with open(OUT / "list.csv", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        start = st["last_page"] + 1
        log.info("이어하기: %d쪽부터 (글 %d개 있음)", start, len(rows))
    seen, no_date_pages = {r["post_id"] for r in rows}, 0

    def save(page: int, finished: bool):
        write_csv(OUT / "list.csv", rows, LIST_FIELDS)
        STATE.write_text(json.dumps({"last_page": page, "finished": finished, "n": len(rows)}), encoding="utf-8")

    page = start - 1
    finished = False
    for page in range(start, max_pages + 1):
        html, why = f.get(f"{BASE}{BOARD}?page={page}")
        if html is None:
            log.warning("목록 %d쪽 못 받음 (%s) — 중단", page, why)
            break
        got = [r for r in parse_list(html) if r["post_id"] not in seen]
        if not got:
            log.info("%d쪽: 새 글 없음 — 중단", page)
            finished = True
            break
        for r in got:
            seen.add(r["post_id"])
            rows.append(r)
        dates = [r["date"] for r in got if r["date"]]
        if not dates:
            no_date_pages += 1
        elif max(dates) < d_from:  # 쪽마다 오래된 고정 공지(2023년)가 끼어 있어 min 이 아니라 max 로 판단
            log.info("%d쪽: %s 까지 내려옴 (기간 시작 %s 이전) — 중단", page, max(dates), d_from)
            finished = True
            break
        if page % 20 == 0:
            save(page, False)
            log.info("%d쪽, 글 %d개, 이 쪽 최근 날짜 %s", page, len(rows), max(dates) if dates else "?")
    save(page, finished)
    log.info("목록 %d개 → %s (날짜 없는 쪽 %d)", len(rows), OUT / "list.csv", no_date_pages)
    if no_date_pages:
        log.warning("목록에서 날짜를 못 찾은 쪽이 있음 → 파서 확인 필요 (fetch 단계에서 글 페이지 날짜로 거름)")


def cmd_fetch(names: list[str], per_patch: int, seed: int):
    with open(OUT / "list.csv", encoding="utf-8-sig") as fh:
        lst = list(csv.DictReader(fh))
    allp = targets()
    wins = windows("research")
    f = PoliteFetcher()
    want: dict[str, dict] = {}
    for pid in names:
        m = AliasMatcher(allp[pid])
        by_patch = defaultdict(list)
        for r in lst:
            if not m.found(r["title"]):
                continue
            p = patch_of(r["date"], wins) if r["date"] else "?"  # 날짜 모르면 일단 받고 나중에 거름
            if p:
                by_patch[p].append(r)
        rng = random.Random(f"{seed}-{pid}")
        for p, rs in by_patch.items():
            rng.shuffle(rs)
            for r in rs[: per_patch if p != "?" else per_patch * 3]:
                want.setdefault(r["post_id"], {**r, "players": []})["players"].append(pid)
        log.info("%s: 제목 일치 %d개 → 받을 글 %d개", pid, sum(len(v) for v in by_patch.values()),
                 sum(min(len(v), per_patch) for v in by_patch.values()))
    done = {r["post_id"] for r in read_jsonl(OUT / "posts.jsonl")} if (OUT / "posts.jsonl").exists() else set()
    if done:
        log.info("이어하기: 이미 받은 글 %d개 건너뜀", len(done & set(want)))
    n_new = 0
    for r in want.values():
        if r["post_id"] in done:
            continue
        html, why = f.get(r["url"])
        if html is None:
            continue
        title, body, date = parse_post(html)
        date = date or r["date"]
        if not date or not patch_of(date, wins):
            continue
        with open(OUT / "posts.jsonl", "a", encoding="utf-8") as fh:  # 한 글씩 바로 덧붙임 → 끊겨도 받은 만큼 남음
            fh.write(json.dumps({"post_id": r["post_id"], "url": r["url"], "title": title or r["title"],
                                 "body": body or "", "date": date, "players": r["players"]}, ensure_ascii=False) + "\n")
        n_new += 1
    log.info("본문 새로 %d개 저장 → %s (대상 %d)", n_new, OUT / "posts.jsonl", len(want))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--max-pages", type=int, default=None)
    d.add_argument("--restart", action="store_true", help="이어하지 않고 1쪽부터")
    fp = sub.add_parser("fetch")
    fp.add_argument("--players", default=None)
    a = ap.parse_args()
    cfg = load_config("sampling")["opgg"]
    if a.cmd == "discover":
        cmd_discover(a.max_pages or cfg["max_list_pages"], windows("research")[0][1], a.restart)
    else:
        cmd_fetch(a.players.split(",") if a.players else list(targets()), cfg["fetch_per_patch"],
                  load_config("sampling")["seed"])


if __name__ == "__main__":
    main()
