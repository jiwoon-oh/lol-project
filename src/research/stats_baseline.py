"""S6 대조용 기록 수치표 — OE 경기 기록 + 밴픽 순서 (코드만, 토큰 0).

판정 기준이 아니라 프로필 "기록 대조" 칸의 참고 자료.
입력  data/processed/2026/a_player_games.csv, a_team_games.csv, b_draft_actions.csv (src/pipeline 산출물, LCK 2026)
출력  research/profiles/stats/{player}.md, {player}.json

- 같은 포지션 LCK 선수(MIN_GAMES 경기 이상) 대비 z 점수: 지표별 선수 평균을 포지션 안에서 표준화
- 패치별: 대회 패치(26.xx) 단위 — 리서치 창 안 경기만 (tournament_patches.csv, 26.16 플레이오프는 검증용으로 제외)
- 시기별: split(+플레이오프) 단위 (참고)
- 챔피언별: 경기·승률·KDA·딜 비중·15분 골드 차·팀 픽 순서(1~5)·1페이즈(1~3번째) 비율
- 결장: 팀 경기 중 선수가 없는 경기

사용: python stats_baseline.py [--players Doran,Oner,Faker]
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from common import DATA, RESEARCH, get_logger, players
from patch_windows import patch_of, read_patch_csv, to_riot, windows

log = get_logger("stats")
PROC = DATA / "processed" / "2026"
OUT = RESEARCH / "profiles" / "stats"
MIN_GAMES = 20
METRICS = {  # 칸 이름: (설명, 높을수록 좋은가)
    "kda": ("KDA", True), "kp": ("킬 관여율", True), "deaths": ("경기당 데스", False),
    "damageshare": ("팀 내 딜 비중", True), "dpm": ("분당 딜", True), "earnedgoldshare": ("팀 내 골드 비중", True),
    "vspm": ("분당 시야 점수", True), "golddiffat10": ("10분 골드 차", True), "golddiffat15": ("15분 골드 차", True),
    "csdiffat15": ("15분 CS 차", True), "xpdiffat15": ("15분 경험치 차", True),
}


def load() -> pd.DataFrame:
    pg = read_patch_csv(PROC / "a_player_games.csv")
    tg = read_patch_csv(PROC / "a_team_games.csv")[["gameid", "teamname", "kills", "opp_teamname"]]
    tg = tg.rename(columns={"kills": "team_kills", "opp_teamname": "opponent"})
    pg = pg.merge(tg, on=["gameid", "teamname"], how="left")
    pg["kp"] = (pg["kills"] + pg["assists"]) / pg["team_kills"].where(pg["team_kills"] > 0)
    pg["period"] = pg["split"] + pg["playoffs"].map({0: "", 1: " PO"})
    pg["date_kst"] = (pd.to_datetime(pg["date"]) + pd.Timedelta(hours=9)).dt.date.astype(str)
    pg["patch"] = pg["patch"].map(to_riot)  # 16.xx → 26.xx
    wins = windows("research")
    pg = pg[pg["date_kst"].map(lambda d: patch_of(d, wins) is not None)]  # 리서치 창 안 경기만 (26.16 플레이오프·월즈 제외)
    # 밴픽 순서 붙이기: 같은 팀·챔피언·KST 날짜의 픽
    da = read_patch_csv(PROC / "b_draft_actions.csv")
    da = da[da["action"] == "pick"].copy()
    da["date_kst"] = da["date_kst"].str[:10]
    da = da[["team", "champion", "date_kst", "side_order"]].drop_duplicates(["team", "champion", "date_kst"])
    pg = pg.merge(da.rename(columns={"team": "teamname", "side_order": "pick_slot"}),
                  on=["teamname", "champion", "date_kst"], how="left")
    return pg


def kda(df: pd.DataFrame) -> float:
    return round((df["kills"].sum() + df["assists"].sum()) / max(1, df["deaths"].sum()), 2)


def summarize(df: pd.DataFrame) -> dict:
    return {"games": len(df), "winrate": round(df["result"].mean(), 3), "kda": kda(df),
            "kp": round(df["kp"].mean(), 3), "damageshare": round(df["damageshare"].mean(), 3),
            "golddiffat15": round(df["golddiffat15"].mean()), "csdiffat15": round(df["csdiffat15"].mean(), 1),
            "vspm": round(df["vspm"].mean(), 2)}


def position_z(pg: pd.DataFrame, name: str, pos: str) -> dict:
    p = pg[pg["position"] == pos]
    agg = p.groupby("playername").agg(games=("gameid", "count"), kills=("kills", "sum"), assists=("assists", "sum"),
                                      deaths_sum=("deaths", "sum"),
                                      **{k: (k, "mean") for k in METRICS if k not in ("kda", "deaths")})
    agg["kda"] = (agg["kills"] + agg["assists"]) / agg["deaths_sum"].clip(lower=1)
    agg["deaths"] = agg["deaths_sum"] / agg["games"]
    agg = agg[(agg["games"] >= MIN_GAMES) | (agg.index == name)]  # 비교군은 20경기 이상, 대상 선수는 적어도 포함
    out = {}
    for k, (label, higher) in METRICS.items():
        col = agg[k]
        z = (col[name] - col.mean()) / col.std()
        rank = int(col.rank(ascending=not higher)[name])
        out[k] = {"label": label, "value": round(float(col[name]), 3), "pos_mean": round(float(col.mean()), 3),
                  "z": round(float(z), 2), "rank": rank, "of": len(agg)}
    return out


def build(pg: pd.DataFrame, pid: str, pcfg: dict) -> dict:
    me = pg[pg["playername"] == pid]
    pos = me["position"].mode()[0]
    team = me["teamname"].mode()[0]
    team_games = pg[(pg["teamname"] == team) & (pg["position"] == pos)]
    missed = team_games[team_games["playername"] != pid]
    champs = (me.groupby("champion").apply(lambda d: pd.Series({
        **summarize(d), "pick_slot": round(d["pick_slot"].mean(), 1) if d["pick_slot"].notna().any() else None,
        "phase1_share": round((d["pick_slot"] <= 3).mean(), 2) if d["pick_slot"].notna().any() else None,
    }), include_groups=False).sort_values("games", ascending=False))
    by_patch = [{"patch": pt, **summarize(d)} for pt, d in me.groupby("patch")]
    periods = [{"period": per, "from": d["date_kst"].min(), "to": d["date_kst"].max(), **summarize(d)}
               for per, d in me.groupby("period", sort=False)]
    return {
        "player": pid, "team": team, "position": pos,
        "overall": summarize(me),
        "team_games": int(team_games["gameid"].nunique()),
        "missed": [{"date": r.date_kst, "period": r.period, "opponent": r.opponent, "replacement": r.playername}
                   for r in missed.itertuples()],
        "pick_slot_mean": round(me["pick_slot"].mean(), 2),
        "phase1_share": round((me["pick_slot"] <= 3).mean(), 2),
        "position_z": position_z(pg, pid, pos),
        "patches": by_patch,
        "periods": periods,
        "champions": champs.reset_index().to_dict("records"),
    }


def to_md(s: dict, pcfg: dict) -> str:
    o = s["overall"]
    L = [f"# {pcfg['name_ko']} ({s['team']} · {s['position']}) — 2026 LCK 기록 기준표",
         "", "출처: Oracle's Elixir 2026 (LCK 1부, 국제대회 제외) + Leaguepedia 밴픽 순서. 리서치 구간 26.01~26.16 정규시즌만 "
             "(26.16 플레이오프는 모델 검증용으로 제외). 판정 기준이 아니라 대조용.", "",
         f"- 출전 {o['games']}경기 / 팀 {s['team_games']}경기 → 결장 {len(s['missed'])}경기"
         + (": " + ", ".join(f"{d} {opp} {n}세트({rep} 출전)" for (d, opp, rep), n in
                              pd.Series([(m["date"], m["opponent"], m["replacement"]) for m in s["missed"]]).value_counts(sort=False).items())
            if s["missed"] else ""),
         f"- 승률 {o['winrate']:.0%}, KDA {o['kda']}, 킬 관여 {o['kp']:.0%}, 딜 비중 {o['damageshare']:.0%}, "
         f"15분 골드 차 {o['golddiffat15']:+d}",
         f"- 팀 픽 순서 평균 {s['pick_slot_mean']} (1~5), 1페이즈(1~3번째) 픽 비율 {s['phase1_share']:.0%}", "",
         f"## 같은 포지션 대비 (LCK {s['position']} {MIN_GAMES}경기 이상, z 점수)", "",
         "| 지표 | 값 | 포지션 평균 | z | 순위 |", "|---|---|---|---|---|"]
    for k, m in s["position_z"].items():
        fmt = (lambda v: f"{v:.0%}") if k in ("kp", "damageshare", "earnedgoldshare") else               (lambda v: f"{v:+.0f}") if k.endswith(("at10", "at15")) and not k.startswith("cs") else               (lambda v: f"{v:+.1f}") if k.startswith("cs") else (lambda v: f"{v:.2f}")
        L.append(f"| {m['label']} | {fmt(m['value'])} | {fmt(m['pos_mean'])} | {m['z']:+.2f} | {m['rank']}/{m['of']} |")
    L += ["", "## 패치별 (대회 패치, 리서치 구간)", "", "| 패치 | 경기 | 승률 | KDA | 킬 관여 | 딜 비중 | 15분 골드 차 | 15분 CS 차 | 시야/분 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for p in s["patches"]:
        L.append(f"| {p['patch']} | {p['games']} | {p['winrate']:.0%} | {p['kda']} | {p['kp']:.0%} | "
                 f"{p['damageshare']:.0%} | {p['golddiffat15']:+d} | {p['csdiffat15']:+.1f} | {p['vspm']} |")
    L += ["", "## 시기별 (참고)", "", "| 시기 | 기간(KST) | 경기 | 승률 | KDA | 킬 관여 | 딜 비중 | 15분 골드 차 | 15분 CS 차 | 시야/분 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for p in s["periods"]:
        L.append(f"| {p['period']} | {p['from'][5:]}~{p['to'][5:]} | {p['games']} | {p['winrate']:.0%} | {p['kda']} | {p['kp']:.0%} | "
                 f"{p['damageshare']:.0%} | {p['golddiffat15']:+d} | {p['csdiffat15']:+.1f} | {p['vspm']} |")
    L += ["", "## 챔피언 (3경기 이상)", "", "| 챔피언 | 경기 | 승률 | KDA | 딜 비중 | 15분 골드 차 | 팀 픽 순서 | 1페이즈 비율 |",
          "|---|---|---|---|---|---|---|---|"]
    for c in s["champions"]:
        if c["games"] < 3:
            continue
        L.append(f"| {c['champion']} | {int(c['games'])} | {c['winrate']:.0%} | {c['kda']} | {c['damageshare']:.0%} | "
                 f"{int(c['golddiffat15']):+d} | {c['pick_slot']} | "
                 f"{'' if c['phase1_share'] is None else format(c['phase1_share'], '.0%')} |")
    rest = [c for c in s["champions"] if c["games"] < 3]
    if rest:
        L += ["", "그 외 1~2경기: " + ", ".join(f"{c['champion']} {int(c['games'])}경기 {c['winrate']:.0%}" for c in rest)]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", default=None)
    args = ap.parse_args()
    pg = load()
    allp = players()
    OUT.mkdir(parents=True, exist_ok=True)
    for pid in (args.players.split(",") if args.players else list(allp)):
        s = build(pg, pid, allp[pid])
        (OUT / f"{pid}.json").write_text(json.dumps(s, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
        (OUT / f"{pid}.md").write_text(to_md(s, allp[pid]), encoding="utf-8")
        log.info("%s: %d경기, 결장 %d, 챔피언 %d종, 픽 순서 매칭 %.0f%%", pid, s["overall"]["games"], len(s["missed"]),
                 len(s["champions"]), 100 * pg[pg.playername == pid]["pick_slot"].notna().mean())


if __name__ == "__main__":
    main()
