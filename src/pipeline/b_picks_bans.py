"""B. 픽밴 순서 (Leaguepedia Cargo: PicksAndBansS7 + ScoreboardGames), common.YEAR LCK.

출력
  data/processed/<YEAR>/lp_tournaments.csv     LCK 대회 목록 (OverviewPage)
  data/processed/<YEAR>/b_draft_games.csv      경기 1행: 선픽/후픽 팀별 밴1~5·픽1~5(순서대로), 픽 포지션, 진영, 승자, 패치, 일시
  data/processed/<YEAR>/b_draft_actions.csv    드래프트 액션 1행 (경기 x 1~20번째): 순서, 진영, 밴/픽, 챔피언, 포지션

2026 First Selection 규칙으로 선픽과 진영이 분리됐다.
PicksAndBansS7 의 Team1 = 선픽 팀 (OE firstPick 과 502경기 전부 일치, 레드 선픽 371경기).
진영·선택권은 MatchScheduleGame 에서 가져온다 (Blue 가 OE side 와 502경기 전부 일치, 503경기 전부 값 있음).
ScoreboardGames 가 없는 경기는 date_kst·Patch 를 f_match_schedule.csv 의 매치 값으로 채운다.
밴도 진영과 상관없이 선픽 팀이 먼저 한다 (레드 선픽 경기 VOD 1건으로 확인).
표준 토너먼트 드래프트 순서 (F=선픽 팀, L=후픽 팀):
  밴1  F L F L F L  |  픽1  F L L F F L  |  밴2  L F L F  |  픽2  L F F L
"""
from __future__ import annotations

import argparse

import pandas as pd

from common import PROCESSED, get_logger, save_csv
from f_roster_schedule import TEAM_RENAMES
from leaguepedia import Cargo, in_clause, lck_tournaments

log = get_logger("B")

# (step, phase, order, kind, n) — n = 해당 팀의 몇 번째 밴/픽인지
DRAFT_ORDER = [
    (1, "ban1", "fp", "ban", 1), (2, "ban1", "lp", "ban", 1),
    (3, "ban1", "fp", "ban", 2), (4, "ban1", "lp", "ban", 2),
    (5, "ban1", "fp", "ban", 3), (6, "ban1", "lp", "ban", 3),
    (7, "pick1", "fp", "pick", 1), (8, "pick1", "lp", "pick", 1),
    (9, "pick1", "lp", "pick", 2), (10, "pick1", "fp", "pick", 2),
    (11, "pick1", "fp", "pick", 3), (12, "pick1", "lp", "pick", 3),
    (13, "ban2", "lp", "ban", 4), (14, "ban2", "fp", "ban", 4),
    (15, "ban2", "lp", "ban", 5), (16, "ban2", "fp", "ban", 5),
    (17, "pick2", "lp", "pick", 4), (18, "pick2", "fp", "pick", 4),
    (19, "pick2", "fp", "pick", 5), (20, "pick2", "lp", "pick", 5),
]

PB_FIELDS = (
    ["Team1", "Team2", "Winner", "Team1Score", "Team2Score", "Phase", "Tab",
     "N_GameInMatch", "GameId", "MatchId", "OverviewPage", "UniqueLine"]
    + [f"Team{t}Ban{i}" for t in (1, 2) for i in range(1, 6)]
    + [f"Team{t}Pick{i}" for t in (1, 2) for i in range(1, 6)]
    + [f"Team{t}Role{i}" for t in (1, 2) for i in range(1, 6)]
)
SG_FIELDS = ["DateTime_UTC", "Patch", "RiotPlatformGameId", "Gamelength", "WinTeam"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="캐시 무시하고 다시 조회")
    args = ap.parse_args()
    cargo = Cargo(refresh=args.refresh)

    tours = lck_tournaments(cargo)
    save_csv(pd.DataFrame(tours), PROCESSED / "lp_tournaments.csv")
    pages = [t["OverviewPage"] for t in tours]
    log.info("tournaments: %s", pages)

    rows = cargo.query(
        "picks_bans",
        tables="PicksAndBansS7=PB, ScoreboardGames=SG, MatchScheduleGame=MSG",
        join_on="PB.GameId=SG.GameId, PB.GameId=MSG.GameId",
        fields=", ".join([f"PB.{f}={f}" for f in PB_FIELDS] + [f"SG.{f}={f}" for f in SG_FIELDS]
                         + ["MSG.Blue=BlueTeam", "MSG.FirstSelection=FirstSelection",
                            "MSG.PickSelection=PickSelection"]),
        where=in_clause("PB.OverviewPage", pages),
        order_by="SG.DateTime_UTC, PB.N_GameInMatch",
    )
    g = pd.DataFrame(rows).replace({"": None})
    g = g.rename(columns={"DateTime UTC": "DateTime_UTC"})
    g["date_utc"] = pd.to_datetime(g["DateTime_UTC"], errors="coerce")
    g["date_kst"] = g["date_utc"].dt.tz_localize("UTC").dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d %H:%M")
    sched = pd.read_csv(PROCESSED / "f_match_schedule.csv", dtype=str).set_index("MatchId")
    g["date_kst"] = g["date_kst"].fillna(g["MatchId"].map(sched["date_kst"]))
    g["Patch"] = g["Patch"].fillna(g["MatchId"].map(sched["Patch"]))
    g["fp_team"], g["lp_team"] = g["Team1"].replace(TEAM_RENAMES), g["Team2"].replace(TEAM_RENAMES)
    g["blue_team"] = g["BlueTeam"].replace(TEAM_RENAMES)
    g["red_team"] = g.apply(lambda r: r["lp_team"] if r["blue_team"] == r["fp_team"] else r["fp_team"], axis=1)
    g["fp_side"] = g.apply(lambda r: "Blue" if r["blue_team"] == r["fp_team"] else "Red", axis=1)
    g["winner_team"] = g.apply(lambda r: r["fp_team"] if r["Winner"] == "1"
                               else r["lp_team"] if r["Winner"] == "2" else None, axis=1)
    g["winner_side"] = g.apply(lambda r: None if r["winner_team"] is None
                               else "Blue" if r["winner_team"] == r["blue_team"] else "Red", axis=1)
    # 선택권 보유 팀과 그 팀의 선택. 결승 1세트는 'N/A' 로, 한 팀이 진영과 픽 순서를 모두 정한다
    g["fs_team"] = g["FirstSelection"].replace({"N/A": None}).replace(TEAM_RENAMES)
    picker = g["PickSelection"].replace(TEAM_RENAMES)
    g["fs_choice"] = (picker == g["fs_team"]).map({True: "pick", False: "side"}).where(g["fs_team"].notna())
    rename = {}
    for t, order in ((1, "fp"), (2, "lp")):
        for i in range(1, 6):
            rename[f"Team{t}Ban{i}"] = f"{order}_ban{i}"
            rename[f"Team{t}Pick{i}"] = f"{order}_pick{i}"
            rename[f"Team{t}Role{i}"] = f"{order}_pick{i}_role"
    g = g.rename(columns=rename)
    front = ["GameId", "MatchId", "RiotPlatformGameId", "OverviewPage", "Tab", "Phase", "N_GameInMatch",
             "date_kst", "Patch", "fp_team", "lp_team", "fp_side", "blue_team", "red_team",
             "fs_team", "fs_choice", "winner_side", "winner_team", "Gamelength"]
    draft_cols = list(rename.values())
    games = g[front + draft_cols].sort_values(["date_kst", "N_GameInMatch"])
    save_csv(games, PROCESSED / "b_draft_games.csv")

    # long format: 1~20번째 액션
    acts = []
    for _, r in games.iterrows():
        for step, phase, order, kind, n in DRAFT_ORDER:
            champ = r[f"{order}_{kind}{n}"]
            team = r[f"{order}_team"]
            side = "Blue" if team == r["blue_team"] else "Red"
            acts.append({
                "GameId": r["GameId"], "date_kst": r["date_kst"], "Patch": r["Patch"],
                "step": step, "phase": phase, "pick_order": order, "side": side, "action": kind, "side_order": n,
                "team": team, "opponent": r["lp_team" if order == "fp" else "fp_team"],
                "champion": None if champ in (None, "None") else champ,
                "role": r.get(f"{order}_pick{n}_role") if kind == "pick" else None,
                "is_winner": r["winner_team"] == team,
            })
    actions = pd.DataFrame(acts)
    save_csv(actions, PROCESSED / "b_draft_actions.csv")
    log.info("draft games=%d, actions=%d, missing champs=%d, fp side=%s, fs choice=%s",
             len(games), len(actions), actions["champion"].isna().sum(),
             games["fp_side"].value_counts(dropna=False).to_dict(),
             games["fs_choice"].value_counts(dropna=False).to_dict())


if __name__ == "__main__":
    main()
