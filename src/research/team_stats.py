"""팀 기록 기준표 — 밴픽 성향·운영·자주 쓰는 조합 (코드만, 토큰 0).

팀 프로필(S6 팀)의 "기록" 쪽. 리서치 창 안 경기만 (26.16 플레이오프·월즈 제외).
입력  data/processed/2026/b_draft_games.csv (밴픽 순서), a_team_games.csv (운영 지표), d_champions.csv (태그)
출력  research/profiles/team_stats/{team}.md, {team}.json

1. 개요: 경기·승률, 진영별, 패치별
2. 밴픽 성향
   - 선택권(First Selection)을 가졌을 때 픽 순서 vs 진영 중 무엇을 골랐나, 선픽/후픽·블루/레드 승률
   - 밴: 많이 한 밴, 1페이즈/2페이즈 밴, 상대가 이 팀에게 한 밴(존중받는 픽), T1 상대로 한 밴
   - 픽: 1페이즈(팀 1~3번째) 우선 픽, 첫 픽 포지션, 마지막 픽 포지션(카운터 자리), 여러 포지션 픽(플렉스)
3. 자주 쓰는 조합: 포지션 2인 조합(정글-미드, 원딜-서폿, 탑-정글, 미드-서폿), 3인(정글-미드-서폿), 같은 5인 조합,
   조합 성격(탱커 이니시 챔피언 수(d_champions is_engage, 15종), AP 수, 원거리 수)별 승률
4. 운영: 첫 킬·첫 드래곤·첫 전령·첫 포탑·첫 바론 비율과 그때 승률, 10·15분 골드 차, 경기 시간,
   역전승(15분 −1000 이하에서 승)·역전패(+1000 이상에서 패)

사용: python team_stats.py [--teams T1,Gen.G]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from itertools import combinations

import pandas as pd

from common import DATA, RESEARCH, get_logger, load_config
from patch_windows import patch_of, read_patch_csv, to_riot, windows

log = get_logger("team")
PROC = DATA / "processed" / "2026"
OUT = RESEARCH / "profiles" / "team_stats"
ROLES = ["Top", "Jungle", "Mid", "Bot", "Support"]
ROLE_KO = {"Top": "탑", "Jungle": "정글", "Mid": "미드", "Bot": "원딜", "Support": "서폿"}
MIN_N = 3  # 조합·챔피언 표에 넣는 최소 경기 수


def slug(team: str) -> str:
    return team.replace(" ", "_").replace(".", "")


def wr(s: pd.Series) -> str:
    return f"{s.mean():.0%}" if len(s) else "-"


def load():
    wins = windows("research")
    dg = read_patch_csv(PROC / "b_draft_games.csv")
    dg["day"] = dg["date_kst"].str[:10]
    dg = dg[dg["day"].map(lambda d: patch_of(d, wins) is not None)].copy()
    dg["patch"] = dg["day"].map(lambda d: patch_of(d, wins))
    tg = read_patch_csv(PROC / "a_team_games.csv")
    tg["day"] = (pd.to_datetime(tg["date"]) + pd.Timedelta(hours=9)).dt.date.astype(str)
    tg = tg[tg["day"].map(lambda d: patch_of(d, wins) is not None)].copy()
    tg["patch"] = tg["patch"].map(to_riot)
    ch = pd.read_csv(PROC / "d_champions.csv")
    tags = {r.name_en: r for r in ch.itertuples()}
    return dg, tg, tags


def team_draft_rows(dg: pd.DataFrame, team: str) -> list[dict]:
    """경기마다 이 팀 시점의 밴·픽 (순서·포지션 포함)."""
    rows = []
    for r in dg[(dg["fp_team"] == team) | (dg["lp_team"] == team)].itertuples():
        me, opp = ("fp", "lp") if r.fp_team == team else ("lp", "fp")
        g = r._asdict()
        side = "Blue" if r.blue_team == team else "Red"
        picks = [(i, g[f"{me}_pick{i}"], g[f"{me}_pick{i}_role"]) for i in range(1, 6)]
        rows.append({
            "game": r.GameId, "day": r.day, "patch": r.patch, "opponent": g[f"{opp}_team"], "side": side,
            "first_pick": me == "fp", "had_fs": r.fs_team == team, "fs_choice": r.fs_choice if r.fs_team == team else None,
            "win": r.winner_team == team,
            "bans": [(i, g[f"{me}_ban{i}"]) for i in range(1, 6) if isinstance(g[f"{me}_ban{i}"], str)],
            "opp_bans": [g[f"{opp}_ban{i}"] for i in range(1, 6) if isinstance(g[f"{opp}_ban{i}"], str)],
            "picks": picks, "by_role": {role: champ for _, champ, role in picks if isinstance(role, str)},
        })
    return rows


def top_counts(items, n=10) -> str:
    c = Counter(items)
    return ", ".join(f"{k} {v}" for k, v in c.most_common(n)) or "-"


def champ_wr(rows, key) -> list[tuple]:
    """(챔피언, 경기, 승률) — key(row) 가 그 경기의 챔피언 목록."""
    games, wins_ = Counter(), Counter()
    for r in rows:
        for c in key(r):
            games[c] += 1
            wins_[c] += r["win"]
    return sorted(((c, n, wins_[c] / n) for c, n in games.items()), key=lambda x: -x[1])


def build(team: str, dg, tg, tags) -> dict:
    R = team_draft_rows(dg, team)
    T = tg[tg["teamname"] == team]
    df = pd.DataFrame(R)
    out = {"team": team, "games": len(R)}
    # 1. 개요
    out["overall"] = {"winrate": round(df["win"].mean(), 3),
                      "blue": [int((df.side == "Blue").sum()), wr(df[df.side == "Blue"]["win"])],
                      "red": [int((df.side == "Red").sum()), wr(df[df.side == "Red"]["win"])]}
    out["by_patch"] = [{"patch": p, "games": len(d), "winrate": wr(d["win"]),
                        "top_picks": top_counts([c for r in d.itertuples() for _, c, _ in r.picks], 5),
                        "top_bans": top_counts([c for r in d.itertuples() for _, c in r.bans], 5)}
                       for p, d in df.groupby("patch")]
    # 2. 밴픽 성향
    fs = df[df.had_fs]
    out["first_selection"] = {"had": len(fs), "chose_pick": int((fs.fs_choice == "pick").sum()),
                              "chose_side": int((fs.fs_choice == "side").sum()),
                              "side_when_chose_side": top_counts(fs[fs.fs_choice == "side"]["side"]),
                              "fp_games": [int(df.first_pick.sum()), wr(df[df.first_pick]["win"])],
                              "lp_games": [int((~df.first_pick).sum()), wr(df[~df.first_pick]["win"])]}
    out["bans"] = {"all": top_counts([c for r in R for _, c in r["bans"]], 15),
                   "phase1": top_counts([c for r in R for i, c in r["bans"] if i <= 3], 10),
                   "phase2": top_counts([c for r in R for i, c in r["bans"] if i > 3], 10),
                   "received": top_counts([c for r in R for c in r["opp_bans"]], 15),
                   "vs_T1": top_counts([c for r in R if r["opponent"] == "T1" for _, c in r["bans"]], 10)
                   if team != "T1" else "-"}
    p1 = champ_wr(R, lambda r: [c for i, c, _ in r["picks"] if i <= 3])
    out["picks"] = {
        "phase1": [(c, n, f"{w:.0%}") for c, n, w in p1 if n >= MIN_N][:15],
        "first_pick_role": top_counts([ROLE_KO.get(r["picks"][0][2], "?") for r in R if r["first_pick"]]),
        "last_pick_role": top_counts([ROLE_KO.get(r["picks"][4][2], "?") for r in R]),
        "by_role": {ROLE_KO[ro]: [(c, n, f"{w:.0%}") for c, n, w in champ_wr(R, lambda r, ro=ro: [r["by_role"].get(ro)] if r["by_role"].get(ro) else []) if n >= MIN_N][:8]
                    for ro in ROLES},
        "flex": sorted({c for c in {c for r in R for c in r["by_role"].values()}
                        if len({ro for r in R for ro, cc in r["by_role"].items() if cc == c}) > 1}),
    }
    # 3. 조합
    pairs = {"정글-미드": ("Jungle", "Mid"), "원딜-서폿": ("Bot", "Support"), "탑-정글": ("Top", "Jungle"),
             "미드-서폿": ("Mid", "Support"), "정글-서폿": ("Jungle", "Support")}
    out["duos"] = {}
    for name, (a, b) in pairs.items():
        cw = champ_wr(R, lambda r, a=a, b=b: [f"{r['by_role'].get(a)} + {r['by_role'].get(b)}"]
                      if r["by_role"].get(a) and r["by_role"].get(b) else [])
        out["duos"][name] = [(c, n, f"{w:.0%}") for c, n, w in cw if n >= 2][:8]
    trio = champ_wr(R, lambda r: [" + ".join(r["by_role"].get(x, "?") for x in ("Jungle", "Mid", "Support"))])
    out["trio_jng_mid_sup"] = [(c, n, f"{w:.0%}") for c, n, w in trio if n >= 2][:8]
    full = champ_wr(R, lambda r: [" / ".join(r["by_role"].get(x, "?") for x in ROLES)])
    out["full_comps_repeated"] = [(c, n, f"{w:.0%}") for c, n, w in full if n >= 2][:10]

    def archetype(r):
        cs = [tags.get(c) for c in r["by_role"].values()]
        cs = [c for c in cs if c is not None]
        eng = sum(bool(getattr(c, "is_engage", False)) for c in cs)
        ap = sum(str(getattr(c, "damage_type", "")).lower().startswith(("ap", "magic")) for c in cs)
        rng = sum(bool(getattr(c, "is_ranged", False)) for c in cs)
        return f"탱커 이니시 {min(eng, 3)}{'+' if eng >= 3 else ''} · AP {ap} · 원거리 {rng}"  # is_engage = 탱커형 15종만
    arch = champ_wr(R, lambda r: [archetype(r)])
    out["archetypes"] = [(c, n, f"{w:.0%}") for c, n, w in arch if n >= MIN_N][:10]
    # 4. 운영
    def rate(col):
        s = T[col].dropna()
        got = T[T[col] == 1]
        return {"rate": f"{s.mean():.0%}" if len(s) else "-", "win_when": wr(got["result"]) if len(got) else "-"}
    gd15 = T["golddiffat15"]
    gl = pd.to_numeric(T["gamelength"], errors="coerce") / 60
    out["macro"] = {k: rate(k) for k in ("firstblood", "firstdragon", "firstherald", "firsttower", "firstbaron")}
    out["macro"] |= {"gd10": round(T["golddiffat10"].mean()), "gd15": round(gd15.mean()),
                     "length_win_min": round(gl[T.result == 1].mean(), 1), "length_loss_min": round(gl[T.result == 0].mean(), 1),
                     "comeback": f"{int(((gd15 <= -1000) & (T.result == 1)).sum())}/{int((gd15 <= -1000).sum())}",
                     "throw": f"{int(((gd15 >= 1000) & (T.result == 0)).sum())}/{int((gd15 >= 1000).sum())}"}
    return out


def league_ranks(stats: dict) -> dict:
    """운영 지표 리그 순위 (10팀 중)."""
    df = pd.DataFrame({t: {"gd15": s["macro"]["gd15"], "gd10": s["macro"]["gd10"]} for t, s in stats.items()}).T
    return {t: {k: int(df[k].rank(ascending=False)[t]) for k in df} for t in stats}


def to_md(s: dict, ko: str, ranks: dict) -> str:
    fmt = lambda xs: ", ".join(f"{c} {n}경기 {w}" for c, n, w in xs) or "-"
    m, f = s["macro"], s["first_selection"]
    L = [f"# {ko} ({s['team']}) — 2026 LCK 팀 기록 기준표",
         "", "출처: Leaguepedia 밴픽 순서 + Oracle's Elixir. 리서치 구간(26.01~26.16 정규)만, 26.16 플레이오프 제외. 대조용.", "",
         f"## 1. 개요\n- {s['games']}경기, 승률 {s['overall']['winrate']:.0%} · 블루 {s['overall']['blue'][0]}경기 {s['overall']['blue'][1]} · 레드 {s['overall']['red'][0]}경기 {s['overall']['red'][1]}",
         "", "| 패치 | 경기 | 승률 | 많이 한 픽 | 많이 한 밴 |", "|---|---|---|---|---|",
         *[f"| {p['patch']} | {p['games']} | {p['winrate']} | {p['top_picks']} | {p['top_bans']} |" for p in s["by_patch"]],
         "", "## 2. 밴픽 성향",
         f"- 선택권 {f['had']}회 → 픽 순서 {f['chose_pick']} / 진영 {f['chose_side']} (진영을 골랐을 때: {f['side_when_chose_side']})",
         f"- 선픽 {f['fp_games'][0]}경기 승률 {f['fp_games'][1]} · 후픽 {f['lp_games'][0]}경기 승률 {f['lp_games'][1]}",
         f"- 밴 전체: {s['bans']['all']}", f"- 1페이즈 밴: {s['bans']['phase1']}", f"- 2페이즈 밴: {s['bans']['phase2']}",
         f"- 상대가 이 팀에게 한 밴 (존중받는 픽): {s['bans']['received']}", f"- T1 상대로 한 밴: {s['bans']['vs_T1']}",
         f"- 1페이즈(팀 1~3번째) 픽: {fmt(s['picks']['phase1'])}", f"- 첫 픽 포지션: {s['picks']['first_pick_role']}",
         f"- 마지막 픽 포지션 (카운터 자리): {s['picks']['last_pick_role']}",
         f"- 여러 포지션으로 쓴 챔피언 (플렉스): {', '.join(s['picks']['flex']) or '-'}",
         "", "| 포지션 | 많이 쓴 챔피언 (경기, 승률) |", "|---|---|",
         *[f"| {ro} | {fmt(v)} |" for ro, v in s["picks"]["by_role"].items()],
         "", "## 3. 자주 쓰는 조합 (2경기 이상)", "", "| 조합 | 챔피언 (경기, 승률) |", "|---|---|",
         *[f"| {k} | {fmt(v)} |" for k, v in s["duos"].items()],
         f"| 정글-미드-서폿 | {fmt(s['trio_jng_mid_sup'])} |",
         f"| 같은 5인 조합 (탑/정글/미드/원딜/서폿) | {fmt(s['full_comps_repeated'])} |",
         f"| 조합 성격 ({MIN_N}경기 이상) | {fmt(s['archetypes'])} |",
         "", "## 4. 운영", "", "| 지표 | 비율 | 그때 승률 |", "|---|---|---|",
         *[f"| {ko} | {m[k]['rate']} | {m[k]['win_when']} |" for k, ko in (("firstblood", "첫 킬"), ("firstdragon", "첫 드래곤"),
            ("firstherald", "첫 전령"), ("firsttower", "첫 포탑"), ("firstbaron", "첫 바론"))],
         "", f"- 10분 골드 차 {m['gd10']:+d} (리그 {ranks['gd10']}위), 15분 {m['gd15']:+d} (리그 {ranks['gd15']}위)",
         f"- 경기 시간: 이길 때 {m['length_win_min']}분, 질 때 {m['length_loss_min']}분",
         f"- 역전승 (15분 −1000 이하에서 승): {m['comeback']} · 역전패 (15분 +1000 이상에서 패): {m['throw']}"]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", default=None)
    a = ap.parse_args()
    teams = load_config("teams")["teams"]
    targets = a.teams.split(",") if a.teams else list(teams)
    dg, tg, tags = load()
    stats = {t: build(t, dg, tg, tags) for t in teams}
    ranks = league_ranks(stats)
    OUT.mkdir(parents=True, exist_ok=True)
    for t in targets:
        (OUT / f"{slug(t)}.json").write_text(json.dumps(stats[t], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        (OUT / f"{slug(t)}.md").write_text(to_md(stats[t], teams[t]["name_ko"], ranks[t]), encoding="utf-8")
        log.info("%s: %d경기", t, stats[t]["games"])


if __name__ == "__main__":
    main()
