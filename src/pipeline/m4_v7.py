"""M4 시리즈·상대 전적 특징 + LightGBM 정리 (2026-11-07, docs/PIPELINE.md "M4")

정보 범위 (사용자 결정 2026-11-07, 'A'): 경기 전날까지 + **같은 매치(시리즈) 앞 세트의 밴·픽과 승패**.
  같은 날 다른 매치 · 지금 세트의 아직 안 나온 단계 · 지금 세트 결과는 쓰지 않음.
근거: 세트 2+ LCK 밴의 39.5% (1밴 페이즈 56.3%) 가 같은 팀이 앞 세트에 밴한 챔피언, 같은 상대에게 전에 밴한 챔피언 43.3%
특징 (후보 c, 지금 팀 T · 상대 O)
  s_ban_same   : T 가 이 시리즈 앞 세트에서 c 를 밴한 횟수
  s_ban_last   : T 가 바로 앞 세트에서 c 를 밴했나
  s_ban_opp    : O 가 이 시리즈 앞 세트에서 c 를 밴한 횟수
  s_lost_x_ban : T 가 바로 앞 세트를 졌나 × s_ban_last (지면 밴을 바꾸나)
  h2h_ban      : T 가 시즌 중 O 를 상대로 (전날까지) 한 밴 중 c 비율 (log)
  h2h_pick_opp : O 가 시즌 중 T 를 상대로 (전날까지) c 를 뽑은 수 (log1p)  — 타깃 밴·응수
  h2h_pick     : T 가 시즌 중 O 를 상대로 c 를 뽑은 수 (log1p)
  game_n       : 세트 번호 (다른 특징과 함께 비선형 모델이 씀)
모델: 이길 가치 칸 뺌 (누수 걱정 제거, 성적 차이 없음 확인). LightGBM 은 학습 기간 마지막 15% 날짜로 조기 종료 → 그 반복 수로 전체 다시 학습
비교 (LCK 리허설): 픽·밴 각각 선형(LCK 만) / LightGBM(5리그 w=3) / 앙상블, 시리즈 특징 유무
누수 대조: ① 정답 섞기 ② 평가 경기의 시리즈 특징을 다른 매치 것으로 바꿔 넣기 (성적이 시리즈 특징 없는 수준으로 떨어져야 함)
캐시: m4/m4_v6_rows.pkl (5리그 특징 표)

    python src/pipeline/m4_v7.py
"""
from __future__ import annotations

import bisect
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m1_comp as M1  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v1 as V  # noqa: E402
import m4_v6 as V6  # noqa: E402
import m4_v6b as V6B  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = B.OUT
SER = ["s_ban_same", "s_ban_last", "s_ban_opp", "s_lost_x_ban", "h2h_ban", "h2h_pick_opp", "h2h_pick", "game_n"]
PICK_FEATS = [f for f in MM.PICK_FEATS if f != "win_value"]
BAN_FEATS = [f for f in MM.BAN_FEATS if f != "win_value"]


def winners(a):
    """GameId → 이긴 팀. LCK = is_winner, 다른 리그 = OE result."""
    w = {}
    l = a[(a["league"] == "LCK") & (a["is_winner"] == True)]  # noqa: E712
    w.update(l.drop_duplicates("GameId").set_index("GameId")["team"].to_dict())
    oe = pd.read_csv(M1.OE_FILE, usecols=["gameid", "position", "teamname", "result"], low_memory=False)
    oe = oe[(oe["position"] == "team") & (oe["result"] == 1)]
    ow = dict(zip("OE_" + oe["gameid"].astype(str), oe["teamname"]))
    for g in a.loc[a["league"] != "LCK", "GameId"].unique():
        if g in ow:
            w[g] = ow[g]
    return w


def series_features(rows, a, wins, shuffle_series=False, seed=0):
    """r[12] = 시리즈·상대 전적 특징. shuffle_series=True 면 평가용 대조 (다른 매치의 앞 세트 정보)."""
    g = a.drop_duplicates("GameId").set_index("GameId")[["MatchId", "N_GameInMatch", "day"]]
    acts = defaultdict(list)                     # 매치 → [(세트, 팀, 행동, 챔피언)]
    for r in a.dropna(subset=["cid"]).itertuples():
        acts[r.MatchId].append((int(r.N_GameInMatch), r.team, r.action, r.cid))
    game_of = {(m, n): gid for gid, (m, n, _) in g.iterrows()}
    h2h = defaultdict(list)                      # (팀, 상대) → [(날짜, 행동, 챔피언)]
    for r in a.dropna(subset=["cid"]).sort_values("day").itertuples():
        h2h[(r.team, r.opponent)].append((r.day, r.action, r.cid))
    matches = list(acts)
    rng = np.random.default_rng(seed)
    alt = dict(zip(matches, rng.permutation(matches))) if shuffle_series else None
    for r in rows:
        gid, ctx = r[1], r[8]
        T, O, D = r[7], ctx["opp_team"], ctx["D"]
        mid, n = g.loc[gid, "MatchId"], int(g.loc[gid, "N_GameInMatch"])
        src = alt[mid] if shuffle_series else mid
        prev = [x for x in acts[src] if x[0] < n]
        if shuffle_series:                        # 다른 매치의 팀 이름을 지금 팀으로 바꿔 끼움
            teams = sorted({x[1] for x in acts[src]})
            mp = {teams[0]: T, teams[-1]: O} if teams else {}
            prev = [(k, mp.get(t, t), act, c) for k, t, act, c in prev]
        ban_T = Counter(c for k, t, act, c in prev if t == T and act == "ban")
        ban_last = {c for k, t, act, c in prev if t == T and act == "ban" and k == n - 1}
        ban_O = Counter(c for k, t, act, c in prev if t == O and act == "ban")
        last_gid = game_of.get((mid, n - 1))
        lost = float(n > 1 and last_gid is not None and wins.get(last_gid) not in (None, T))
        hv = h2h.get((T, O), [])
        hv = hv[:bisect.bisect_left(hv, (D, "", ""))]
        hb = Counter(c for _, act, c in hv if act == "ban")
        hp = Counter(c for _, act, c in hv if act == "pick")
        ho = h2h.get((O, T), [])
        ho = ho[:bisect.bisect_left(ho, (D, "", ""))]
        hpo = Counter(c for _, act, c in ho if act == "pick")
        nb = max(1, sum(hb.values()))
        X = np.zeros((len(ctx["cands"]), len(SER)), np.float32)
        for i, c in enumerate(ctx["cands"]):
            sl = float(c in ban_last)
            X[i] = [ban_T.get(c, 0), sl, ban_O.get(c, 0), lost * sl, np.log((hb.get(c, 0) + 0.1) / (nb + 1)),
                    np.log1p(hpo.get(c, 0)), np.log1p(hp.get(c, 0)), n]
        if len(r) > 12:
            r[12] = X
        else:
            r.append(X)
    return rows


def mats(rr, action, ser):
    feats = PICK_FEATS if action == "pick" else BAN_FEATS
    ix = [V.ALL.index(k) for k in feats]
    sid, y, X, lck = [], [], [], []
    for r in rr:
        parts = [r[4][:, ix]]
        if action == "ban":
            parts += [r[9], r[10]]
        if ser:
            parts.append(r[12])
        X.append(np.hstack(parts))
        sid.append(np.full(len(r[3]), r[0]))
        y.append(r[3])
        lck.append(np.full(len(r[3]), float(r[11] == "LCK")))
    return np.concatenate(sid), np.concatenate(y).astype(float), np.vstack(X).astype(float), np.concatenate(lck)


def lgb_es(trg, gday, action, ser, w_lck=3.0):
    """학습 기간 마지막 15% 날짜로 조기 종료 → 그 반복 수로 전체 다시 학습."""
    import lightgbm as lgb
    days = sorted({gday[r[1]] for r in trg})
    cut = days[int(len(days) * 0.85)]
    fit_r = [r for r in trg if gday[r[1]] < cut]
    val_r = [r for r in trg if gday[r[1]] >= cut]

    def ds(rr):
        sid, y, X, lck = mats(rr, action, ser)
        order = np.argsort(sid, kind="stable")
        _, cnt = np.unique(sid[order], return_counts=True)
        return lgb.Dataset(np.hstack([X, lck[:, None]])[order], label=y[order], group=cnt,
                           weight=np.where(lck[order] == 1, w_lck, 1.0))
    d_fit, d_val = ds(fit_r), ds(val_r)
    m = lgb.train(V6B.PARAMS, d_fit, num_boost_round=1000, valid_sets=[d_val],
                  callbacks=[lgb.early_stopping(50, verbose=False)])
    best = max(30, m.best_iteration)
    full = lgb.train(V6B.PARAMS, ds(trg), num_boost_round=best)
    return full, best


def main():
    t0 = time.time()
    rows, a, fear, ds_days = pd.read_pickle(OUT / "m4_v6_rows.pkl")
    if "is_winner" not in a.columns:
        a["is_winner"] = np.nan
    wins = winners(a)
    rows = series_features(rows, a, wins)
    log.info("시리즈 특징 (%.0f초)", time.time() - t0)
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    sets = B.eval_sets(a[a["league"] == "LCK"])
    res, iters = [], []
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        tr = [r for r in rows if gday[r[1]] < start]
        te = [r for r in rows if r[1] in gids]
        for action in ("pick", "ban"):
            for lo, hi in MM.SEGMENTS[action]:
                trg = [r for r in tr if r[2] == action and lo <= r[5] <= hi]
                teg = [r for r in te if r[2] == action and lo <= r[5] <= hi]
                meta = {r[0]: r for r in teg}
                for ser in (False, True):
                    tag = " + 시리즈" if ser else ""
                    sid, y, X, lck = mats(trg, action, ser)
                    s2, y2, X2, _ = mats(teg, action, ser)
                    mu, sd = X.mean(0), X.std(0) + 1e-9
                    m = lck == 1
                    b = MM.fit_off(sid[m], y[m], (X[m] - mu) / sd, np.zeros(m.sum()), 1.0)
                    lin = ((X2 - mu) / sd) @ b
                    gbm, best = lgb_es(trg, gday, action, ser)
                    iters.append((name, action, lo, ser, best))
                    gb = gbm.predict(np.hstack([X2, np.ones((len(X2), 1))]))
                    ens = V6.zscore_within(s2, lin) + V6.zscore_within(s2, gb)
                    variants = {f"선형 LCK 만{tag}": lin, f"LightGBM 5리그{tag}": gb, f"앙상블{tag}": ens}
                    if ser and action == "ban" and name in ("26.15", "RTM"):
                        pass
                    for vname, sc in variants.items():
                        for k, rk in MM.rank_list(s2, y2, sc):
                            r = meta[k]
                            res.append({"set": name, "model": vname, "action": action, "step": r[5], "team": r[7],
                                        "game_n": int(r[12][0, -1]), "rank": rk})
        log.info("  %s 끝 (%.0f초)", name, time.time() - t0)
    R = pd.DataFrame(res)
    H.save_csv(R, OUT / "m4_v7_ranks.csv")

    # 누수 대조 (26.15 · RTM, 밴, 시리즈 특징 모델): ① 정답 섞기 ② 다른 매치 시리즈 정보
    ctrl = []
    rows_alt = series_features([list(r) for r in rows], a, wins, shuffle_series=True)
    alt_by = {r[0]: r for r in rows_alt}
    for name in ("26.15", "RTM"):
        gids = sets[name]
        start = min(gday[g] for g in gids)
        for lo, hi in MM.SEGMENTS["ban"]:
            trg = [r for r in rows if gday[r[1]] < start and r[2] == "ban" and lo <= r[5] <= hi]
            teg = [r for r in rows if r[1] in gids and r[2] == "ban" and lo <= r[5] <= hi]
            sid, y, X, lck = mats(trg, "ban", True)
            s2, y2, X2, _ = mats(teg, "ban", True)
            _, _, X2a, _ = mats([alt_by[r[0]] for r in teg], "ban", True)
            mu, sd = X.mean(0), X.std(0) + 1e-9
            m = lck == 1
            b = MM.fit_off(sid[m], y[m], (X[m] - mu) / sd, np.zeros(m.sum()), 1.0)
            rng = np.random.default_rng(0)
            ysh = np.zeros_like(y)
            _, grp = np.unique(sid, return_inverse=True)
            first = np.r_[0, np.flatnonzero(np.diff(grp)) + 1]
            sizes = np.diff(np.r_[first, len(grp)])
            ysh[first + (rng.random(len(first)) * sizes).astype(int)] = 1
            bsh = MM.fit_off(sid[m], ysh[m], (X[m] - mu) / sd, np.zeros(m.sum()), 1.0)
            for cname, sc in (("정상", ((X2 - mu) / sd) @ b), ("다른 매치 시리즈 정보", ((X2a - mu) / sd) @ b),
                              ("정답 섞기", ((X2 - mu) / sd) @ bsh)):
                for k, rk in MM.rank_list(s2, y2, sc):
                    ctrl.append({"set": name, "control": cname, "rank": rk})
    Cc = pd.DataFrame(ctrl)

    def t(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%} · {(1 / g['rank']).mean():.3f}"
    L = ["# M4 시리즈·상대 전적 특징 + LightGBM 정리 (LCK 리허설)", "",
         "- 정보 범위 A: 경기 전날까지 + 같은 매치 앞 세트 밴·픽·승패. 이길 가치 칸 뺌. LightGBM 은 학습 기간 마지막 15% 로 조기 종료",
         "- 값 = top1 · top3 · top5 · MRR (26.14 · 26.15 · RTM 합)", "",
         "| 모델 | 픽 | 밴 | 강팀 픽 top5 | RTM 픽 · 밴 top5 | 세트 2+ 밴 top5 |", "|---|---|---|---|---|---|"]
    for mname in R["model"].unique():
        g = R[R["model"] == mname]
        gp, gb = g[g["action"] == "pick"], g[g["action"] == "ban"]
        L.append(f"| {mname} | {t(gp)} | {t(gb)} | {(gp[gp['team'].isin(V6.STRONG)]['rank'] <= 5).mean():.1%} | "
                 f"{(gp[gp['set'] == 'RTM']['rank'] <= 5).mean():.1%} · {(gb[gb['set'] == 'RTM']['rank'] <= 5).mean():.1%} | "
                 f"{(gb[gb['game_n'] >= 2]['rank'] <= 5).mean():.1%} |")
    L += ["", "## 누수 대조 (밴, 선형 + 시리즈, 26.15 · RTM 합)", "", "| 대조 | top1 · top3 · top5 · MRR |", "|---|---|"]
    for c, g in Cc.groupby("control", sort=False):
        L.append(f"| {c} | {t(g)} |")
    L += ["", "- '다른 매치 시리즈 정보' 가 시리즈 특징 없는 선형 수준으로, '정답 섞기' 가 무작위(top5 약 4%) 근처로 떨어지면 누수 없음", "",
          "## LightGBM 조기 종료 반복 수", "", "| 세트 | 종류 | 구간 | 시리즈 | 반복 |", "|---|---|---|---|---|"]
    for it in iters:
        L.append(f"| {it[0]} | {it[1]} | {it[2]} | {'있음' if it[3] else '없음'} | {it[4]} |")
    (OUT / "m4_v7_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("끝 (%.0f초)", time.time() - t0)


if __name__ == "__main__":
    main()
