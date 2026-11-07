"""M4 P4 — 시리즈 단위 피어리스 계획 (2026-11-07, docs/PIPELINE.md "M4")

피어리스 다전제: 한 매치에서 쓴 챔피언은 그 매치 안에서 양 팀 모두 다시 못 씀 → 팀은 주력을 뒤 세트를 위해 아끼거나,
지면 끝나는 세트에서 아끼던 챔피언을 꺼내고, 상대 주력을 먼저 써서 뺏기도 함. 지금 모델은 쓴 챔피언을 빼기만 함.
시리즈 형식 (경기 시점에 알 수 있는 것만): 정규시즌·LCK Cup 플레이인 = 3판 2선승, 플레이오프·결승·RTM = 5판 3선승,
  세트 번호가 3을 넘으면 5판. 매치 점수 = 앞 세트 승자 (그 세트가 끝난 뒤 알려진 정보)
특징 (픽 = 우리 빈 포지션 선수, 밴 = 상대 빈 포지션 선수; 선수 풀 = 대회 ×2 + 솔랭 30일, 이번 매치에 쓴 것·이번 경기 픽·밴 제외)
  p4_core_rem   : 후보가 그 선수 남은 풀 1·2순위 × (최대 남은 세트 − 1)        → 남은 세트가 많으면 주력을 아낌
  p4_last       : 후보가 남은 풀 1~3순위 × [어느 한쪽이 매치 포인트]           → 끝날 수 있는 세트엔 아끼던 주력
  p4_core_depth : 1·2순위 × log(1 + 남은 풀 깊이(상위 10 중 남은 수))           → 풀이 깊으면 바로 씀
  p4_deny       : (픽만) 후보가 상대 빈 포지션 선수 남은 풀 1~3순위 × 최대 남은 세트 → 상대 주력 뺏기
  p4_team_late  : 그 팀이 다전제에서 이 챔피언을 쓴 평균 세트 번호 − 팀 평균, × (지금 세트 − 1.5) (LCK, 전날까지)
기준 = M4 최종 (픽 = 공통 + 이길 가치, 밴 = 1·2페이즈 + 이길 가치 + C2 + P1). 특징 표 캐시 m4/m4_v4_rows.pkl 사용

    python src/pipeline/m4_v5.py
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_features as F  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v1 as V  # noqa: E402
import m4_v3 as V3  # noqa: E402
import m4_v4 as V4  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = B.OUT
P4 = ["p4_core_rem", "p4_last", "p4_core_depth", "p4_deny", "p4_team_late"]


def series_info(a):
    g = pd.read_csv(PROCESSED / "b_draft_games.csv")
    g = g[g["date_kst"] < B.VALID_START]
    info = {}
    for mid, gg in g.groupby("MatchId"):
        gg = gg.sort_values("N_GameInMatch")
        tab, page = str(gg["Tab"].iloc[0]), str(gg["OverviewPage"].iloc[0])
        bo = 5 if (("Play" in tab and "Play-In" not in tab) or "Final" in tab or "Road to MSI" in page) else 3
        wins = defaultdict(int)
        for r in gg.itertuples():
            n = int(r.N_GameInMatch)
            info[r.GameId] = {"bo": max(bo, 5 if n > 3 else bo), "n": n, "wins": dict(wins)}
            wins[r.winner_team] += 1
    return info


def team_late_table(a):
    """(팀, 챔피언) → [(날짜, 세트 번호)] 다전제(2세트 이상 간 매치)의 픽."""
    p = a[a["action"] == "pick"]
    multi = p.groupby("MatchId")["N_GameInMatch"].transform("max") >= 2
    t = defaultdict(list)
    for r in p[multi].itertuples():
        t[(r.team, r.cid)].append((r.day, int(r.N_GameInMatch)))
    return t


def p4_features(rows, a, lineup, ex_cache, sinfo, late_t):
    rp = a.drop_duplicates("GameId").set_index("GameId")["RiotPlatformGameId"].to_dict()
    team_avg = {}
    for r in rows:
        ctx, gid = r[8], r[1]
        si = sinfo.get(gid, {"bo": 3, "n": 1, "wins": {}})
        T, O, D = r[7], ctx["opp_team"], ctx["D"]
        need = (si["bo"] + 1) // 2
        wT, wO = si["wins"].get(T, 0), si["wins"].get(O, 0)
        rem = max(1, (need - wT) + (need - wO) - 1)          # 이번 세트 포함 최대 남은 세트
        mp = float(wT == need - 1 or wO == need - 1)
        pool_sorted = ex_cache[D][0]
        game = rp.get(gid)
        gone = set(ctx["bans"]) | {c for c, _ in ctx["own"] + ctx["opp"]}
        # 이번 매치 앞 세트에 쓴 것 = 후보 목록에 없는 픽 (피어리스로 이미 빠짐) → 남은 풀은 후보 목록 안에서만 셈
        cand = set(ctx["cands"])

        def rem_pool(p):
            out = [c for c in pool_sorted.get(p, []) if c in cand and c not in gone]
            return out[:10]
        is_pick = r[2] == "pick"
        mine = [F._player(lineup, game, T if is_pick else O, ro) for ro in (ctx["my_open"] if is_pick else ctx["op_open"])]
        mine = [p for p in mine if p]
        theirs = [p for p in (F._player(lineup, game, O, ro) for ro in ctx["op_open"]) if p] if is_pick else []
        rpm = {p: rem_pool(p) for p in mine}
        rpt = {p: rem_pool(p) for p in theirs}
        key = (T, D)
        if key not in team_avg:
            vals = [n for (tm, _), lst in late_t.items() if tm == T for d, n in lst if d < D]
            team_avg[key] = np.mean(vals) if vals else 1.5
        X = np.zeros((len(ctx["cands"]), len(P4)), np.float32)
        for i, c in enumerate(ctx["cands"]):
            core = max([float(c in v[:2]) for v in rpm.values()] or [0.0])
            top3 = max([float(c in v[:3]) for v in rpm.values()] or [0.0])
            depth = max([len(v) for p, v in rpm.items() if c in v[:2]] or [0])
            deny = max([float(c in v[:3]) for v in rpt.values()] or [0.0])
            hist = [n for d, n in late_t.get((T if is_pick else O, c), []) if d < D]
            late = (np.mean(hist) - team_avg[key]) if hist else 0.0
            X[i] = [core * (rem - 1), top3 * mp, core * np.log1p(depth), deny * rem, late * (si["n"] - 1.5)]
        r.append(X)
    return rows


def main():
    t0 = time.time()
    rows, a, gday, all_games = pd.read_pickle(OUT / "m4_v4_rows.pkl")
    _, pro_pick, _, lineup, sq, _ = B.load()
    days = sorted(a["day"].unique())
    ex_cache = {D: V.day_extras(D, pro_pick, sq) for D in days}
    sinfo = series_info(a)
    rows = p4_features(rows, a, lineup, ex_cache, sinfo, team_late_table(a))   # r[12]
    log.info("P4 특징 (%.0f초)", time.time() - t0)
    sets = B.eval_sets(a)
    base = {"pick": {"extra": [], "p1": False}, "ban": {"extra": ["c2_hab"], "p1": True}}
    configs = {"M4 최종 (기준)": None, "+ P4 전체": P4}
    for f in P4:
        configs[f"+ P4 − {f}"] = [x for x in P4 if x != f]

    def arrays(rr, action, p4):
        sp = base[action]
        feats = MM.PICK_FEATS if action == "pick" else MM.BAN_FEATS
        ix = [V.ALL.index(k) for k in feats]
        exi = [V3.EX_NAMES.index(k) for k in sp["extra"]]
        p4i = [P4.index(k) for k in (p4 or []) if not (action == "ban" and k == "p4_deny")]
        sid = np.concatenate([np.full(len(r[3]), r[0]) for r in rr])
        y = np.concatenate([r[3] for r in rr]).astype(float)
        X = np.vstack([np.hstack([r[4][:, ix], r[9][:, exi]] + ([r[10]] if sp["p1"] else []) + ([r[12][:, p4i]] if p4i else []))
                       for r in rr]).astype(float)
        return sid, y, X

    res, coefs = [], {}
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        tr = [r for r in rows if gday[r[1]] < start]
        te = [r for r in rows if r[1] in gids]
        for cname, p4 in configs.items():
            for action in ("pick", "ban"):
                for lo, hi in MM.SEGMENTS[action]:
                    trg = [r for r in tr if r[2] == action and lo <= r[5] <= hi]
                    teg = [r for r in te if r[2] == action and lo <= r[5] <= hi]
                    sid, y, X = arrays(trg, action, p4)
                    mu, sd = X.mean(0), X.std(0) + 1e-9
                    b = MM.fit_off(sid, y, (X - mu) / sd, np.zeros(len(y)), 1.0)
                    if cname == "+ P4 전체" and name == "26.15" and p4:
                        k = len([x for x in P4 if not (action == "ban" and x == "p4_deny")])
                        coefs[f"{action} {lo}-{hi}"] = dict(zip([x for x in P4 if not (action == "ban" and x == "p4_deny")], b[-k:]))
                    s2, y2, X2 = arrays(teg, action, p4)
                    meta = {r[0]: r for r in teg}
                    for kk, rk in MM.rank_list(s2, y2, ((X2 - mu) / sd) @ b):
                        r = meta[kk]
                        res.append({"set": name, "config": cname, "action": action, "game_n": sinfo.get(r[1], {}).get("n", 1),
                                    "bo": sinfo.get(r[1], {}).get("bo", 3), "rank": rk})
        log.info("  %s 끝 (%.0f초)", name, time.time() - t0)
    R = pd.DataFrame(res)
    H.save_csv(R, OUT / "m4_v5_ranks.csv")

    def t(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%} · {(1 / g['rank']).mean():.3f}"
    L = ["# M4 P4 — 시리즈 단위 피어리스 계획 (리허설)", "",
         "- 기준 = M4 최종 (픽 = 공통 + 이길 가치, 밴 = 1·2페이즈 + 이길 가치 + C2 + P1). 값 = top1 · top3 · top5 · MRR (3세트 합)", "",
         "| 설정 | 픽 | 밴 | RTM 픽 top5 | 5판 경기 픽 top5 |", "|---|---|---|---|---|"]
    for c in configs:
        g = R[R["config"] == c]
        gp = g[g["action"] == "pick"]
        L.append(f"| {c} | {t(gp)} | {t(g[g['action'] == 'ban'])} | {(gp[gp['set'] == 'RTM']['rank'] <= 5).mean():.1%} | "
                 f"{(gp[gp['bo'] == 5]['rank'] <= 5).mean():.1%} |")
    L += ["", "## 세트 번호별 (밴·픽 합 top5)", "", "| 세트 | 단계 | 기준 | P4 전체 |", "|---|---|---|---|"]
    for n, g in R.groupby("game_n"):
        L.append(f"| {n} | {len(g[g['config'] == 'M4 최종 (기준)'])} | {(g[g['config'] == 'M4 최종 (기준)']['rank'] <= 5).mean():.1%} | "
                 f"{(g[g['config'] == '+ P4 전체']['rank'] <= 5).mean():.1%} |")
    C = pd.DataFrame(coefs)
    L += ["", "## P4 계수 (26.15 세트 학습, 1 SD 당)", "", "| 특징 | " + " | ".join(C.columns) + " |", "|---|" + "---|" * len(C.columns)]
    for f in P4:
        if f in C.index:
            L.append(f"| {f} | " + " | ".join("—" if pd.isna(C.loc[f, c]) else f"{C.loc[f, c]:+.2f}" for c in C.columns) + " |")
    (OUT / "m4_v5_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
