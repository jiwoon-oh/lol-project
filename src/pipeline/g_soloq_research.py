"""G-2. 리서치 대상 58명의 솔로랭크 전체 경기 (Riot Games API, KR) — 리서치 기간만, 표본 없이 전부.

g_riot_soloq.py 는 계정·월당 30경기 표본이라 챔피언별 판수가 적게 잡힘. 이 스크립트는
사용자가 정리한 계정 목록(data/raw/riot/soloq_range.csv)으로 대상 선수 계정의 리서치 기간 솔랭을 모두 받는다.

키: 프로젝트 루트 .env 의 RIOT_API_KEY (키 하나로 전체 실행. 개발 키는 24시간마다 재발급)

단계
  plan     : 계정 목록 ↔ 대상 58명 대조만 (API 호출 없음, 키 불필요)
  accounts : Riot ID → PUUID (account-v1, 캐시 data/raw/riot/accounts.json 을 g_riot_soloq.py 와 같이 씀)
  ids      : PUUID별 기간 내 솔랭 경기 id 전부 (월별 + 100개씩 페이지 넘김, 캐시 data/raw/riot/matchids_research/)
  matches  : 경기 상세 (캐시 data/raw/riot/matches/ 공용 — 이미 받은 경기는 다시 안 받음) → CSV

기간: research/config/tournament_patches.csv 의 리서치 창 첫날 ~ 마지막 날 (2026-01-14 ~ 2026-08-25, KST).
      비시즌·MSI 기간 경기도 받고 research_patch 칸을 비워 둠 (쓸지는 분석 때 결정)

출력 (data/processed/2026/)
  g_research_accounts.csv        선수 × 계정 × PUUID
  g_research_account_check.csv   계정별 포지션 일치율 (타인 계정 의심 표시)
  g_research_soloq.csv           대상 선수 본인 경기만 1행 (패치, 리서치 패치 창, 챔피언, 승패, KDA …)
  g_research_soloq_champs.csv    선수 × 챔피언 집계 (다시하기·의심 계정 제외)

사용
  python g_soloq_research.py --steps plan
  python g_soloq_research.py                      # accounts,ids,matches (중단돼도 다시 돌리면 캐시로 이어감)
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from urllib.parse import quote

import pandas as pd
import yaml

from common import PROCESSED, RAW, ROOT, ensure_dir, get_logger, save_csv
from g_riot_soloq import KST, QUEUE_SOLO, REGION, RIOT, Riot, flatten, load_key, match

log = get_logger("G2")

ACCOUNTS_CSV = RAW / "riot" / "soloq_range.csv"
PLAYERS_YAML = ROOT / "research" / "config" / "players.yaml"
PATCHES_CSV = ROOT / "research" / "config" / "tournament_patches.csv"
POS = {"탑": "TOP", "정글": "JUNGLE", "미드": "MIDDLE", "원딜": "BOTTOM", "서폿": "UTILITY"}
MIN_CHECK_GAMES = 10
MIN_ROLE_SHARE = 0.4
PAGE = 100


def research_windows() -> pd.DataFrame:
    w = pd.read_csv(PATCHES_CSV, encoding="utf-8-sig", dtype=str)
    return w[w["role"] == "research"]


def period() -> tuple[int, int]:
    """리서치 창 첫날 00:00 ~ 마지막 날 다음날 00:00 (KST) 의 epoch 초."""
    w = research_windows()
    a = datetime.fromisoformat(w["window_from"].min()).replace(tzinfo=KST)
    b = datetime.fromisoformat(w["window_to"].max()).replace(tzinfo=KST) + pd.Timedelta(days=1)
    return int(a.timestamp()), int(b.timestamp())


def plan() -> pd.DataFrame:
    """대상 58명의 계정 행 (status ok · listed > 0). 계정 목록의 선수명은 대소문자 무시로 맞춤."""
    players = yaml.safe_load(PLAYERS_YAML.read_text(encoding="utf-8"))["players"]
    s = pd.read_csv(ACCOUNTS_CSV, encoding="utf-8-sig")
    name = {k.lower(): k for k in players}
    s["player"] = s["player"].str.lower().map(name)
    missing = sorted(set(players) - set(s["player"].dropna()))
    s = s[s["player"].notna() & (s["status"] == "ok") & (s["listed"] > 0)].copy()
    s["position"] = s["player"].map(lambda p: players[p]["position"])
    s["team_research"] = s["player"].map(lambda p: players[p]["team"])
    dup = s[s.duplicated("riot_id", keep=False)]
    log.info("대상 %d명 중 계정 있는 선수 %d명, 계정 %d개 (목록에 없는 선수: %s)",
             len(players), s["player"].nunique(), len(s), missing or "없음")
    if len(dup):
        log.warning("두 선수에 올라 있는 계정 (선수 라벨을 붙이지 않음): %s",
                    dup.groupby("riot_id")["player"].apply(list).to_dict())
    no_acc = sorted(set(players) - set(s["player"]))
    if no_acc:
        log.warning("경기 있는 계정이 없는 선수: %s", no_acc)
    return s


def resolve(riot: Riot, s: pd.DataFrame) -> pd.DataFrame:
    cache_f = RIOT / "accounts.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    rows = []
    for _, r in s.iterrows():
        key = r["riot_id"].lower()
        if key not in cache:
            name, tag = r["riot_id"].rsplit("#", 1)
            cache[key] = riot.get(f"{REGION}/riot/account/v1/accounts/by-riot-id/"
                                  f"{quote(name.strip(), safe='')}/{quote(tag.strip(), safe='')}")
            cache_f.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        acc = cache[key]
        rows.append({"player": r["player"], "team": r["team_research"], "position": r["position"],
                     "riot_id": r["riot_id"], "listed_all": r["listed"], "oldest_listed": r["oldest"],
                     "puuid": acc["puuid"] if acc else None})
    df = pd.DataFrame(rows)
    save_csv(df, PROCESSED / "g_research_accounts.csv")
    log.info("계정 %d개 중 PUUID 확인 %d개 (선수 %d명)", len(df), df["puuid"].notna().sum(),
             df.dropna(subset=["puuid"])["player"].nunique())
    return df


def match_ids(riot: Riot, puuid: str, start: int, end: int) -> list[str]:
    """기간 내 솔랭 id 전부. 월별로 나누고 각 달은 100개씩 페이지를 넘김 (목록은 최신순)."""
    f = ensure_dir(RIOT / "matchids_research") / f"{puuid}_{start}_{end}.json"
    if f.exists():
        return json.loads(f.read_text())
    out = []
    edges = list(pd.date_range(pd.Timestamp(start, unit="s", tz=KST), pd.Timestamp(end, unit="s", tz=KST),
                               freq="MS").map(lambda t: int(t.timestamp())))
    edges = sorted({start, *edges, end})
    for a, b in zip(edges, edges[1:]):
        i = 0
        while True:
            ids = riot.get(f"{REGION}/lol/match/v5/matches/by-puuid/{puuid}/ids",
                           {"queue": QUEUE_SOLO, "startTime": a, "endTime": b, "start": i, "count": PAGE}) or []
            out += ids
            if len(ids) < PAGE:
                break
            i += PAGE
    f.write_text(json.dumps(out))
    return out


def account_check(own: pd.DataFrame, accts: pd.DataFrame) -> pd.DataFrame:
    pos = dict(zip(accts["player"], accts["position"].map(POS)))
    rows = []
    for (player, puuid), g in own[~own["is_remake"]].groupby(["pro_player", "puuid"]):
        share = (g["teamPosition"] == pos[player]).mean()
        rows.append({"player": player, "puuid": puuid, "riot_ids": "|".join(sorted(g["riot_id"].unique())),
                     "n_matches": len(g), "first": g["game_start_kst"].min(), "last": g["game_start_kst"].max(),
                     "role_share": round(share, 3), "win_rate": round(g["win"].mean(), 3),
                     "suspect": len(g) >= MIN_CHECK_GAMES and share < MIN_ROLE_SHARE})
    return pd.DataFrame(rows).sort_values(["player", "n_matches"], ascending=[True, False])


def label_patch(t: pd.Series) -> pd.Series:
    """경기 시각(KST 문자열) → 리서치 패치 창 이름 (26.xx). 창 밖(비시즌·MSI)은 빈칸."""
    out = pd.Series(None, index=t.index, dtype=object)
    day = t.str[:10]
    for _, w in research_windows().iterrows():
        out[(day >= w["window_from"]) & (day <= w["window_to"])] = w["patch"]
    return out


def champs(own: pd.DataFrame) -> pd.DataFrame:
    g = own.groupby(["pro_player", "champion_id"])
    df = g.agg(games=("win", "size"), wins=("win", "sum"), kills=("kills", "sum"), deaths=("deaths", "sum"),
               assists=("assists", "sum"), cspm=("cspm", "mean"), damageshare=("damageshare", "mean")).reset_index()
    df["win_rate"] = (df["wins"] / df["games"]).round(3)
    df["kda"] = ((df["kills"] + df["assists"]) / df["deaths"].clip(lower=1)).round(2)
    df["share"] = (df["games"] / df.groupby("pro_player")["games"].transform("sum")).round(3)
    df[["cspm", "damageshare"]] = df[["cspm", "damageshare"]].round(3)
    return df.rename(columns={"pro_player": "player"}).sort_values(["player", "games"], ascending=[True, False])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="accounts,ids,matches", help="plan / accounts,ids,matches (쉼표 구분)")
    args = ap.parse_args()
    steps = set(args.steps.split(","))
    s = plan()
    if steps == {"plan"}:
        log.info("\n%s", s.groupby("player").agg(accounts=("riot_id", "size"), oldest=("oldest", "min")).to_string())
        return

    riot = Riot(load_key())
    accts = resolve(riot, s) if "accounts" in steps else pd.read_csv(PROCESSED / "g_research_accounts.csv")
    a = accts.dropna(subset=["puuid"])
    shared = set(a.loc[a.duplicated("puuid", keep=False), "puuid"])  # 두 선수에 올라 있는 계정
    pro_map = {p: pl for p, pl in zip(a["puuid"], a["player"]) if p not in shared}
    start, end = period()
    log.info("기간 %s ~ %s, 계정 %d개", datetime.fromtimestamp(start, KST).date(),
             datetime.fromtimestamp(end - 1, KST).date(), len(pro_map))
    if not {"ids", "matches"} & steps:
        return

    all_ids = []
    for i, pu in enumerate(pro_map, 1):
        all_ids += match_ids(riot, pu, start, end)
        if i % 10 == 0:
            log.info("경기 id: 계정 %d/%d, 누적 %d", i, len(pro_map), len(set(all_ids)))
    all_ids = list(dict.fromkeys(all_ids))
    cached = sum((RIOT / "matches" / f"{m}.json.gz").exists() for m in all_ids)
    log.info("경기 %d개 (이미 받음 %d, 새로 받을 것 %d — 개발 키로 시간당 약 3,000개)", len(all_ids), cached,
             len(all_ids) - cached)
    if "matches" not in steps:
        return

    matches = []
    for i, mid in enumerate(all_ids, 1):
        d = match(riot, mid)
        if d and d["info"].get("participants"):
            matches.append(d)
        if i % 200 == 0:
            log.info("경기 상세: %d/%d", i, len(all_ids))
    parts, _ = flatten(matches, pro_map)
    own = parts[parts["pro_player"].notna()].copy()
    own = own[(own["game_start_kst"] >= datetime.fromtimestamp(start, KST).strftime("%Y-%m-%d"))
              & (own["game_start_kst"] < datetime.fromtimestamp(end, KST).strftime("%Y-%m-%d"))]
    own.insert(4, "research_patch", label_patch(own["game_start_kst"]))
    check = account_check(own, accts)
    save_csv(check, PROCESSED / "g_research_account_check.csv")
    bad = set(check.loc[check["suspect"], "puuid"])
    own["suspect_account"] = own["puuid"].isin(bad)
    save_csv(own, PROCESSED / "g_research_soloq.csv")
    ok = own[~own["is_remake"] & ~own["suspect_account"]]
    save_csv(champs(ok), PROCESSED / "g_research_soloq_champs.csv")
    per = ok.groupby("pro_player").agg(games=("matchId", "size"), first=("game_start_kst", "min"))
    log.info("선수 %d명, 경기 %d (다시하기·의심 계정 제외), 의심 계정 %d개", len(per), len(ok), len(bad))
    log.info("경기 적은 선수 (100 미만): %s", per[per["games"] < 100].to_dict("index"))


if __name__ == "__main__":
    main()
