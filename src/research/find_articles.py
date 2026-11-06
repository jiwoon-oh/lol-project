"""S1. 기사·인터뷰·영어 매체 찾기 — 검색 API·RSS를 코드로 호출. LLM 사용 안 함 (재현성, 토큰 0).

출처 (research/config/queries.yaml)
  news     네이버 검색 API(뉴스)   → type article / interview
  (영어 매체는 collect_english.py — 사이트맵 순회. 검색 RSS 경로는 폐기)
공통
- 기간: 리서치 패치 창 (tournament_patches.csv, 26.16 검증 패치·비시즌·MSI·월즈 제외), 각 기사에 patch 칸, 제목에 선수 별칭이 있는 것만, 제목+요약에 LoL 문맥어
- 중복 제거: 같은 URL, 제목 유사도 title_dup_threshold 이상
- 선택: 출처별 상한까지, 인터뷰 우선(뉴스·영어), 나머지는 패치별로 고르게
출력: research/raw/articles/{player}/index.csv (+ candidates.csv: 거르기 전 전체)

사용: python find_articles.py [--players Doran,Oner,Faker] [--sources news]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import random
import re
import time
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from rapidfuzz import fuzz

from common import ARTICLES, AliasMatcher, env, get_logger, load_config, period, players, slug, targets, write_csv
from patch_windows import patch_of, windows

log = get_logger("S1")
NAVER = "https://openapi.naver.com/v1/search/{}.json"
FIELDS = ["id", "source", "url", "naver_link", "title", "press", "published", "patch", "query", "type", "description"]
_TAG = re.compile(r"<[^>]+>")


def clean(s: str) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


def uid(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def naver_search(session: requests.Session, kind: str, query: str, sort: str, display: int, start: int) -> list[dict]:
    for attempt in range(3):
        r = session.get(NAVER.format(kind), params={"query": query, "display": display, "start": start, "sort": sort},
                        timeout=10)
        if r.status_code == 429:  # API 초당 한도 → 잠깐 쉬고 재시도 (API 자체 한도라 우회 아님)
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json().get("items", [])
    raise RuntimeError(f"naver api 429 반복: {query}")


def fmt(t: str, pid: str, pcfg: dict, **kw) -> str:
    return t.format(nick=pcfg["name_ko"], real=pcfg["real_name"], team=pcfg["team"], en=pid, **kw)


# ---------- 뉴스 ----------

def collect_news(pid: str, pcfg: dict, qcfg: dict, session, d_from: date, d_to: date) -> list[dict]:
    matcher = AliasMatcher(pcfg)
    if pcfg.get("kind") == "team":  # 팀: 밴픽·운영·조합·코치진 검색어
        qs = [fmt(t, pid, pcfg) for t in qcfg["team_templates"]]
    else:
        qs = [fmt(t, pid, pcfg) for t in qcfg["templates"]]
        qs += [fmt(qcfg["issue_template"], pid, pcfg, issue=i) for i in qcfg["issues"]]
    qs = [q for q in dict.fromkeys(qs) if q.strip()]  # 본명 없는 선수 등 빈 검색어 제외
    out, seen, n_noctx = [], set(), 0
    for q in qs:
        # sort=sim 은 기간 전체에서 관련도순, sort=date 는 최신순 → 둘 다 받아 한쪽 쏠림을 줄임
        for sort in ("sim", "date"):
            for page in range(qcfg["max_pages"]):
                items = naver_search(session, "news", q, sort, qcfg["display"], 1 + page * qcfg["display"])
                for it in items:
                    pub = parsedate_to_datetime(it["pubDate"]).date()
                    url = it.get("originallink") or it["link"]
                    title = clean(it["title"])
                    if not d_from <= pub <= d_to or url in seen or not matcher.found(title):
                        continue
                    if not any(w in title + " " + clean(it["description"]) for w in qcfg["context_words"]):
                        n_noctx += 1
                        continue
                    seen.add(url)
                    out.append({
                        "id": uid(url), "source": "news", "url": url, "naver_link": it["link"], "title": title,
                        "press": urlparse(url).netloc.removeprefix("www."),  # S2에서 사이트명으로 갱신
                        "published": pub.isoformat(), "query": q,
                        "type": "interview" if any(k in title for k in qcfg["interview_markers"]) else "article",
                        "description": clean(it["description"]),
                    })
                if len(items) < qcfg["display"]:
                    break
                time.sleep(0.2)
    log.info("%s 뉴스: 후보 %d (문맥 없어 제외 %d)", pid, len(out), n_noctx)
    return out


# ---------- 선택 ----------

def dedupe_titles(rows: list[dict], thr: int) -> list[dict]:
    keep: list[dict] = []
    for r in sorted(rows, key=lambda r: ("interview" not in r["type"], r["published"])):
        if all(fuzz.ratio(r["title"], k["title"]) < thr for k in keep):
            keep.append(r)
    return keep


def select(rows: list[dict], n: int, rng: random.Random) -> list[dict]:
    """인터뷰를 먼저 (최대 절반), 남은 자리는 나머지로 월별 고르게."""
    from sample_community import stratified
    inter = [r for r in rows if "interview" in r["type"]]
    arts = [r for r in rows if "interview" not in r["type"]]
    picked = stratified(inter, min(len(inter), n // 2), rng, date_key="published", key=lambda r: r["patch"])
    picked += stratified(arts, n - len(picked), rng, date_key="published", key=lambda r: r["patch"])
    if len(picked) < n:  # 기사가 모자라면 남은 인터뷰로 채움
        rest = [r for r in inter if r not in picked]
        picked += stratified(rest, n - len(picked), rng, date_key="published", key=lambda r: r["patch"])
    return sorted(picked, key=lambda r: r["published"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", default=None)
    ap.add_argument("--sources", default="news")
    args = ap.parse_args()
    sources = set(args.sources.split(","))
    qcfg = load_config("queries")
    cid, secret = env("NAVER_CLIENT_ID"), env("NAVER_CLIENT_SECRET")
    if "news" in sources and not (cid and secret):
        raise SystemExit(".env 에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 이 필요합니다 (.env.example 참고)")
    d_from, d_to = (date.fromisoformat(d) for d in period())
    allp = targets()
    s = requests.Session()
    s.headers.update({"X-Naver-Client-Id": cid or "", "X-Naver-Client-Secret": secret or ""})
    caps = {"news": qcfg["max_articles_per_player"]}

    for pid in (args.players.split(",") if args.players else list(allp)):
        pcfg = allp[pid]
        found = {}
        if "news" in sources:
            found["news"] = collect_news(pid, pcfg, qcfg, s, d_from, d_to)
        cands, picked = [], []
        wins = windows("research")
        for src, rows in found.items():
            for r in rows:
                r["patch"] = patch_of(r["published"], wins)
            rows = [r for r in rows if r["patch"]]  # 리서치 패치 창 밖(비시즌·MSI·26.16 이후) 제외
            rows = dedupe_titles(rows, qcfg["title_dup_threshold"])
            cands += rows
            sel = select(rows, caps[src], random.Random(f"s1-{pid}-{src}"))
            picked += sel
            n_int = sum("interview" in r["type"] for r in sel)
            log.info("%s %s: 제목 중복 제거 후 %d → 선택 %d (인터뷰 %d)", pid, src, len(rows), len(sel), n_int)
        d = ARTICLES / slug(pid)
        write_csv(d / "candidates.csv", cands, FIELDS)
        write_csv(d / "index.csv", sorted(picked, key=lambda r: r["published"]), FIELDS)
    log.info("완료 %s", datetime.now().isoformat(timespec="seconds"))


if __name__ == "__main__":
    main()
