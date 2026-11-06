"""A. Oracle's Elixir 경기 기록 (common.YEAR, LCK).

출력
  data/inven_2026/games/<YEAR>_LoL_esports_match_data_from_OraclesElixir.csv  (원본, 전 리그)
  data/processed/<YEAR>/a_oe_lck_<YEAR>_raw.csv      LCK 행만 (원본 컬럼 전체)
  data/processed/a_team_games.csv           팀-경기 1행: 사이드/결과/밴/포지션별 픽·선수
  data/processed/a_player_games.csv         선수-경기 1행: 골드·CS 차이 @10/15, 딜 비중, KDA 등
"""
from __future__ import annotations

import argparse

import gdown
import pandas as pd

from common import OE_DIR, PROCESSED, YEAR, OE_LEAGUES, ensure_dir, get_logger, save_csv

log = get_logger("A")

# Oracle's Elixir 공개 Google Drive 폴더
OE_FOLDER_ID = "1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH"
OE_FILENAME = f"{YEAR}_LoL_esports_match_data_from_OraclesElixir.csv"

POSITIONS = ["top", "jng", "mid", "bot", "sup"]
GAME_COLS = ["gameid", "league", "split", "playoffs", "date", "game", "patch", "datacompleteness", "url"]
PLAYER_METRICS = [
    "kills", "deaths", "assists", "damageshare", "dpm", "earnedgoldshare", "vspm",
    "goldat10", "csat10", "golddiffat10", "csdiffat10", "xpdiffat10",
    "goldat15", "csat15", "golddiffat15", "csdiffat15", "xpdiffat15",
    "totalgold", "total cs", "gamelength",
]


def find_file_id() -> str:
    files = gdown.download_folder(id=OE_FOLDER_ID, skip_download=True, quiet=True)
    for f in files:
        if f.path.endswith(OE_FILENAME):
            return f.id
    raise FileNotFoundError(f"{OE_FILENAME} not found in Oracle's Elixir folder")


def download(force: bool = False):
    out = ensure_dir(OE_DIR) / OE_FILENAME
    if out.exists() and not force:
        log.info("cached: %s", out)
        return out
    file_id = find_file_id()
    log.info("downloading %s (id=%s)", OE_FILENAME, file_id)
    try:
        gdown.download(id=file_id, output=str(out), quiet=False)
    except Exception as e:  # Google Drive 다운로드 한도 초과가 흔함 (최대 24h 후 해제)
        raise SystemExit(
            f"Google Drive 다운로드 실패: {type(e).__name__}\n"
            f"브라우저에서 https://drive.google.com/uc?id={file_id} 로 받아\n  {out}\n"
            f"에 두거나 `--file <경로>` 로 지정한 뒤 다시 실행하세요.")
    return out


def build(path) -> None:
    df = pd.read_csv(path, low_memory=False)
    log.info("all leagues: %d rows, leagues containing LCK: %s",
             len(df), sorted(l for l in df["league"].dropna().unique() if "LCK" in l))
    df = df[df["league"].isin(OE_LEAGUES)].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"].dt.year == YEAR]
    df["patch"] = df["patch"].map(lambda p: f"{p:.2f}" if pd.notna(p) else p)
    save_csv(df, PROCESSED / f"a_oe_lck_{YEAR}_raw.csv")

    players = df[df["position"].isin(POSITIONS)].copy()
    teams = df[df["position"] == "team"].copy()

    # --- 선수-경기 테이블
    players["kda"] = (players["kills"] + players["assists"]) / players["deaths"].clip(lower=1)
    pcols = GAME_COLS + ["side", "teamname", "teamid", "position", "playername", "playerid",
                         "champion", "result"] + PLAYER_METRICS + ["kda"]
    player_games = players[[c for c in pcols if c in players.columns]].sort_values(["date", "gameid", "side", "position"])
    save_csv(player_games, PROCESSED / "a_player_games.csv")

    # --- 팀-경기 테이블 (포지션별 pick/선수 pivot)
    order = {p: i for i, p in enumerate(POSITIONS)}
    players["pos_idx"] = players["position"].map(order)
    pick = players.pivot_table(index=["gameid", "side"], columns="position", values="champion", aggfunc="first")
    pick = pick[POSITIONS].add_suffix("_pick")
    who = players.pivot_table(index=["gameid", "side"], columns="position", values="playername", aggfunc="first")
    who = who[POSITIONS].add_suffix("_player")

    tcols = GAME_COLS + ["side", "teamname", "teamid", "result", "gamelength",
                         "ban1", "ban2", "ban3", "ban4", "ban5",
                         "firstblood", "firstdragon", "firstherald", "firstbaron", "firsttower",
                         "golddiffat10", "golddiffat15", "kills", "deaths", "totalgold"]
    team_games = teams[[c for c in tcols if c in teams.columns]].set_index(["gameid", "side"])
    team_games = team_games.join(pick).join(who).reset_index()
    # 상대팀 이름
    opp = team_games[["gameid", "side", "teamname"]].copy()
    opp["side"] = opp["side"].map({"Blue": "Red", "Red": "Blue"})
    team_games = team_games.merge(opp.rename(columns={"teamname": "opp_teamname"}), on=["gameid", "side"], how="left")
    team_games = team_games.sort_values(["date", "gameid", "side"])
    save_csv(team_games, PROCESSED / "a_team_games.csv")

    log.info("LCK %d: %d games, %d team rows, %d player rows, patches=%s",
             YEAR, team_games["gameid"].nunique(), len(team_games), len(player_games),
             sorted(team_games["patch"].dropna().unique()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="원본 CSV 재다운로드")
    ap.add_argument("--file", help="직접 받은 Oracle's Elixir CSV 경로 (다운로드 생략)")
    args = ap.parse_args()
    build(args.file or download(args.force))


if __name__ == "__main__":
    main()
