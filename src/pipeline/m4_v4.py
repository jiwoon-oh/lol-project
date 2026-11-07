"""M4 P1 — 패치 맞춤 메타 (2026-11-07, docs/PIPELINE.md "M4")

검증 기간(26.16)은 LCK 경기가 08-25 까지 25개뿐 → 같은 패치로 치러진 다른 리그 경기를 메타 신호로.
LCS·LEC 는 패치를 LCK 보다 4~7일 먼저 시작 (26.16: LEC 08-14 · LCS 08-15 · LCKC 08-17 · LCK·LPL 08-19) → 그 앞선 경기가 새 패치 초반 신호.
특징 (그 LCK 경기의 패치 P, 그 경기 전날까지, 다른 리그는 08-25 까지):
  pp_pres      : 패치 P 경기들의 픽+밴 비율 (log)
  pp_prev      : 직전 패치의 픽+밴 비율 (log)
  pp_delta     : pp_pres − pp_prev (패치로 메타가 움직인 정도)
  pp_young     : pp_pres × [LCK 가 패치 P 를 아직 10경기 미만 치름] (새 패치 초반엔 다른 리그 신호를 더 믿도록)
  pp_buff      : 패치 P 패치노트에서 버프 +1 · 너프 −1 · 조정/없음 0 (c_patch_champion_summary)
변형: 같은 패치 집계를 5리그(LCK·LCKC·LPL·LEC·LCS)로 / 전 리그(OE 41개)로. 학습은 둘 다 LCK 경기만
기준 = M4 최종 (5리그 · M4 1차 + C2)

    python src/pipeline/m4_v4.py
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
import m4_baseline as B  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v1 as V  # noqa: E402
import m4_v3 as V3  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = B.OUT
P1 = ["pp_pres", "pp_prev", "pp_delta", "pp_young", "pp_buff"]


def patch_events(cid):
    """전 리그 팀 행 → (리그, 날짜, OE 패치, 챔피언) 픽·밴 사건, 08-25 까지."""
    cols = ["gameid", "league", "date", "patch", "position"] + [f"pick{i}" for i in range(1, 6)] + [f"ban{i}" for i in range(1, 6)]
    oe = pd.read_csv(M1.OE_FILE, usecols=cols, low_memory=False, dtype={"patch": str})
    tm = oe[(oe["position"] == "team") & (oe["date"].str[:10] <= B.OTHER_END)].copy()
    tm["day"] = tm["date"].str[:10]
    ev = tm.melt(id_vars=["gameid", "league", "day", "patch"], value_vars=cols[5:], value_name="ch").dropna(subset=["ch"])
    ev["cid"] = ev["ch"].map(cid)
    games = tm[["gameid", "league", "day", "patch"]].drop_duplicates()
    return ev.dropna(subset=["cid"]), games


def oe_patch(p26: str) -> str:
    a, b = str(p26).split(".")
    return f"{int(a) - 10}.{int(b):02d}"


def p1_features(rows, a, ev, games, buff, five_only):
    if five_only:
        ev = ev[ev["league"].isin(M1.LEAGUES)]
        games = games[games["league"].isin(M1.LEAGUES)]
    gp = games.set_index("gameid")["patch"].to_dict()
    patch_of = a.drop_duplicates("GameId").set_index("GameId")["RiotPlatformGameId"].map(gp).to_dict()
    patches = sorted(games["patch"].dropna().unique())
    prev_of = {p: patches[i - 1] if i > 0 else None for i, p in enumerate(patches)}
    cache = {}
    lck_games = games[games["league"] == "LCK"]
    for r in rows:
        D, P = r[8]["D"], patch_of[r[1]]
        key = (D, P)
        if key not in cache:
            def pres(patch):
                if patch is None:
                    return {}, 1
                e = ev[(ev["patch"] == patch) & (ev["day"] < D)]
                n = max(1, (games[(games["patch"] == patch) & (games["day"] < D)]).shape[0] * 2)
                return Counter(e["cid"]), n
            cp, n_p = pres(P)
            cq, n_q = pres(prev_of.get(P))
            young = float((lck_games[(lck_games["patch"] == P) & (lck_games["day"] < D)]).shape[0] < 10)
            cache[key] = (cp, n_p, cq, n_q, young)
        cp, n_p, cq, n_q, young = cache[key]
        X = np.zeros((len(r[8]["cands"]), len(P1)), np.float32)
        for i, c in enumerate(r[8]["cands"]):
            pp = np.log((cp.get(c, 0) + 0.5) / (n_p + 5))
            pv = np.log((cq.get(c, 0) + 0.5) / (n_q + 5))
            X[i] = [pp, pv, pp - pv, pp * young, buff.get((P, c), 0.0)]
        r.append(X)
    return rows


def main():
    t0 = time.time()
    wv = MM.win_value_table()
    rm = V3.research_mentions()
    cid = B.cid_map()
    ev, games = patch_events(cid)
    cache = OUT / "m4_v4_rows.pkl"   # 특징 표 캐시 (끝에서 실패해도 다시 만들지 않게)
    if cache.exists():
        rows, a, gday, all_games = pd.read_pickle(cache)
    else:
        rows, a, gday, all_games = V3.build(V3.BASE_LEAGUES, wv, rm)
        s = pd.read_csv(PROCESSED / "c_patch_champion_summary.csv", dtype={"patch": str})
        buff = {(r.patch, r.champion_id): (1.0 if r.label == "buff" else -1.0 if r.label == "nerf" else 0.0) for r in s.itertuples()}
        rows = p1_features(rows, a, ev, games, buff, five_only=True)      # r[10] = 5리그
        rows = p1_features(rows, a, ev, games, buff, five_only=False)     # r[11] = 전 리그
        pd.to_pickle((rows, a, gday, all_games), cache)
    log.info("특징 (%.0f초)", time.time() - t0)
    sets = B.eval_sets(a)
    gp = games.set_index("gameid")["patch"].to_dict()
    patch_of = a.drop_duplicates("GameId").set_index("GameId")["RiotPlatformGameId"].map(gp)
    lck_first = games[games["league"] == "LCK"].groupby("patch")["day"].min()
    configs = {"M4 최종 (기준)": None, "+ P1 (같은 패치 5리그)": 10, "+ P1 (같은 패치 전 리그)": 11}

    def arrays(rr, feats, ex, slot):
        ix = [V.ALL.index(k) for k in feats]
        exi = [V3.EX_NAMES.index(k) for k in ex]
        sid = np.concatenate([np.full(len(r[3]), r[0]) for r in rr])
        y = np.concatenate([r[3] for r in rr]).astype(float)
        X = np.vstack([np.hstack([r[4][:, ix], r[9][:, exi]] + ([r[slot]] if slot else [])) for r in rr]).astype(float)
        return sid, y, X

    res = []
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        tr = [r for r in rows if gday[r[1]] < start]
        te = [r for r in rows if r[1] in gids]
        for cname, slot in configs.items():
            for action in ("pick", "ban"):
                feats = MM.PICK_FEATS if action == "pick" else MM.BAN_FEATS
                ex = [] if action == "pick" else ["c2_hab"]
                for lo, hi in MM.SEGMENTS[action]:
                    trg = [r for r in tr if r[2] == action and lo <= r[5] <= hi]
                    teg = [r for r in te if r[2] == action and lo <= r[5] <= hi]
                    sid, y, X = arrays(trg, feats, ex, slot)
                    mu, sd = X.mean(0), X.std(0) + 1e-9
                    b = MM.fit_off(sid, y, (X - mu) / sd, np.zeros(len(y)), 1.0)
                    s2, y2, X2 = arrays(teg, feats, ex, slot)
                    meta = {r[0]: r for r in teg}
                    for k, rk in MM.rank_list(s2, y2, ((X2 - mu) / sd) @ b):
                        r = meta[k]
                        pf = patch_of.get(r[1])
                        days_in = (pd.Timestamp(r[8]["D"]) - pd.Timestamp(lck_first[pf])).days if pf in lck_first.index else 99
                        res.append({"set": name, "config": cname, "action": action, "step": r[5], "team": r[7],
                                    "rank": rk, "early_patch": days_in < 7})
                    if name == "26.15" and slot:
                        log.info("  %s %s %s P1 계수 %s", cname, action, lo, np.round(b[-len(P1):], 2))
        log.info("  %s 끝 (%.0f초)", name, time.time() - t0)
    R = pd.DataFrame(res)
    H.save_csv(R, OUT / "m4_v4_ranks.csv")

    def t(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%} · {(1 / g['rank']).mean():.3f}"
    L = ["# M4 P1 — 패치 맞춤 메타 (리허설)", "",
         "- 같은 패치(그 LCK 경기의 패치)로 치러진 다른 리그 경기의 픽·밴 비율, 직전 패치 비율, 변화량, 새 패치 초반 가중, 패치노트 버프·너프",
         "- LCS·LEC 가 패치를 4~7일 먼저 시작 → LCK 새 패치 초반의 신호. 다른 리그는 08-25 까지, 학습은 LCK 만", "",
         "| 설정 | 픽 | 밴 | 패치 첫 주 픽 top5 | 패치 첫 주 밴 top5 |", "|---|---|---|---|---|"]
    for c in configs:
        g = R[R["config"] == c]
        e = g[g["early_patch"]]
        L.append(f"| {c} | {t(g[g['action'] == 'pick'])} | {t(g[g['action'] == 'ban'])} | "
                 f"{(e[e['action'] == 'pick']['rank'] <= 5).mean():.1%} ({(e['action'] == 'pick').sum()}) | "
                 f"{(e[e['action'] == 'ban']['rank'] <= 5).mean():.1%} |")
    L += ["", "| 세트 | 설정 | 픽 top5 | 밴 top5 |", "|---|---|---|---|"]
    for (s_, c), g in R.groupby(["set", "config"], sort=False):
        L.append(f"| {s_} | {c} | {(g[g['action'] == 'pick']['rank'] <= 5).mean():.1%} | {(g[g['action'] == 'ban']['rank'] <= 5).mean():.1%} |")
    (OUT / "m4_v4_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
