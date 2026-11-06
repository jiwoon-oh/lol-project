"""G 보조: trackingthepros.com 에서 LCK 출전 선수의 솔랭 계정 수집.

Leaguepedia SoloqueueIds 는 옛 이름이 많아 계정을 절반밖에 못 찾으므로 보완한다.
선수 페이지의 설명문에 있는 본명(한글)을 f_rosters.csv 의 NativeName 과 대조해 동명이인을 거른다.

출력
  data/raw/trackingthepros/<slug>.html       원본 (캐시)
  data/processed/g_ttp_accounts.csv          선수 x KR 계정: riot_id, 현재 티어, 비활성 여부, 본명 대조 결과
"""
from __future__ import annotations

import html as htmllib
import re
import time
from urllib.parse import quote

import pandas as pd
import requests

from common import HEADERS, PROCESSED, RAW, ensure_dir, get_logger, save_csv

log = get_logger("TTP")

BASE = "https://www.trackingthepros.com/player/"
MIN_INTERVAL = 2.0  # 요청 간 최소 간격(초)
RAW_DIR = ensure_dir(RAW / "trackingthepros")
# 페이지에 한글 본명이 없거나 표기가 달라 자동 대조가 안 되는 선수. 영문 본명·팀·포지션을 사람이 확인함
MANUAL_VERIFIED = {"DuDu (Lee Dong-ju)", "LazyFeel", "Kingen", "Jiwoo", "kyeahoo", "Casting", "Paduck",
                   "Quantum (Son Jeong-hwan)", "Willer", "Scout"}

ROW = re.compile(r"<tr(?P<attr>[^>]*)><td><b>\[(?P<region>[A-Z]+)\]</b>\s*(?P<rid>[^<]+)</td>"
                 r"<td class='dt-right'>(?P<rank>[^<]*)</td></tr>")


def slug(player: str) -> str:
    return re.sub(r"\s*\(.*\)$", "", player).strip()  # 'Clear (Song Hyeon-min)' -> 'Clear'


def fetch(name: str) -> str | None:
    f = RAW_DIR / f"{quote(name, safe='')}.html"
    if f.exists():
        return f.read_text(encoding="utf-8")
    time.sleep(MIN_INTERVAL)
    r = requests.get(BASE + quote(name), headers=HEADERS, timeout=30)
    if r.status_code != 200:
        log.warning("%s: HTTP %d", name, r.status_code)
        return None
    f.write_text(r.text, encoding="utf-8")
    return r.text


def parse(page: str) -> tuple[str, list[dict]]:
    m = re.search(r'<meta name="description" content="([^"]*)"', page)
    desc = htmllib.unescape(m.group(1)) if m else ""
    rows = [{"region": x["region"], "riot_id": htmllib.unescape(x["rid"]).strip(),
             "rank_now": x["rank"].strip(), "inactive": "inactive_account" in x["attr"]}
            for x in ROW.finditer(page)]
    return desc, rows


def main():
    r = pd.read_csv(PROCESSED / "f_rosters.csv")
    players = r[~r["is_coach"] & (r["games_played"] > 0)].drop_duplicates("Player")
    out, summary = [], []
    for _, p in players.iterrows():
        page = fetch(slug(p["Player"]))
        if page is None:
            summary.append({"player": p["Player"], "status": "no_page"})
            continue
        desc, rows = parse(page)
        native = p["NativeName"] if isinstance(p["NativeName"], str) else None
        verified = bool(native and native in desc) or p["Player"] in MANUAL_VERIFIED
        kr = [x for x in rows if x["region"] == "KR"]
        summary.append({"player": p["Player"], "status": "ok" if verified else "name_mismatch",
                        "native": native, "desc": desc[:90], "kr_accounts": len(kr), "all_accounts": len(rows)})
        for x in kr:
            out.append({"player": p["Player"], "team": p["Team"], "role": p["Role"], **x,
                        "native_verified": verified})
    df = pd.DataFrame(out)
    save_csv(df, PROCESSED / "g_ttp_accounts.csv")
    s = pd.DataFrame(summary)
    log.info("players=%d, status=%s, kr accounts=%d", len(s), s["status"].value_counts().to_dict(), len(df))
    print(s[s["status"] != "ok"].to_string())
    print("KR 계정 0개:", s[(s["status"] == "ok") & (s["kr_accounts"] == 0)]["player"].tolist())


if __name__ == "__main__":
    main()
