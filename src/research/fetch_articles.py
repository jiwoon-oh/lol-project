"""S2. 원문 저장 — S1 index.csv 의 기사 본문을 받아 저장 (토큰 0).

수집 정책 (CLAUDE.md 1-1, common.PoliteFetcher)
- robots.txt 금지 경로는 받지 않음 → blocked.csv
- 요청 간격 1~2초, 403·429·430·보안 확인 화면이면 그 도메인 전체 중단 → blocked.csv
- 언론사 원문(originallink)을 먼저, 안 되면 같은 기사의 네이버 뉴스 주소를 시도 (둘 다 robots 확인)
- 둘 다 못 받으면 S1 제목·요약만 저장 (text_source: "summary")
출력: research/raw/articles/{player}/{id}.json

사용: python fetch_articles.py [--players Doran,Oner,Faker] [--force]
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter

import trafilatura

from common import ARTICLES, PoliteFetcher, get_logger, load_config, players, slug, targets

log = get_logger("S2")
MIN_TEXT = 200  # 이보다 짧으면 본문 추출 실패로 봄


def candidates(row: dict) -> list[str]:
    """시도할 주소 순서: 원문 → 네이버 뉴스 주소."""
    urls = [row["url"], row.get("naver_link")]
    return [u for u in dict.fromkeys(urls) if u]


def extract(html: str) -> tuple[str | None, str | None, str | None]:
    text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True)
    meta = trafilatura.extract_metadata(html)
    site = meta.sitename if meta else None
    title = meta.title if meta else None
    return (text if text and len(text) >= MIN_TEXT else None), site, title


def fetch_one(fetcher: PoliteFetcher, row: dict, markers: dict[str, list[str]]) -> dict:
    rec = {k: row[k] for k in ("id", "url", "title", "press", "published", "type", "query")}
    rec["source"] = row.get("source") or "news"
    rec["patch"] = row.get("patch")
    tried = []
    for url in candidates(row):
        html, why = fetcher.get(url)
        tried.append(f"{url} → {why}")
        if html is None:
            continue
        text, site, title = extract(html)
        if text:
            rec.update(text=text, text_source="full", fetched_url=url)
            if site:
                rec["press"] = site
            # 네이버 API 제목은 잘려 있을 수 있음 → 원문 전체 제목으로 유형 다시 판정
            if title and len(title) > len(row["title"].rstrip(".…")):
                rec["title_full"] = title
                if rec["source"] in markers and any(k in title for k in markers[rec["source"]]):
                    rec["type"] = "interview_en" if rec["source"] == "english" else "interview"
            break
        tried[-1] += " (본문 추출 실패)"
    else:
        rec.update(text=f"{row['title']}\n{row['description']}", text_source="summary", fetched_url=None)
    rec["fetch_log"] = tried
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", default=None)
    ap.add_argument("--force", action="store_true", help="이미 받은 기사도 다시 받기")
    args = ap.parse_args()
    fetcher = PoliteFetcher()
    q = load_config("queries")
    markers = {"news": q["interview_markers"], "english": q["english"]["interview_markers"]}
    for pid in (args.players.split(",") if args.players else list(targets())):
        d = ARTICLES / slug(pid)
        idx = d / "index.csv"
        if not idx.exists():
            log.warning("%s: %s 없음 — S1 먼저 실행", pid, idx)
            continue
        cnt = Counter()
        with open(idx, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                out = d / f"{row['id']}.json"
                if out.exists() and not args.force:
                    cnt[json.loads(out.read_text(encoding="utf-8"))["text_source"]] += 1
                    continue
                rec = fetch_one(fetcher, row, markers)
                out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
                cnt[rec["text_source"]] += 1
        log.info("%s: 본문 %d, 요약만 %d", pid, cnt["full"], cnt["summary"])
    if fetcher.blocked_domains:
        log.info("중단한 도메인 %d개: %s", len(fetcher.blocked_domains), ", ".join(sorted(fetcher.blocked_domains)))


if __name__ == "__main__":
    main()
