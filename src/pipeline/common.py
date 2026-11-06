"""파이프라인 공통 설정/유틸."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
# Oracle's Elixir 원본은 CLAUDE.md 구조대로 data/inven_2026/games/ (하나만 둘 것)
OE_DIR = ROOT / "data" / "inven_2026" / "games"

YEAR = 2026
# 연도별 출력 폴더 (2025 데이터는 삭제함)
PROCESSED = ROOT / "data" / "processed" / str(YEAR)
# Oracle's Elixir의 league 값. LCK Cup 도 별도 값이면 여기에 추가.
OE_LEAGUES = {"LCK"}
# Leaguepedia Tournaments.League 값
LP_LEAGUE = "LoL Champions Korea"

HEADERS = {"User-Agent": "lol-project-data-pipeline/0.1 (research)"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def env(name: str) -> str | None:
    """환경변수 → 프로젝트 루트 .env 순으로 조회 (API 키/로그인 정보용)."""
    import os
    if os.getenv(name):
        return os.getenv(name)
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def http_get(url: str, *, params=None, retries: int = 5, backoff: float = 2.0, **kw) -> requests.Response:
    """재시도/백오프가 붙은 GET."""
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60, **kw)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                r.raise_for_status()
        except requests.RequestException:
            if i == retries - 1:
                raise
        time.sleep(backoff * (2 ** i))
    r.raise_for_status()
    return r


def save_json(obj, path: Path) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def save_csv(df, path: Path) -> None:
    ensure_dir(path.parent)
    # utf-8-sig: 엑셀에서 한글 깨짐 방지
    df.to_csv(path, index=False, encoding="utf-8-sig")
