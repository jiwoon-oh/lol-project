"""F. 로스터·일정 (Leaguepedia Cargo: TournamentPlayers, Players, ScoreboardPlayers, MatchSchedule), common.YEAR LCK.

출력
  data/processed/f_rosters.csv          대회 x 팀 x 선수: 포지션, is_coach, games_played, 본명, 국적, 생일, 솔랭 계정(SoloqueueIds)
  data/processed/f_players_played.csv   대회 x 팀 x 선수 x 포지션: 실제 출전 세트 수 (ScoreboardPlayers)
  data/processed/f_match_schedule.csv   매치(세트 묶음) 1행: 일시(KST), 팀, 스코어, 라운드
  data/processed/f_team_schedule.csv    팀 관점 1행: 상대, 결과, 다음 경기 일시/상대 (스카우팅 대상 선정용)

--as-of YYYY-MM-DD 를 주면 그 시점 기준 '다음 경기 상대' 테이블(f_next_opponents.csv)도 만든다.
"""
from __future__ import annotations

import argparse

import pandas as pd

from common import PROCESSED, get_logger, save_csv
from leaguepedia import Cargo, in_clause, lck_tournaments

log = get_logger("F")

# 시즌 중 팀명 변경: 옛 이름 -> 현재 이름. 팀 관점 테이블이 끊기지 않도록 통일한다
TEAM_RENAMES = {"BRION": "HANJIN BRION", "DRX": "Kiwoom DRX"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--as-of", help="이 날짜(KST) 이후 첫 경기를 '다음 경기'로 계산")
    args = ap.parse_args()
    cargo = Cargo(refresh=args.refresh)
    pages = [t["OverviewPage"] for t in lck_tournaments(cargo)]

    # --- 로스터
    rows = cargo.query(
        "rosters",
        tables="TournamentPlayers=TP, Players=P",
        join_on="TP.Link=P.OverviewPage",
        fields="TP.OverviewPage=OverviewPage, TP.Team=Team, TP.Link=Player, TP.Role=Role, "
               "TP.N_PlayerInTeam=N_PlayerInTeam, P.ID=ID, P.Name=Name, P.NativeName=NativeName, "
               "P.Country=Country, P.Birthdate=Birthdate, P.Role=MainRole, P.SoloqueueIds=SoloqueueIds",
        where=in_clause("TP.OverviewPage", pages),
        order_by="TP.OverviewPage, TP.Team, TP.N_PlayerInTeam",
    )
    rosters = pd.DataFrame(rows).replace({"": None}).drop(columns=["Birthdate__precision"], errors="ignore")
    rosters["is_coach"] = rosters["Role"].eq("Coach")  # TournamentPlayers 에는 코치도 포함됨
    rosters["team_std"] = rosters["Team"].replace(TEAM_RENAMES)

    # --- 실제 출전 기록 (로스터 등록 != 출전: 서브/2군 콜업 인원 구분용)
    rows = cargo.query(
        "players_played",
        tables="ScoreboardPlayers=SP",
        fields="SP.OverviewPage=OverviewPage, SP.Team=Team, SP.Link=Player, SP.Role=Role, COUNT(*)=Games",
        where=in_clause("SP.OverviewPage", pages),
        group_by="SP.OverviewPage, SP.Team, SP.Link, SP.Role",
        order_by="SP.OverviewPage, SP.Team",
    )
    played = pd.DataFrame(rows).replace({"": None})
    played["Games"] = played["Games"].astype(int)
    save_csv(played, PROCESSED / "f_players_played.csv")
    games = (played.groupby(["OverviewPage", "Team", "Player"])["Games"].sum()
             .rename("games_played").reset_index())
    rosters = rosters.merge(games, on=["OverviewPage", "Team", "Player"], how="left")
    rosters["games_played"] = rosters["games_played"].fillna(0).astype(int)
    save_csv(rosters, PROCESSED / "f_rosters.csv")
    log.info("roster rows=%d, teams=%d, players=%d, coaches=%d", len(rosters), rosters["Team"].nunique(),
             rosters.loc[~rosters["is_coach"], "Player"].nunique(), rosters.loc[rosters["is_coach"], "Player"].nunique())

    # --- 일정
    rows = cargo.query(
        "match_schedule",
        tables="MatchSchedule",
        fields="MatchId, OverviewPage, Tab, Round, Phase, N_MatchInPage, DateTime_UTC, BestOf, "
               "Team1, Team2, Winner, Team1Score, Team2Score, Patch, IsTiebreaker",
        where=in_clause("OverviewPage", pages),
        order_by="DateTime_UTC",
    )
    ms = (pd.DataFrame(rows).replace({"": None})
          .rename(columns={"DateTime UTC": "DateTime_UTC", "N MatchInPage": "N_MatchInPage"})
          .drop(columns=["DateTime UTC__precision"], errors="ignore"))
    ms["dt_kst"] = (pd.to_datetime(ms["DateTime_UTC"], errors="coerce")
                    .dt.tz_localize("UTC").dt.tz_convert("Asia/Seoul"))
    ms["date_kst"] = ms["dt_kst"].dt.strftime("%Y-%m-%d %H:%M")
    ms["winner_team"] = ms.apply(lambda r: r["Team1"] if r["Winner"] == "1"
                                 else r["Team2"] if r["Winner"] == "2" else None, axis=1)
    save_csv(ms.drop(columns=["dt_kst"]), PROCESSED / "f_match_schedule.csv")

    # --- 팀 관점 + 다음 경기
    team_rows = []
    for _, r in ms.iterrows():
        for me, opp, my_s, op_s in (("Team1", "Team2", "Team1Score", "Team2Score"),
                                    ("Team2", "Team1", "Team2Score", "Team1Score")):
            team_rows.append({"MatchId": r["MatchId"], "OverviewPage": r["OverviewPage"], "Tab": r["Tab"],
                              "dt_kst": r["dt_kst"], "team": TEAM_RENAMES.get(r[me], r[me]),
                              "opponent": TEAM_RENAMES.get(r[opp], r[opp]),
                              "score": r[my_s], "opp_score": r[op_s],
                              "result": None if r["winner_team"] is None else ("W" if r["winner_team"] == r[me] else "L")})
    ts = pd.DataFrame(team_rows).sort_values(["team", "dt_kst"])
    ts["next_match_kst"] = ts.groupby("team")["dt_kst"].shift(-1)
    ts["next_opponent"] = ts.groupby("team")["opponent"].shift(-1)
    for c in ("dt_kst", "next_match_kst"):
        ts[c] = ts[c].dt.strftime("%Y-%m-%d %H:%M")
    save_csv(ts.rename(columns={"dt_kst": "date_kst"}), PROCESSED / "f_team_schedule.csv")
    log.info("matches=%d, team-rows=%d", len(ms), len(ts))

    if args.as_of:
        cut = pd.Timestamp(args.as_of, tz="Asia/Seoul")
        fut = pd.DataFrame(team_rows)
        fut = fut[fut["dt_kst"] >= cut].sort_values("dt_kst").groupby("team").head(1)
        save_csv(fut, PROCESSED / "f_next_opponents.csv")
        log.info("next opponents as of %s: %d teams", args.as_of, len(fut))


if __name__ == "__main__":
    main()
