"""S1-c. 영어 매체 기사 찾기 — 검색 대신 사이트 사이트맵을 순회해 URL 슬러그로 선수·팀 매칭 (코드만, 토큰 0).

흐름
  discover  robots.txt 의 Sitemap → 사이트맵 인덱스 → 하위 사이트맵(2026 가능성 있는 것만, 사이트당 상한)
            → <loc>·날짜 수집 → 슬러그에 선수 영문 ID / 팀 별칭이 낱말로 들어간 것만 후보로 저장
            → research/raw/english/candidates.csv (이어받기: done_sitemaps.txt 에 있는 하위 사이트맵은 건너뜀)
  select    후보에서 대상(선수·팀)별 max_per_player 개 선택(인터뷰 우선, 패치별로 고르게)
            → research/raw/articles/{대상}/index.csv 에 source="english" 행으로 반영 (영어 아닌 기존 행은 그대로)

정책 (CLAUDE.md 1-1)
- 모든 요청은 PoliteFetcher (robots.txt·도메인별 1~2초 간격·403/429/430/캡차 시 그 도메인 중단 + blocked.csv)
- gzip 사이트맵(.xml.gz)은 같은 정책 검사를 거치는 get_bytes 로 받음 (본문이 바이너리라 text 로 못 받기 때문)
- 제목은 슬러그에서 만든 임시값. S2 가 실제 제목으로 교체
- 날짜는 사이트맵 날짜(news:publication_date 우선, 없으면 lastmod). 리서치 패치 창 밖은 제외

사용: python collect_english.py discover [--max-child 2] [--sites www.invenglobal.com,...]
      python collect_english.py select [--targets Faker,Oner,T1]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import random
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse

import requests

from common import (ARTICLES, RESEARCH, PoliteFetcher, get_logger, load_config, log_blocked, players, write_csv,
                    BLOCK_STATUS)
from find_articles import FIELDS, select as pick, uid
from patch_windows import patch_of, windows
from team_stats import slug as team_slug

log = get_logger("S1c")
OUT_DIR = RESEARCH / "raw" / "english"
CAND_CSV = OUT_DIR / "candidates.csv"
DONE_TXT = OUT_DIR / "done_sitemaps.txt"
CAND_FIELDS = FIELDS + ["target", "kind"]  # kind: player / team
YEAR = re.compile(r"(?<!\d)(20\d\d)(?!\d)")


# ---------- 요청 ----------

class SitemapFetcher(PoliteFetcher):
    """PoliteFetcher 의 정책(robots·간격·차단 중단)을 그대로 쓰되 바이트로 받는 변형 (gz 사이트맵용)."""

    def get_bytes(self, url: str) -> tuple[bytes | None, str]:
        domain = urlparse(url).netloc
        if domain in self.blocked_domains:
            return None, "domain_blocked"
        rp = self._robots(url)
        if rp is None:
            self.blocked_domains.add(domain)
            log_blocked(url, "robots_http_blocked")
            return None, "domain_blocked"
        if not rp.can_fetch(url):
            log_blocked(url, "robots_disallow")
            return None, "robots_disallow"
        self._wait(domain)
        try:
            r = self.s.get(url, timeout=60)
        except requests.RequestException as e:
            log_blocked(url, "error", type(e).__name__)
            return None, "error"
        if r.status_code in BLOCK_STATUS:
            self.blocked_domains.add(domain)
            log_blocked(url, f"http_{r.status_code}", "도메인 전체 중단")
            return None, f"http_{r.status_code}"
        if r.status_code != 200:
            log_blocked(url, f"http_{r.status_code}")
            return None, f"http_{r.status_code}"
        return r.content, "ok"

    def robots_ok(self, url: str) -> bool | None:
        rp = self._robots(url)
        return None if rp is None else rp.can_fetch(url)

    def sitemap_lines(self, site: str) -> list[str]:
        """robots.txt 의 Sitemap: 줄 (Robots 클래스는 이 줄을 저장하지 않아 robots.txt 만 한 번 더 읽음)."""
        try:
            r = self.s.get(f"https://{site}/robots.txt", timeout=10)
        except requests.RequestException:
            return []
        if r.status_code != 200:
            return []
        return [ln.split(":", 1)[1].strip() for ln in r.text.splitlines() if ln.lower().startswith("sitemap:")]


def fetch_xml(f: SitemapFetcher, url: str) -> bytes | None:
    if url.endswith(".gz"):
        raw, why = f.get_bytes(url)
        if raw is None:
            log.warning("사이트맵 못 받음 %s (%s)", url, why)
            return None
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    txt, why = f.get(url)
    if txt is None:
        log.warning("사이트맵 못 받음 %s (%s)", url, why)
        return None
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", txt.lstrip("﻿")).encode("utf-8")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(data: bytes):
    """('index', [(loc, lastmod)]) 또는 ('urlset', [(loc, date, title)]). 형식 오류면 (None, [])."""
    kind, rows = None, []
    try:
        for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
            t = local(el.tag)
            if t in ("sitemap", "url"):
                d = {local(c.tag): (c.text or "").strip() for c in el}
                loc = d.get("loc", "")
                if t == "sitemap":
                    kind = "index"
                    rows.append((loc, d.get("lastmod", "")))
                else:
                    kind = "urlset"
                    rows.append((loc, (d.get("publication_date") or d.get("lastmod") or "")[:10], d.get("title", "")))
                el.clear()
    except ET.ParseError as e:
        log.warning("사이트맵 형식 오류: %s", e)
        if not rows:
            return None, []
    return kind, rows


def likely_2026(loc: str, lastmod: str) -> bool:
    if lastmod and lastmod[:4].isdigit() and lastmod[:10] < "2026-01-01":
        return False
    years = {int(y) for y in YEAR.findall(loc)}
    return not years or 2026 in years or max(years) > 2026


def beyond(loc: str, last_day: str) -> bool:
    """사이트맵 주소에 든 연·월(2026-10, year=2026&month=10, year=2026&quarter=4)이 패치 창 끝(last_day)보다 뒤면 True."""
    m = re.search(r"(20\d\d)[-/_]?(0[1-9]|1[0-2])(?:[-/_]?\d\d)?(?!\d)", loc)
    if m:
        y, mo = m.group(1), int(m.group(2))
    else:
        y_ = re.search(r"year=(20\d\d)", loc)
        if not y_:
            return False
        y = y_.group(1)
        mm, q = re.search(r"month=(\d+)", loc), re.search(r"quarter=(\d)", loc)
        mo = int(mm.group(1)) if mm else (int(q.group(1)) * 3 - 2 if q else 1)  # 분기는 첫 달 기준
    return f"{y}-{mo:02d}" > last_day[:7]


# ---------- 슬러그 매칭 ----------

def slug_tokens(url: str) -> str:
    """URL 마지막 경로 조각을 소문자 낱말로 (숫자뿐이면 경로 전체). ' a b c ' 형태 (앞뒤 공백)."""
    path = unquote(urlparse(url).path).rstrip("/")
    last = path.rsplit("/", 1)[-1]
    if not re.search(r"[A-Za-z]", last):
        last = path
    last = re.sub(r"\.(html?|php|aspx?)$", "", last, flags=re.I)
    return " " + " ".join(re.findall(r"[a-z0-9]+", last.lower())) + " "


def phrase(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


class Matcher:
    def __init__(self, ecfg: dict):
        self.ecfg = ecfg
        pl = players()
        common = {phrase(x) for x in ecfg["common_ids"]}
        self.players = {pid: (phrase(pid), phrase(pid) in common or bool(c.get("ambiguous"))) for pid, c in pl.items()}
        teams = load_config("teams")["teams"]
        weak = {phrase(x) for x in ecfg["weak_team_aliases"]}
        self.teams: dict[str, list[tuple[str, bool]]] = {}
        for tk, tc in teams.items():
            al = {phrase(tk)}
            al |= {phrase(a) for a in tc["aliases"] if a.isascii()}
            al |= {phrase(a) for a in ecfg["team_extra_aliases"].get(tk, [])}
            self.teams[tk] = [(a, a in weak) for a in sorted(al) if a]
        self.team_phrases = sorted({a for v in self.teams.values() for a, _ in v if " " in a}, key=len, reverse=True)
        self.ctx_base = [phrase(w) for w in ecfg["slug_context_words"]]
        self.team_all = [a for v in self.teams.values() for a, w in v if not w]
        self.excl = [phrase(w) for w in ecfg.get("slug_exclude_words", [])]
        self.iv = [phrase(w) for w in ecfg["slug_interview_words"]]

    @staticmethod
    def has(s: str, ph: str) -> bool:
        return f" {ph} " in s

    def has_ctx(self, s: str) -> bool:
        return any(self.has(s, w) for w in self.ctx_base + self.team_all)

    def has_base_ctx(self, s: str) -> bool:
        return any(self.has(s, w) for w in self.ctx_base)

    def match(self, url: str) -> tuple[list[str], list[str]]:
        s = slug_tokens(url)
        if any(self.has(s, w) for w in self.excl):
            return [], []
        sp = s  # 여러 낱말 팀 이름 속의 낱말은 선수로 보지 않음 (hanwha-life-esports 의 'Life')
        for ph in self.team_phrases:
            sp = sp.replace(f" {ph} ", " ")
        ps = [pid for pid, (ph, need) in self.players.items() if self.has(sp, ph) and (not need or self.has_ctx(s))]
        ts = []
        for tk, als in self.teams.items():
            for a, weak in als:
                if self.has(s, a) and (not weak or self.has_base_ctx(s)):
                    ts.append(tk)
                    break
        return ps, ts

    def is_interview(self, url: str) -> bool:
        s = slug_tokens(url)
        return any(self.has(s, w) for w in self.iv)


# ---------- discover ----------

def read_candidates() -> list[dict]:
    if not CAND_CSV.exists():
        return []
    with open(CAND_CSV, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def discover(args):
    qcfg = load_config("queries")
    ecfg = qcfg["english"]
    cap = args.max_child if args.max_child is not None else ecfg["max_child_sitemaps"]
    sites = args.sites.split(",") if args.sites else ecfg["sitemap_sites"]
    m = Matcher(ecfg)
    wins = windows("research")
    last_day = max(w[2] for w in wins)
    f = SitemapFetcher()
    done = set(DONE_TXT.read_text(encoding="utf-8").split()) if DONE_TXT.exists() else set()
    cands = read_candidates()
    have = {(r["target"], r["url"]) for r in cands}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {}

    for site in sites:
        st = {"robots": None, "children_found": 0, "children_run": 0, "skipped_year": 0, "skipped_cap": 0,
              "urls_2026": 0, "urls_window": 0, "matched": {}}
        report[site] = st
        base = f"https://{site}/"
        st["robots"] = f.robots_ok(base)
        if not st["robots"]:
            log.warning("%s: robots.txt 로 / 접근 불가 또는 확인 실패 — 건너뜀", site)
            continue
        roots = f.sitemap_lines(site)
        if not roots:
            log.warning("%s: robots.txt 에 Sitemap 없음", site)
            continue
        # robots.txt 의 사이트맵 → 인덱스면 하위 사이트맵 목록, urlset 이면 그 자체가 하위 사이트맵
        leaves: dict[str, tuple[str, list | None]] = {}  # loc -> (lastmod, 미리 받은 rows)
        queue = [(r, "", 0) for r in roots]
        while queue:
            url, lm, depth = queue.pop(0)
            if not likely_2026(url, lm):  # 받기 전에 연도로 거름 (예: koreaherald /sitemap/articles/2019)
                st["skipped_year"] += 1
                continue
            if url in leaves or url in done:
                if url in done:
                    leaves.setdefault(url, (lm, None))
                continue
            data = fetch_xml(f, url)
            if data is None:
                continue
            kind, rows = parse_sitemap(data)
            if kind == "index":
                # 하위는 받지 않고 후보로만 올림 (koreaherald 는 하위 300개 — 전부 받으면 수 분 걸림). 고른 것만 나중에 받음
                for loc, l in rows:
                    leaves.setdefault(loc, (l, None))
            elif kind == "urlset":
                leaves[url] = (lm, rows)
        st["children_found"] = len(leaves)
        keep = []
        for loc, (lm, rows) in leaves.items():
            if likely_2026(loc, lm):
                keep.append((loc, lm, rows))
            else:
                st["skipped_year"] += 1
        # 기사 사이트맵(post·article·news·lol…) 먼저, 패치 창이 끝난 뒤 기간은 뒤로, 그다음 최근 수정 순 (날짜 모르면 뒤)
        keep.sort(key=lambda x: x[1], reverse=True)
        keep.sort(key=lambda x: (beyond(x[0], last_day), not re.search(r"post|article|news|lol|league|esport", x[0], re.I)))
        todo = keep[:cap]
        st["skipped_cap"] = len(keep) - len(todo)
        if st["skipped_cap"]:
            log.info("%s: 상한 %d 때문에 건너뛴 하위 사이트맵 %d개: %s", site, cap, st["skipped_cap"],
                     ", ".join(x[0] for x in keep[cap:cap + 5]) + (" ..." if st["skipped_cap"] > 5 else ""))
        if st["skipped_year"]:
            log.info("%s: 2026 이전 것으로 보여 건너뜀 %d개", site, st["skipped_year"])

        for loc, lm, rows in todo:
            if loc in done:
                continue
            if rows is None:
                data = fetch_xml(f, loc)
                if data is None:
                    if site in f.blocked_domains:
                        break
                    continue
                kind, rows = parse_sitemap(data)
                if kind != "urlset":  # 인덱스 안의 인덱스: 건너뜀 (2단계 이상은 지원 안 함)
                    log.info("%s: urlset 아님 — 건너뜀", loc)
                    continue
            st["children_run"] += 1
            new = []
            for url, day, title in rows:
                if not day.startswith("2026"):
                    continue
                st["urls_2026"] += 1
                p = patch_of(day, wins)
                if not p:
                    continue
                st["urls_window"] += 1
                ps, ts = m.match(url)
                if not ps and not ts:
                    continue
                typ = "interview_en" if m.is_interview(url) else "article_en"
                t = title or slug_tokens(url).strip()
                for kind_, names in (("player", ps), ("team", ts)):
                    for name in names:
                        if (name, url) in have:
                            continue
                        have.add((name, url))
                        st["matched"][name] = st["matched"].get(name, 0) + 1
                        new.append({"id": uid(url), "source": "english", "url": url, "naver_link": "", "title": t,
                                    "press": urlparse(url).netloc.removeprefix("www."), "published": day, "patch": p,
                                    "query": f"sitemap:{site}", "type": typ, "description": "",
                                    "target": name, "kind": kind_})
            cands += new
            write_csv(CAND_CSV, cands, CAND_FIELDS)  # 하위 사이트맵마다 저장 → 중단돼도 이어받기
            done.add(loc)
            with open(DONE_TXT, "a", encoding="utf-8") as fh:
                fh.write(loc + "\n")
            log.info("%s %s: URL %d개 중 후보 +%d", site, loc, len(rows), len(new))
        if site in f.blocked_domains:
            log.warning("%s: 차단 신호로 중단 (blocked.csv 참고)", site)

    print("\n=== discover 요약 ===")
    for site, st in report.items():
        print(f"{site}: robots_allowed={st['robots']} 하위사이트맵 발견={st['children_found']} 실행={st['children_run']} "
              f"(연도로 제외 {st['skipped_year']}, 상한으로 제외 {st['skipped_cap']}) "
              f"2026 URL={st['urls_2026']} 패치창 안={st['urls_window']} 후보 대상별={st['matched']}")
    allc = read_candidates()
    print(f"candidates.csv 총 {len(allc)}행 → {CAND_CSV}")


# ---------- select ----------

def update_index(target: str, rows: list[dict]) -> int:
    path = ARTICLES / target / "index.csv"
    old, extra = [], []
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            extra = [c for c in (rd.fieldnames or []) if c not in FIELDS]
            old = list(rd)
    keep = [r for r in old if r.get("source") != "english"]
    if not path.exists() and not rows:
        return 0
    allr = keep + rows
    allr.sort(key=lambda r: r.get("published", ""))
    write_csv(path, allr, FIELDS + extra)
    return len(rows)


def select(args):
    ecfg = load_config("queries")["english"]
    n = ecfg["max_per_player"]
    cands = read_candidates()
    by: dict[str, list[dict]] = {}
    for r in cands:
        by.setdefault(team_slug(r["target"]) if r["kind"] == "team" else r["target"], []).append(r)
    all_targets = list(players()) + [team_slug(t) for t in load_config("teams")["teams"]]
    targets = args.targets.split(",") if args.targets else all_targets
    targets = [team_slug(t) if t in load_config("teams")["teams"] else t for t in targets]
    for tg in targets:
        seen, rows = set(), []
        for r in sorted(by.get(tg, []), key=lambda r: r["published"]):
            if r["url"] not in seen:
                seen.add(r["url"])
                rows.append(r)
        sel = pick(rows, n, random.Random(f"s1c-{tg}")) if rows else []
        c = update_index(tg, sel)
        if rows or c:
            log.info("%s 영어: 후보 %d → 선택 %d (인터뷰 %d)", tg, len(rows), len(sel),
                     sum("interview" in r["type"] for r in sel))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--max-child", type=int, default=None, help="사이트당 하위 사이트맵 상한 (기본: queries.yaml)")
    d.add_argument("--sites", default=None)
    s = sub.add_parser("select")
    s.add_argument("--targets", default=None, help="선수 ID 또는 팀 이름(콤마). 기본 전체")
    args = ap.parse_args()
    {"discover": discover, "select": select}[args.cmd](args)


if __name__ == "__main__":
    main()
