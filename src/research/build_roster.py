"""선수·팀 설정 만들기 — research/config/players.yaml, teams.yaml (코드만, 토큰 0).

대상 선수 (2026-11-03 결정): 리서치 기간 LCK 경기 10경기 이상 + 주전 (각 split 팀·포지션 최다 출전).
  주전은 모두 10경기 이상이라 결과는 "10경기 이상" 과 같음 → 62명
별칭: 인벤 노트북 v8.8 의 ROSTER(64명, 한글 닉네임·추가 별칭·애매 표시·제외 패턴)를 research/config/roster_aliases.json 으로 옮겨 씀
  + 본명(Leaguepedia NativeName) + 영문 ID. 노트북에 없는 선수는 EXTRA 에 손으로 추가
  + 이미 손본 선수(도란·오너·페이커)는 MANUAL 그대로 유지
팀: 노트북 TEAM_ALIASES + OE 팀 이름

사용: python build_roster.py
"""
from __future__ import annotations

import json
import re

import pandas as pd
import yaml

from common import CONFIG, DATA, INVEN, get_logger, nfc

log = get_logger("roster")
PROC = DATA / "processed" / "2026"
MIN_GAMES = 10
TEAM_KO_TO_OE = {"젠지": "Gen.G", "한화생명": "Hanwha Life Esports", "T1": "T1", "KT": "KT Rolster",
                 "디플러스기아": "Dplus Kia", "농심": "Nongshim RedForce", "피어엑스": "BNK FEARX",
                 "브리온": "HANJIN BRION", "DRX": "Kiwoom DRX", "DN수퍼스": "DN SOOPers"}
POS_KO = {"top": "탑", "jng": "정글", "mid": "미드", "bot": "원딜", "sup": "서폿"}
# 노트북 ROSTER 에 없는 10경기 이상 선수 (2026-11-03 추가)
EXTRA = {"Sharvel": {"ko": ["샤벨"], "opt": {}},
         "Loki": {"ko": ["로키"], "opt": {"ambiguous": True, "exclude": r"로키\s*(산맥|마블|토르)"}}}
# v1 에서 데이터로 손본 별칭 — 덮어쓰지 않음
MANUAL = {"Doran", "Oner", "Faker"}


def inven_file(pid: str) -> str | None:
    names = {nfc(f.stem).lower(): f.stem for f in (INVEN / "search").glob("*.csv")}
    return names.get(pid.lower())


def aliases_for(pid: str, ko: list[str], opt: dict, real: str | None) -> tuple[list[str], list[str]]:
    al = [opt["pat"]] if opt.get("pat") else list(ko)
    al += opt.get("nick", [])
    al.append(rf"(?i)\b{re.escape(pid)}\b")
    if real and isinstance(real, str) and len(real) >= 3:  # 2글자 본명(허수 등)은 일반 낱말과 겹쳐서 뺌
        al.append(real)
    ex = [opt["exclude"]] if opt.get("exclude") else []
    return list(dict.fromkeys(al)), ex


def main():
    nb = json.loads((CONFIG / "roster_aliases.json").read_text(encoding="utf-8"))
    nb_by_id = {r[2]: r for r in nb["roster"]}
    pg = pd.read_csv(PROC / "a_player_games.csv", dtype={"patch": str})
    ros = pd.read_csv(PROC / "f_rosters.csv").drop_duplicates("ID").set_index("ID")
    games = pg.groupby("playername").size()
    team_of = pg.groupby("playername")["teamname"].agg(lambda s: s.value_counts().index[0])
    pos_of = pg.groupby("playername")["position"].agg(lambda s: s.value_counts().index[0])
    targets = sorted(games[games >= MIN_GAMES].index, key=lambda p: (team_of[p], list(POS_KO).index(pos_of[p])))

    old = yaml.safe_load((CONFIG / "players.yaml").read_text(encoding="utf-8"))["players"]
    out, missing_alias = {}, []
    for pid in targets:
        real = ros["NativeName"].get(pid) if pid in ros.index else None
        real = real if isinstance(real, str) else None  # 본명 없음(NaN)
        if pid in MANUAL and pid in old:
            out[pid] = old[pid] | {"games": int(games[pid])}
            continue
        if pid in nb_by_id:
            _, _, _, ko, opt = nb_by_id[pid]
        elif pid in EXTRA:
            ko, opt = EXTRA[pid]["ko"], EXTRA[pid]["opt"]
        else:
            missing_alias.append(pid)
            ko, opt = [], {}
        al, ex = aliases_for(pid, ko, opt, real)
        out[pid] = {"name_ko": ko[0] if ko else pid, "real_name": real or "", "team": team_of[pid],
                    "position": POS_KO[pos_of[pid]], "inven_search": inven_file(pid), "games": int(games[pid]),
                    "ambiguous": bool(opt.get("ambiguous")), "aliases": al, "exclude": ex}
    head = ("# 대상 선수와 별칭 — build_roster.py 가 생성 (2026-11-03). 손으로 고친 뒤엔 다시 생성하지 말 것\n"
            "# 대상: 리서치 기간 LCK 10경기 이상 + 주전. 별칭 출처: 인벤 노트북 v8.8 ROSTER + 본명 + 영문 ID\n"
            "# ambiguous: true = 일반 낱말과 겹치는 닉네임 (S4·S5 에서 선수 얘기인지 더 엄격히)\n"
            "# inven_search: null = 인벤 검색 데이터 없음 (커뮤니티는 OP.GG·유튜브만)\n")
    (CONFIG / "players.yaml").write_text(head + yaml.safe_dump({"players": out}, allow_unicode=True, sort_keys=False),
                                         encoding="utf-8")
    teams = {}
    for ko_name, al in nb["team_aliases"].items():
        oe = TEAM_KO_TO_OE[ko_name]
        teams[oe] = {"name_ko": ko_name, "aliases": list(dict.fromkeys([ko_name, *al, oe])),
                     "players": [p for p in out if out[p]["team"] == oe]}
    (CONFIG / "teams.yaml").write_text("# 팀과 별칭 — build_roster.py 생성 (출처: 인벤 노트북 TEAM_ALIASES)\n"
                                       + yaml.safe_dump({"teams": teams}, allow_unicode=True, sort_keys=False),
                                       encoding="utf-8")
    log.info("선수 %d명 (별칭 없음 %s, 인벤 검색 없음 %s), 팀 %d개", len(out), missing_alias,
             [p for p in out if not out[p]["inven_search"]], len(teams))


if __name__ == "__main__":
    main()
