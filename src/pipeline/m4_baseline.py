"""M4 기준선 (2026-11-06, docs/PIPELINE.md 7-7 "M4 전 보완" D4)

M4(픽 선택)를 만들기 전에 단순한 방법으로 LCK 밴·픽을 얼마나 맞히는지 잼 → M4 가 넘어야 할 선.
평가 = 드래프트 단계마다 고를 수 있는 챔피언(이번 경기에 이미 픽·밴, 같은 매치 앞 세트에 이미 픽 = 피어리스 제외) 중
       실제 선택이 점수 상위 1·3·5 안에 드는 비율 (밴·픽 따로) + 평균 역순위(MRR)
리허설 세트 (D3, 2026-11-06 결정): LCK 26.14 · 26.15 · Road to MSI(06-06~14). 최종 검증(플레이오프 26.16)은 보지 않음
그 시점 자료만: 각 경기 날짜 '전날까지'의 경기 (LCK 은 앞 경기, 다른 리그 대회 기록은 08-25 까지 — D2 결정)

기준선
  B0 무작위
  B1 메타: 최근 21일 대회(5리그) 픽+밴 수 (없으면 시즌 전체)
  B2 메타 + 빈 포지션: 픽은 아직 안 채운 포지션의 그 챔피언 픽 수만 (밴은 B1)
  B3 + 팀 습관: 그 팀이 지금까지 뽑은(밴한) 챔피언 비율을 섞음
  B4 + 선수 풀: 픽 = 빈 포지션 우리 선수의 대회 판수 + 최근 14일 솔랭 판수, 밴 = 상대 선수들의 같은 값 (타깃 밴)
섞는 비율은 고정 (각 신호를 확률로 바꿔 더함, 가중치 W). 이 단계는 '기준선'이라 비율을 고르지 않음

    python src/pipeline/m4_baseline.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
from d_champion_meta import norm  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m4"
OUT.mkdir(exist_ok=True)
VALID_START = "2026-08-26"     # 이 날부터는 최종 검증 — 기준선 계산에 절대 안 씀
OTHER_END = "2026-08-25"       # 다른 리그 대회 기록은 여기까지 (D2)
ROLE = {"Top": "TOP", "Jungle": "JUNGLE", "Mid": "MIDDLE", "Bot": "BOTTOM", "Support": "UTILITY"}
META_DAYS, SOLO_DAYS = 21, 14
W = {"meta": 1.0, "team": 1.0, "player": 1.0, "solo": 1.0}


def cid_map():
    nm = pd.read_csv(PROCESSED / "d_champion_name_map.csv").set_index("alias")["champion_id"]
    return lambda s: nm.get(norm(s)) if isinstance(s, str) else None


# ---------------------------------------------------------------- 자료
def load():
    cid = cid_map()
    a = pd.read_csv(PROCESSED / "b_draft_actions.csv")
    g = pd.read_csv(PROCESSED / "b_draft_games.csv")
    a = a[a["date_kst"].str[:10] < VALID_START].copy()
    a["cid"] = a["champion"].map(cid)
    a["role"] = a["role"].map(ROLE)
    a["day"] = a["date_kst"].str[:10]
    a = a.merge(g[["GameId", "MatchId", "N_GameInMatch", "OverviewPage", "RiotPlatformGameId"]], on="GameId", how="left")
    # 대회 기록 (5리그): 픽·밴, 선수 × 챔피언
    oe = pd.read_csv(M1.OE_FILE, low_memory=False,
                     usecols=["gameid", "league", "date", "side", "position", "playername", "teamname", "champion"]
                     + [f"ban{i}" for i in range(1, 6)])
    oe = oe[oe["league"].isin(M1.LEAGUES) & (oe["date"].str[:10] <= OTHER_END)]
    oe["day"] = oe["date"].str[:10]
    pl = oe[oe["position"].isin(H.OE_POS)].copy()
    pl["cid"] = pl["champion"].map(cid)
    pl["role"] = pl["position"].map(H.OE_POS)
    tm = oe[oe["position"] == "team"]
    bans = tm.melt(id_vars=["gameid", "day", "league", "teamname"], value_vars=[f"ban{i}" for i in range(1, 6)], value_name="b")
    bans["cid"] = bans["b"].map(cid)
    pro_pick = pl[["gameid", "day", "league", "teamname", "playername", "role", "cid"]].dropna(subset=["cid"])
    pro_ban = bans[["gameid", "day", "league", "teamname", "cid"]].dropna(subset=["cid"])
    lineup = pl.set_index(["gameid", "teamname", "role"])["playername"]
    # 솔랭 (리서치 선수)
    sq = pd.read_csv(PROCESSED / "g_research_soloq_games.csv", usecols=["player", "game_start_kst", "championName"], low_memory=False)
    sq["day"] = sq["game_start_kst"].astype(str).str[:10]
    sq["cid"] = sq["championName"].map(cid)
    sq = sq.dropna(subset=["cid"])
    sq = sq[sq["day"] < VALID_START]
    champs = sorted(set(pro_pick["cid"]) | set(pro_ban["cid"]) | set(a["cid"].dropna()))
    return a, pro_pick, pro_ban, lineup, sq, champs


def eval_sets(a: pd.DataFrame) -> dict:
    w = H.research_windows().set_index("patch")
    g = a.drop_duplicates("GameId")
    rtm = g[g["OverviewPage"].astype(str).str.contains("Road to MSI")]["GameId"]
    sets = {"26.14": g[(g["Patch"].astype(str) == "26.14") & ~g["GameId"].isin(rtm)]["GameId"],
            "26.15": g[(g["Patch"].astype(str) == "26.15") & ~g["GameId"].isin(rtm)]["GameId"],
            "RTM": rtm}
    return {k: set(v) for k, v in sets.items()}


# ---------------------------------------------------------------- 점수
def norm_counts(c: Counter, champs, alpha=0.5) -> dict:
    tot = sum(c.values()) + alpha * len(champs)
    return {k: (c.get(k, 0) + alpha) / tot for k in champs}


class Context:
    """한 경기 날짜 기준 '전날까지' 자료 (경기마다 다시 만듦)."""

    def __init__(self, day, team, opp, game, pro_pick, pro_ban, lineup, sq, a, champs):
        prev = pro_pick[pro_pick["day"] < day]
        prevb = pro_ban[pro_ban["day"] < day]
        lo = (pd.Timestamp(day) - pd.Timedelta(days=META_DAYS)).strftime("%Y-%m-%d")
        rp, rb = prev[prev["day"] >= lo], prevb[prevb["day"] >= lo]
        if len(rp) < 200:  # 최근 경기가 적으면 시즌 전체
            rp, rb = prev, prevb
        self.champs = champs
        self.meta = norm_counts(Counter(rp["cid"]) + Counter(rb["cid"]), champs)
        self.meta_role = {r: norm_counts(Counter(rp.loc[rp["role"] == r, "cid"]), champs) for r in M1.POS}
        la = a[(a["day"] < day)]
        self.team_pick = norm_counts(Counter(la.loc[(la["team"] == team) & (la["action"] == "pick"), "cid"]), champs)
        self.team_ban = norm_counts(Counter(la.loc[(la["team"] == team) & (la["action"] == "ban"), "cid"]), champs)
        so = (pd.Timestamp(day) - pd.Timedelta(days=SOLO_DAYS)).strftime("%Y-%m-%d")
        self.players = {}
        for side_team, key in ((team, "us"), (opp, "them")):
            for r in M1.POS:
                try:
                    p = lineup.loc[(game, side_team, r)]
                except KeyError:
                    p = None
                if isinstance(p, pd.Series):
                    p = p.iloc[0]
                if p is None:
                    continue
                pc = Counter(prev.loc[prev["playername"] == p, "cid"])
                sc = Counter(sq.loc[(sq["player"] == p) & (sq["day"] < day) & (sq["day"] >= so), "cid"])
                self.players[(key, r)] = (norm_counts(pc, champs), norm_counts(sc, champs), sum(pc.values()), sum(sc.values()))


def scores(method, action, ctx: Context, open_roles) -> dict:
    ch = ctx.champs
    if method == "B0":
        return {c: 0.0 for c in ch}
    if method == "B1" or (method == "B2" and action == "ban"):
        return ctx.meta
    if action == "pick":
        base = {c: sum(ctx.meta_role[r][c] for r in open_roles) for c in ch}
    else:
        base = ctx.meta
    if method == "B2":
        return base
    team = ctx.team_pick if action == "pick" else ctx.team_ban
    s = {c: W["meta"] * base[c] / max(1, len(open_roles) if action == "pick" else 1) + W["team"] * team[c] for c in ch}
    if method == "B3":
        return s
    # B4: 선수 풀 (픽 = 우리 빈 포지션 선수, 밴 = 상대 선수들)
    who = [("us", r) for r in open_roles] if action == "pick" else [("them", r) for r in M1.POS]
    for k in who:
        if k in ctx.players:
            pc, sc, npc, nsc = ctx.players[k]
            for c in ch:
                s[c] += (W["player"] * pc[c] + W["solo"] * sc[c]) / len(who)
    return s


# ---------------------------------------------------------------- 평가
def run():
    t0 = time.time()
    a, pro_pick, pro_ban, lineup, sq, champs = load()
    sets = eval_sets(a)
    log.info("드래프트 단계 %d (경기 %d), 평가 세트 %s (%.0f초)", len(a), a["GameId"].nunique(),
             {k: len(v) for k, v in sets.items()}, time.time() - t0)
    methods = ["B0", "B1", "B2", "B3", "B4"]
    rows = []
    fearless_viol = 0
    for name, gids in sets.items():
        for gid in sorted(gids):
            ga = a[a["GameId"] == gid].sort_values("step")
            if ga["cid"].isna().any() or len(ga) != 20:
                continue
            day, mid, n = ga["day"].iloc[0], ga["MatchId"].iloc[0], ga["N_GameInMatch"].iloc[0]
            earlier = a[(a["MatchId"] == mid) & (a["N_GameInMatch"] < n) & (a["action"] == "pick")]
            fearless = set(earlier["cid"])
            game = ga["RiotPlatformGameId"].iloc[0]
            ctx = {}
            used, filled = set(), {}
            for r in ga.itertuples():
                if r.team not in ctx:
                    ctx[r.team] = Context(day, r.team, r.opponent, game, pro_pick, pro_ban, lineup, sq, a, champs)
                avail = [c for c in champs if c not in used and c not in fearless]
                if r.cid not in avail:
                    fearless_viol += 1
                    used.add(r.cid)
                    continue
                open_roles = [p for p in M1.POS if p not in filled.get(r.team, set())]
                for m in methods:
                    s = scores(m, r.action, ctx[r.team], open_roles)
                    if m == "B0":
                        rank = (len(avail) + 1) / 2
                    else:
                        v = s[r.cid]
                        rank = 1 + sum(1 for c in avail if s[c] > v) + 0.5 * sum(1 for c in avail if s[c] == v and c != r.cid)
                    rows.append({"set": name, "GameId": gid, "step": r.step, "action": r.action, "method": m,
                                 "rank": rank, "n_avail": len(avail), "game_in_match": n})
                used.add(r.cid)
                if r.action == "pick" and isinstance(r.role, str):
                    filled.setdefault(r.team, set()).add(r.role)
        log.info("  %s 끝 (%.0f초)", name, time.time() - t0)
    R = pd.DataFrame(rows)
    H.save_csv(R, OUT / "m4_baseline_ranks.csv")
    return R, fearless_viol


def summarize(R: pd.DataFrame, viol: int) -> str:
    def agg(g):
        m_ = g["method"].iloc[0] if "method" in g else (g.name[-1] if isinstance(g.name, tuple) else g.name)
        if m_ == "B0":  # 무작위 = 기대값: top-k = k/후보 수, MRR = (1 + 1/2 + … + 1/n)/n
            n = g["n_avail"].to_numpy(float)
            hn = np.array([np.sum(1 / np.arange(1, int(x) + 1)) for x in n])
            return pd.Series({"단계": len(g), "top1": np.mean(1 / n), "top3": np.mean(3 / n), "top5": np.mean(5 / n),
                              "MRR": np.mean(hn / n), "후보 수": n.mean()})
        return pd.Series({"단계": len(g), "top1": (g["rank"] <= 1).mean(), "top3": (g["rank"] <= 3).mean(),
                          "top5": (g["rank"] <= 5).mean(), "MRR": (1 / g["rank"]).mean(), "후보 수": g["n_avail"].mean()})
    names = {"B0": "무작위", "B1": "메타 (대회 픽+밴, 최근 21일)", "B2": "메타 + 빈 포지션", "B3": "+ 팀 습관", "B4": "+ 선수 풀 (대회·최근 솔랭)"}
    L = ["# M4 기준선 — LCK 밴·픽 예측 (리허설 세트)", "",
         "- 평가: 단계마다 고를 수 있는 챔피언(이번 경기 픽·밴, 같은 매치 앞 세트 픽 제외) 중 실제 선택의 순위. top-k = 상위 k 안 비율, MRR = 평균 역순위",
         "- 자료: 각 경기 '전날까지'. 다른 리그 대회 기록은 08-25 까지. 플레이오프(08-26~) 안 봄",
         f"- 피어리스 규칙과 어긋난 실제 선택 {viol}건 (있으면 같은 매치 앞 세트 정의 확인 필요)", ""]
    for act, ko in (("pick", "픽"), ("ban", "밴")):
        L += [f"## {ko}", "", "| 세트 | 방법 | 단계 | top1 | top3 | top5 | MRR | 후보 수 |", "|---|---|---|---|---|---|---|---|"]
        for (s, m), g in R[R["action"] == act].groupby(["set", "method"]):
            x = agg(g)
            L.append(f"| {s} | {names[m]} | {int(x['단계'])} | {x['top1']:.1%} | {x['top3']:.1%} | {x['top5']:.1%} | {x['MRR']:.3f} | {x['후보 수']:.0f} |")
        x = R[R["action"] == act].groupby("method").apply(agg)
        L += ["", "| 세 세트 합 | top1 | top3 | top5 | MRR |", "|---|---|---|---|---|"]
        for m, r in x.iterrows():
            L.append(f"| {names[m]} | {r['top1']:.1%} | {r['top3']:.1%} | {r['top5']:.1%} | {r['MRR']:.3f} |")
        L += [""]
    # 세트 번호별 (피어리스로 후보가 줄어드는 영향)
    L += ["## 세트 번호별 (B4, 밴·픽 합)", "", "| 매치 안 세트 | 단계 | top5 | 후보 수 |", "|---|---|---|---|"]
    for n, g in R[R["method"] == "B4"].groupby("game_in_match"):
        L.append(f"| {n} | {len(g)} | {(g['rank'] <= 5).mean():.1%} | {g['n_avail'].mean():.0f} |")
    return "\n".join(L) + "\n"


def main():
    if "--report" in sys.argv:  # 저장된 순위로 보고서만
        R = pd.read_csv(OUT / "m4_baseline_ranks.csv")
        (OUT / "m4_baseline_eval.md").write_text(summarize(R, 0), encoding="utf-8")
        return
    R, viol = run()
    (OUT / "m4_baseline_eval.md").write_text(summarize(R, viol), encoding="utf-8")
    log.info("기준선 끝")


if __name__ == "__main__":
    main()
