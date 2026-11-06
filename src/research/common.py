"""리서치 파이프라인 공통: 경로, 설정, 별칭 매칭, jsonl, 수집 정책(robots·간격·차단)."""
from __future__ import annotations

import csv
import json
import logging
import random
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
INVEN = DATA / "inven_2026"
YOUTUBE = DATA / "youtube_2026"
RESEARCH = ROOT / "research"
CONFIG = RESEARCH / "config"
ARTICLES = RESEARCH / "raw" / "articles"
BLOCKED_CSV = RESEARCH / "raw" / "blocked.csv"
SAMPLES = RESEARCH / "samples"
LOGS = RESEARCH / "logs"

UA = "lck-opinion-research/0.1 (personal research; contact via repo owner)"

for _s in (sys.stdout, sys.stderr):  # Windows 콘솔(cp949)에서 한글·이모지 깨짐 방지
    _s.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def load_config(name: str) -> dict:
    return yaml.safe_load((CONFIG / f"{name}.yaml").read_text(encoding="utf-8"))


def env(name: str) -> str | None:
    import os
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return os.getenv(name) or None


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


# ---------- 별칭 ----------

class AliasMatcher:
    """선수 별칭 매칭. exclude 패턴에 걸린 구간은 매치에서 뺀다."""

    def __init__(self, player_cfg: dict):
        self.aliases = [re.compile(p) for p in player_cfg["aliases"]]
        self.exclude = [re.compile(p) for p in player_cfg.get("exclude", [])]

    def spans(self, text: str) -> list[tuple[int, int]]:
        bad = [m.span() for p in self.exclude for m in p.finditer(text)]
        out = []
        for p in self.aliases:
            for m in p.finditer(text):
                s, e = m.span()
                if not any(bs <= s < be or bs < e <= be for bs, be in bad):
                    out.append((s, e))
        return sorted(out)

    def found(self, text: str) -> bool:
        return bool(text) and bool(self.spans(text))


def players() -> dict:
    return load_config("players")["players"]


def slug(name: str) -> str:
    """폴더·파일 이름용 (팀 이름의 공백·점 제거). 선수 ID 는 그대로."""
    return name.replace(" ", "_").replace(".", "")


def _team_alias_regex(a: str) -> str:
    # 영문 약칭(T1, KT, DK, DN, NS, GEN ...)은 다른 낱말 안에 들어가지 않게 앞뒤를 막음
    if re.fullmatch(r"[A-Za-z0-9.]+", a):
        return rf"(?i)(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])"
    return re.escape(a)


def targets(kind: str | None = None) -> dict:
    """리서치 대상 = 선수(players.yaml) + 팀(teams.yaml). 키 = 선수 ID 또는 OE 팀 이름.
    팀 설정은 선수 설정과 같은 칸(name_ko, aliases, exclude ...)으로 맞춰서 돌려줌."""
    out = {}
    if kind in (None, "player"):
        for k, v in players().items():
            out[k] = {**v, "kind": "player", "slug": slug(k)}
    if kind in (None, "team"):
        for k, v in load_config("teams")["teams"].items():
            out[k] = {"kind": "team", "slug": slug(k), "name_ko": v["name_ko"], "real_name": "", "team": k,
                      "position": "팀", "inven_search": None, "ambiguous": True,
                      "aliases": [_team_alias_regex(a) for a in v["aliases"]], "exclude": v.get("exclude", []),
                      "players": v.get("players", [])}
    return out


def period() -> tuple[str, str]:
    """조사 기간 (research/config/period.yaml, CLAUDE.md 0장). YYYY-MM-DD 문자열."""
    p = load_config("period")
    return str(p["date_from"]), str(p["date_to"])


# ---------- 문장 ----------

_SENT_SPLIT = re.compile(r"(?<=[.!?。…])\s+|(?<=다\.)|\n+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s and s.strip()]


# ---------- 입출력 ----------

def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def log_blocked(url: str, reason: str, detail: str = "") -> None:
    """막힌 사이트·URL 기록 (CLAUDE.md 1-1)."""
    BLOCKED_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not BLOCKED_CSV.exists()
    with open(BLOCKED_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "domain", "url", "reason", "detail"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), urlparse(url).netloc, url, reason, detail])


# ---------- 수집 정책 ----------

BLOCK_STATUS = {403, 429, 430}
CAPTCHA_HINTS = ("captcha", "보안 확인", "자동입력 방지", "are you a robot", "cf-challenge", "verify you are human")


def looks_like_challenge(html: str) -> bool:
    """보안 확인 화면인지. 일반 기사에도 recaptcha 스크립트가 있으므로
    제목에 신호가 있거나, 본문이 거의 없는데 신호가 있을 때만 차단으로 본다."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").lower() if soup.title else ""
    if any(h in title for h in CAPTCHA_HINTS):
        return True
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    return len(text) < 1500 and any(h in text.lower() for h in CAPTCHA_HINTS)


class Robots:
    """robots.txt 해석 (RFC 9309). 표준 라이브러리 RobotFileParser 는 `*`·`$` 패턴을 몰라서
    `Disallow: /*?q=` 같은 규칙을 놓침 → 직접 구현. 가장 긴 규칙이 이기고, 같은 길이면 Allow."""

    TOKEN = UA.split("/")[0].lower()

    def __init__(self, text: str):
        groups, agents, rules, last_agent = [], [], [], False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if not last_agent and agents:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(v.lower())
                last_agent = True
            elif k in ("allow", "disallow"):
                last_agent = False
                if v:
                    rules.append((k == "allow", v))
        if agents:
            groups.append((agents, rules))
        mine = [g for g in groups if any(a != "*" and a in self.TOKEN for a in g[0])]
        star = [g for g in groups if "*" in g[0]]
        chosen = mine or star
        self.rules = [(allow, pat, self._regex(pat)) for g in chosen for allow, pat in g[1]]

    @staticmethod
    def _regex(pat: str) -> re.Pattern:
        end = pat.endswith("$")
        body = re.escape(pat[:-1] if end else pat).replace(r"\*", ".*")
        return re.compile(body + ("$" if end else ""))

    def can_fetch(self, url: str) -> bool:
        u = urlparse(url)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        best = None  # (길이, allow)
        for allow, pat, rx in self.rules:
            if rx.match(path):
                cand = (len(pat), allow)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                    best = cand
        return True if best is None else best[1]


class PoliteFetcher:
    """robots.txt 확인 + 요청 간격 1~2초 + 차단 신호 시 그 도메인 전체 중단.
    우회(프록시·지문 위장·캡차 풀이) 없음."""

    def __init__(self, min_delay: float = 1.0, max_delay: float = 2.0):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.min_delay, self.max_delay = min_delay, max_delay
        self.robots: dict[str, Robots | None] = {}
        self.blocked_domains: set[str] = set()
        self.last: dict[str, float] = {}
        self.log = get_logger("fetch")

    def _robots(self, url: str) -> Robots | None:
        u = urlparse(url)
        key = f"{u.scheme}://{u.netloc}"
        if key not in self.robots:
            try:
                r = self.s.get(key + "/robots.txt", timeout=10)
                if r.status_code in BLOCK_STATUS:
                    self.robots[key] = None  # robots.txt 조차 막힘 → 도메인 차단으로 취급
                    return None
                self.robots[key] = Robots(r.text if r.status_code == 200 else "")
            except requests.RequestException:
                self.robots[key] = Robots("")
        return self.robots[key]

    def _wait(self, domain: str) -> None:
        gap = random.uniform(self.min_delay, self.max_delay)
        dt = time.time() - self.last.get(domain, 0)
        if dt < gap:
            time.sleep(gap - dt)
        self.last[domain] = time.time()

    def get(self, url: str) -> tuple[str | None, str]:
        """(html 또는 None, 상태 사유). 사유: ok / robots_disallow / domain_blocked / http_<code> / captcha / error"""
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
            r = self.s.get(url, timeout=15)
        except requests.RequestException as e:
            log_blocked(url, "error", type(e).__name__)
            return None, "error"
        if r.status_code in BLOCK_STATUS:
            self.blocked_domains.add(domain)
            log_blocked(url, f"http_{r.status_code}", "도메인 전체 중단")
            self.log.warning("blocked %s (%s) → 도메인 중단", domain, r.status_code)
            return None, f"http_{r.status_code}"
        if r.status_code != 200:
            log_blocked(url, f"http_{r.status_code}")
            return None, f"http_{r.status_code}"
        r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
        if looks_like_challenge(r.text):
            self.blocked_domains.add(domain)
            log_blocked(url, "captcha", "도메인 전체 중단")
            return None, "captcha"
        return r.text, "ok"
