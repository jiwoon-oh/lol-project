"""M5 추천 검증 — 추천을 따랐으면 더 이겼을까 (2026-11-09)

배경: 시험판·대시보드 추천 = 굴리기(M4 확률로 남은 드래프트) + 이길 가치 표(m5_value_champion.csv).
      그 표는 08-25 까지 전부로 만든 값이라 리허설·검증 경기와 겹침 → 재생 검증 ③의 '추천 승률'은 학습 안 결과였음.
      또 실제 승패로는 "추천대로 했으면 이겼다"를 직접 볼 수 없음(반사실). 그래서 할 수 있는 확인을 겹쳐서 봄.
방법: 세트(LCK 패치 26.07 · 26.08 · 26.09 · 26.10 · RTM · 26.14 · 26.15 · 26.16)마다 **세트 시작 전 자료로만**
      ⓐ 이길 가치 표 (m5_value 채택 설정 C 그대로, 마지막 15% 로 k) ⓑ M4 픽·밴 모델 (m5_replay 와 같음) 을 다시 만들고
  ① 조합 가치 → 실제 승패: 실제 완성 조합의 가치 차가 Elo·진영 위에서 승패를 맞히나 (맞춤 없이 학습 밖 로그 손실, 기울기 진단)
     — 추천이 '이기는 쪽'을 고른다는 주장의 필요조건. 이게 안 되면 추천 승률 숫자는 근거 없음
  ② 추천에서 벗어난 만큼 졌나: 모든 팀의 픽 차례마다 굴리기 예상 승률 (추천 1순위 − 실제 픽) = 놓친 몫.
     경기마다 (블루 놓친 몫 합 − 레드) 가 Elo·진영 위에서 실제 승패를 설명하나. 모델이 맞다면 기울기 ≈ 1 (놓친 1%p ≈ 실제 1%p)
  ③ 일치율 (참고): 실제 픽이 추천 1순위·상위 5 안에 있나 vs 평소 확률 (재생 검증 ③을 모든 팀·학습 밖 표로)
한계: 관찰 자료라 인과가 아님 (강팀이 더 잘 고르고 더 잘 이김 → Elo 로만 통제). 밴은 안 봄. 선수 숙련 δ 안 넣음.
      설정 C(λ 등)는 26.14 · 26.15 로, C vs M1 가중은 26.08~26.15 로 골랐으므로 그 패치는 완전한 처음 보는 경기는 아님 → 26.16 · 26.07 따로 표시.
플레이오프(08-26~)는 안 봄.

    python src/pipeline/m5_rec_check.py [--sets 26.16] [--R 300] [--report]
"""
from __future__ import annotations

import json
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m4_baseline as B  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v6 as V6  # noqa: E402
import m4_v7 as V7  # noqa: E402
import m5_calibrate as CAL  # noqa: E402
import m5_replay as RP  # noqa: E402
import m5_state as ST  # noqa: E402
import m5_value as MV  # noqa: E402

PROCESSED, log = MV.PROCESSED, MV.log
OUT = PROCESSED / "m5" / "rec_check"
OUT.mkdir(parents=True, exist_ok=True)
POS = ST.V.POS
PP = MV.PP
R = int(sys.argv[sys.argv.index("--R") + 1]) if "--R" in sys.argv else 300
ALL_SETS = ["26.07", "26.08", "26.09", "26.10", "RTM", "26.14", "26.15", "26.16"]
SETS = sys.argv[sys.argv.index("--sets") + 1].split(",") if "--sets" in sys.argv else ALL_SETS
FRESH = {"26.07", "26.16"}      # 이길 가치 설정 고르기에 안 쓴 패치


def eval_sets(a_full):
    g = a_full[a_full["league"] == "LCK"].drop_duplicates("GameId")
    rtm = g["OverviewPage"].astype(str).str.contains("Road to MSI")
    out = {}
    for p in SETS:
        if p == "RTM":
            out[p] = set(g[rtm]["GameId"])
        else:
            out[p] = set(g[g["Patch"].astype(str).isin({p, p.rstrip("0")}) & ~rtm]["GameId"])
    return out


# ---------------------------------------------------------------- ⓐ 그 시점 이길 가치 표
def value_asof(G, S, cut, name):
    """설정 C(m5_value_config.json) 를 cut 전 자료로: 마지막 15% 로 k (출처별) → 전부로 다시 맞춤 → 챔피언@포지션 대회 %p.
    m5_value_w.py 최종 표와 같은 순서 (부트스트랩 없음)."""
    f = OUT / f"value_asof_{name}.csv"
    if f.exists():
        t = pd.read_csv(f)
        return t.set_index("key")["value_pro"].to_dict(), json.loads((OUT / f"value_asof_{name}.json").read_text(encoding="utf-8"))
    conf = json.loads((MV.OUT / "m5_value_config.json").read_text(encoding="utf-8"))
    assert conf["kind"] == "C" and conf["w_pro"] == 0.0, "채택 설정이 C(M1 없음)가 아님 — 이 스크립트를 고쳐야 함"
    c = conf["cfg"]["C"]
    m = (G["day"] < cut).to_numpy()
    Gc, Sc = G[m].reset_index(drop=True), S[m].reset_index(drop=True)
    A = np.ones(len(Gc), bool)
    for m_ in ((Gc["league"] == "SOLO").to_numpy(), (Gc["league"] != "SOLO").to_numpy()):
        d = np.sort(Gc.loc[m_, "day"].to_numpy())
        A &= ~(m_ & (Gc["day"] >= d[int(len(d) * 0.85)]).to_numpy())
    Ga, Sa, Gh, Sh = Gc[A].reset_index(drop=True), Sc[A].reset_index(drop=True), Gc[~A].reset_index(drop=True), Sc[~A].reset_index(drop=True)
    Da = MV.Design(Ga, Sa, c["chp"])
    Dh = MV.Design(Gh, Sh, c["chp"], Da.coder)
    ma = MV.fit_path(Ga, Da, c["lam"], c["lam_late"])
    ph = (Gh["league"] != "SOLO").to_numpy(float)
    k = MV.fit_k(Dh.vdiff(ma["V"]), Gh["blue_win"].to_numpy(float), MV.base_off(Gh, MV.fit_base(Ga)), ph)
    D = MV.Design(Gc, Sc, c["chp"])
    mf = MV.fit_path(Gc, D, c["lam"], c["lam_late"])
    keys = [kk for kind, kk in D.coder.idx if kind == "ch"]
    t = pd.DataFrame({"key": keys, "value_pro": [float(k[0]) * PP * MV.ch_pro_value(D, mf["V"], kk) for kk in keys],
                      "n_pro": [D.coder.n_pro.get(("ch", kk), 0) for kk in keys]})
    MV.H.save_csv(t, f)
    meta = {"cut": cut, "k_pro": float(k[0]), "k_solo": float(k[1]), "games": int(len(Gc)), "pro_games": int((Gc["league"] != "SOLO").sum())}
    (OUT / f"value_asof_{name}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return t.set_index("key")["value_pro"].to_dict(), meta


def side_elo_coef(cut):
    """그 시점까지 대회 5리그 경기의 블루 승 ~ Elo 차 (m5_draft.team_strength 와 같은 식, cut 전 경기만)."""
    from sklearn.linear_model import LogisticRegression
    m = pd.read_csv(PROCESSED / "m1" / "m1_games.csv", usecols=["league", "day", "elo_diff", "blue_win"], low_memory=False)
    m = m[(m["league"] != "SOLO") & (m["day"] < cut)].dropna()
    lr = LogisticRegression(C=1e4).fit(m[["elo_diff"]].to_numpy(), m["blue_win"].to_numpy())
    return float(lr.intercept_[0]), float(lr.coef_[0][0])


# ---------------------------------------------------------------- 실행
def main():
    t0 = time.time()
    sb = ST.StateBuilder()
    champs = list(sb.champs)
    ci = {c: i for i, c in enumerate(champs)}
    n = len(champs)
    calib = json.loads((MV.OUT / "calibration.json").read_text(encoding="utf-8"))["phase"]
    rows_all, a_full, *_ = pd.read_pickle(B.OUT / "m4_v6_rows.pkl")
    if "is_winner" not in a_full.columns:
        a_full["is_winner"] = np.nan
    rows_all = V7.series_features(rows_all, a_full, V7.winners(a_full))
    gday = a_full.drop_duplicates("GameId").set_index("GameId")["day"]
    lck = a_full[a_full["league"] == "LCK"]
    lineup = sb.lineup[~sb.lineup.index.duplicated()].to_dict()
    ginfo = lck.drop_duplicates("GameId").set_index("GameId")["RiotPlatformGameId"].astype(str)
    win = lck[lck["action"] == "pick"].groupby(["GameId", "team"])["is_winner"].first()
    side = lck.drop_duplicates(["GameId", "team"]).set_index(["GameId", "team"])["side"]
    m1g = pd.read_csv(PROCESSED / "m1" / "m1_games.csv", usecols=["matchId", "elo_diff"], dtype={"matchId": str}).set_index("matchId")["elo_diff"]
    G, S = MV.load_cached()
    sets = eval_sets(a_full)
    log.info("자료 준비 (%.0f초)", time.time() - t0)

    for name, gids in sets.items():
        fo = OUT / f"raw_{name}.pkl"
        if fo.exists():
            log.info("  %s 이미 있음 → 건너뜀", name)
            continue
        start = min(gday[g] for g in gids)
        val, meta = value_asof(G, S, start, name)
        VAL = np.array([[val.get(f"{c}@{r}", 0.0) for r in POS] for c in champs])
        sa, sbe = side_elo_coef(start)
        log.info("  %s: 시작 %s · 이길 가치 표 (대회 %d판, k %.2f) (%.0f초)", name, start, meta["pro_games"], meta["k_pro"], time.time() - t0)
        tr = [r for r in rows_all if gday[r[1]] < start]
        pm = CAL.train_pick([r for r in tr if r[2] == "pick"])
        bms = [((lo, hi), CAL.train_ban([r for r in tr if r[2] == "ban" and lo <= r[5] <= hi], gday)) for lo, hi in MM.SEGMENTS["ban"]]
        log.info("  %s M4 학습 (%.0f초)", name, time.time() - t0)
        games, turns = [], []
        for gid in sorted(gids, key=lambda g: gday[g]):
            recs = sb.a[sb.a["GameId"] == gid].sort_values("step")
            if len(recs) != 20 or recs["cid"].isna().any():
                continue
            fp = recs.loc[recs["pick_order"] == "fp", "team"].iloc[0]
            lp = recs.loc[recs["pick_order"] == "lp", "team"].iloc[0]
            blue = fp if side.get((gid, fp)) == "Blue" else lp
            red = lp if blue == fp else fp
            D = recs["day"].iloc[0]
            share, role_ok, role_any = ST.role_arrays(sb.day(D), champs)
            mid, ng = recs["MatchId"].iloc[0], int(recs["N_GameInMatch"].iloc[0])
            fear = set(sb.a[(sb.a["MatchId"] == mid) & (sb.a["N_GameInMatch"] < ng) & (sb.a["action"] == "pick")]["cid"])
            act = {int(r.step): (r.cid, r.team, r.action, r.role if isinstance(r.role, str) else None) for r in recs.itertuples()}
            # ① 실제 완성 조합 가치
            fin = {fp: 0.0, lp: 0.0}
            miss_role = 0
            for s in range(1, 21):
                c, tm, ac, ro = act[s]
                if ac == "pick":
                    if ro in POS:
                        fin[tm] += VAL[ci[c], POS.index(ro)]
                    else:
                        miss_role += 1
            rpg = ginfo.get(gid)
            players = {tm: {ro: str(lineup.get((rpg, tm, ro), "")) for ro in POS} for tm in (fp, lp)}
            games.append({"set": name, "gid": gid, "day": D, "blue": blue, "red": red, "blue_win": float(win.get((gid, blue), np.nan)),
                          "elo_diff": float(m1g.get(rpg, np.nan)), "draft_diff": fin[blue] - fin[red],
                          "side_a": sa, "elo_b": sbe, "miss_role": miss_role, "fp": fp, "lp": lp, "n_game": ng,
                          "acts": [(s, act[s][0], act[s][1], act[s][2], act[s][3]) for s in range(1, 21)],
                          "players": players, "fearless": sorted(fear), "fin": {fp: fin[fp], lp: fin[lp]}})
            # ② 픽 차례마다 놓친 몫
            for t in range(1, 21):
                c_t, tm_t, ac_t, ro_t = act[t]
                if ac_t != "pick":
                    continue
                sc = RP.score_rows(sb.replay(gid, t), pm, bms)
                FP = {}
                for s, (cands, z) in sc.items():
                    p = CAL.probs(z, calib[CAL.PHASE(s)]["tau"], calib[CAL.PHASE(s)]["eps"])
                    v = np.zeros(n)
                    v[[ci[c] for c in cands]] = p
                    FP[s] = v
                taken0 = np.zeros(n, bool)
                for c in fear:
                    if c in ci:
                        taken0[ci[c]] = True
                open0 = {fp: np.ones(5, bool), lp: np.ones(5, bool)}
                val0 = {fp: 0.0, lp: 0.0}
                for s in range(1, t):
                    c, tm, ac, ro = act[s]
                    taken0[ci[c]] = True
                    if ac == "pick" and ro in POS:
                        open0[tm][POS.index(ro)] = False
                        val0[tm] += VAL[ci[c], POS.index(ro)]
                p_t = FP[t]
                order = np.argsort(-p_t)
                cand = [j for j in order if p_t[j] > 0 and not taken0[j] and role_ok[j][open0[tm_t]].any()]
                cand = [j for i, j in enumerate(cand) if i < 3 or p_t[j] >= 0.03][:15]
                jt = ci[c_t]
                in_cand = jt in cand
                if not in_cand:
                    cand = cand + [jt]          # 실제 픽 예상 승률도 같은 방식으로 (후보 밖이어도)
                opp = lp if tm_t == fp else fp
                seed = zlib.crc32(f"{gid}/{t}".encode())
                wp = {}
                for j in cand:
                    _, vv, _ = ST.rollout(FP, t, fp, lp, taken0, open0, share, role_ok, role_any, R, seed, forced=j, VAL=VAL, val0=val0)
                    wp[j] = float((1 / (1 + np.exp(-(vv[tm_t] - vv[opp]) / PP))).mean())
                rec_c = [j for j in cand if in_cand or j != jt]          # 추천 후보 = 실제 픽을 억지로 넣기 전
                by = sorted(rec_c, key=lambda j: -wp[j])
                turns.append({"set": name, "gid": gid, "step": t, "team": tm_t, "is_blue": tm_t == blue, "in_cand": in_cand,
                              "n_cand": len(rec_c), "wp_actual": wp[jt], "wp_best": wp[by[0]], "wp_habit": wp[order[0]] if order[0] in wp else np.nan,
                              "rec_top1": jt == by[0], "rec_top5": jt in by[:5], "habit_top1": jt == order[0], "habit_top5": jt in list(order[:5]),
                              "rank": by.index(jt) + 1 if jt in by else np.nan,
                              "top": [(champs[j], wp[j]) for j in by[:5]], "habit": [(champs[j], float(p_t[j])) for j in order[:3]],
                              "cands": [(champs[j], wp[j], float(p_t[j]), POS[int(np.argmax(np.where(open0[tm_t], share[j], -1)))]) for j in rec_c]})
            log.info("    %s %s 끝 (%.0f초)", name, gid, time.time() - t0)
        pd.to_pickle((games, turns), fo)
    report()
    export_replay()
    log.info("끝 (%.0f초)", time.time() - t0)


# ---------------------------------------------------------------- 보고서
def sigm(z):
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def ll(y, p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


class _Fit:
    pass


def _newton(X, y, it=50):
    b = np.zeros(X.shape[1])
    for _ in range(it):
        p = sigm(X @ b)
        H_ = X.T @ (X * (p * (1 - p))[:, None]) + 1e-8 * np.eye(X.shape[1])
        step = np.linalg.solve(H_, X.T @ (y - p))
        b += step
        if np.abs(step).max() < 1e-10:
            break
    return b, H_


def logit_fit(X, y, B=2000, seed=0):
    """블루 승 ~ 절편 + X. 계수·95% 구간(경기 다시 뽑기)·p(왈드). statsmodels 없이."""
    from scipy.stats import norm
    Xc = np.column_stack([np.ones(len(y)), X])
    b, H_ = _newton(Xc, y)
    se = np.sqrt(np.diag(np.linalg.inv(H_)))
    rng = np.random.default_rng(seed)
    bs = np.array([_newton(Xc[i], y[i])[0] for i in rng.integers(0, len(y), (B, len(y)))])
    f = _Fit()
    f.params = b
    f.pvalues = 2 * norm.sf(np.abs(b / se))
    f.conf_int = lambda: np.percentile(bs, [2.5, 97.5], axis=0).T
    return f


def boot_ci(d, B=2000, seed=0):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), (B, len(d)))
    lo, hi = np.percentile(d[idx].mean(1), [2.5, 97.5])
    return d.mean(), lo, hi


def report():
    G, T = [], []
    for name in ALL_SETS:
        f = OUT / f"raw_{name}.pkl"
        if f.exists():
            g, t = pd.read_pickle(f)
            G += g
            T += t
    g = pd.DataFrame(G).dropna(subset=["blue_win", "elo_diff"]).sort_values("gid").reset_index(drop=True)   # 순서 고정 → 부트스트랩 구간 재현
    t = pd.DataFrame(T)
    sets_done = [s for s in ALL_SETS if s in set(g["set"])]
    y = g["blue_win"].to_numpy(float)
    z_elo = g["side_a"] + g["elo_b"] * g["elo_diff"]
    z_full = z_elo + g["draft_diff"] / PP
    L = ["# M5 추천 검증 — 추천을 따랐으면 더 이겼을까", "",
         f"- LCK {len(g)}경기 ({' · '.join(sets_done)}). 세트마다 **그 세트 시작 전 자료로만** 이길 가치 표(설정 C)와 M4 를 다시 만듦 → 전부 학습 밖",
         f"- 굴리기 {R}번 (후보끼리 같은 난수). 승률 = 1 / (1 + exp(−(우리 − 상대 가치 합) / 25)), 팀 실력 차는 ①에서만",
         "- 관찰 자료라 인과가 아님: 강팀이 더 잘 고르고 더 잘 이김 → Elo·진영만 통제. 밴은 안 봄. 선수 숙련 δ 없음", "",
         "## ① 완성 조합 가치 → 실제 승패 (필요조건)", "",
         "맞춤 없이: 진영·Elo 계수와 이길 가치 표 모두 세트 시작 전 자료로 만든 값 그대로 예측", "",
         "| 범위 | 경기 | Elo·진영만 로그 손실 | + 조합 가치 | 차이 [95%] (음수 = 조합 가치가 도움) | 블루 승률 |", "|---|---|---|---|---|---|"]
    for lab, m in [("전체", np.ones(len(g), bool)), ("처음 보는 패치 (26.07 · 26.16)", g["set"].isin(FRESH).to_numpy()),
                   ("나머지", ~g["set"].isin(FRESH).to_numpy())] + [(s, (g["set"] == s).to_numpy()) for s in sets_done]:
        if m.sum() < 5:
            continue
        d = ll(y[m], sigm(z_full[m].to_numpy())) - ll(y[m], sigm(z_elo[m].to_numpy()))
        mu, lo, hi = boot_ci(d)
        L.append(f"| {lab} | {int(m.sum())} | {ll(y[m], sigm(z_elo[m].to_numpy())).mean():.4f} | {ll(y[m], sigm(z_full[m].to_numpy())).mean():.4f} "
                 f"| {mu:+.4f} [{lo:+.4f}, {hi:+.4f}] | {y[m].mean():.1%} |")
    X = np.column_stack([g["elo_diff"], g["draft_diff"] / PP])
    fit = logit_fit(X, y)
    ci_ = fit.conf_int()
    L += ["", "진단: 블루 승 ~ 절편 + Elo 차 + (조합 가치 차 / 25) 를 이 경기들로 직접 맞춤 — 조합 가치 기울기가 1 이면 표의 %p 단위가 맞음, 0 이면 쓸모없음", "",
          f"- 조합 가치 기울기 **{fit.params[2]:.2f}** [95% {ci_[2][0]:.2f}, {ci_[2][1]:.2f}], p = {fit.pvalues[2]:.3f} · Elo 기울기 {fit.params[1]:.2f} (p {fit.pvalues[1]:.3f})",
          f"- 조합 가치 차 크기: 표준편차 {g['draft_diff'].std():.1f}%p · 절댓값 중앙 {g['draft_diff'].abs().median():.1f}%p",
          f"- 조합 가치 차 부호가 맞은 경기 (차이 0 제외): {((g['draft_diff'] > 0) == (g['blue_win'] == 1))[g['draft_diff'] != 0].mean():.1%}"]
    bins = pd.qcut(g["draft_diff"], 4, labels=["블루 불리 (하위 25%)", "2", "3", "블루 유리 (상위 25%)"])
    L += ["", "| 조합 가치 차 4분위 | 경기 | 평균 가치 차 | Elo·진영만 기대 블루 승률 | 실제 블루 승률 | 실제 − 기대 |", "|---|---|---|---|---|---|"]
    for b, q in g.groupby(bins, observed=True):
        e = sigm(q["side_a"] + q["elo_b"] * q["elo_diff"]).mean()
        L.append(f"| {b} | {len(q)} | {q['draft_diff'].mean():+.1f}%p | {e:.1%} | {q['blue_win'].mean():.1%} | {q['blue_win'].mean() - e:+.1%} |")

    if len(t):
        t["miss"] = (t["wp_best"] - t["wp_actual"]) * 100
        per = t.groupby(["gid", "is_blue"])["miss"].sum().unstack()
        g2 = g.set_index("gid").join(per.rename(columns={True: "miss_blue", False: "miss_red"}), how="inner")
        g2["dmiss"] = g2["miss_blue"] - g2["miss_red"]
        y2 = g2["blue_win"].to_numpy(float)
        fit2 = logit_fit(np.column_stack([g2["elo_diff"], g2["dmiss"] / PP]), y2)
        c2 = fit2.conf_int()
        L += ["", f"## ② 추천에서 벗어난 만큼 졌나 ({len(t)}번 픽 차례 · {len(g2)}경기)", "",
              "놓친 몫 = 그 픽 차례 굴리기 예상 승률 (추천 1순위 − 실제 픽), %p. 경기마다 팀 합. 모델이 맞다면 놓친 몫이 큰 팀이 그만큼 더 짐", "",
              f"- 픽 차례당 놓친 몫 평균 {t['miss'].mean():.2f}%p (중앙 {t['miss'].median():.2f}) · 실제 픽 = 추천 1순위 {t['rec_top1'].mean():.1%}",
              f"- 경기당 팀 놓친 몫 합 평균 {pd.concat([g2['miss_blue'], g2['miss_red']]).mean():.1f}%p",
              f"- 블루 승 ~ Elo 차 + (블루 − 레드 놓친 몫 / 25): 놓친 몫 기울기 **{fit2.params[2]:.2f}** [95% {c2[2][0]:.2f}, {c2[2][1]:.2f}], p = {fit2.pvalues[2]:.3f}"
              " — 모델이 맞다면 −1 근처, 0 이면 놓친 몫이 실제 승패와 무관", "",
              "| 블루 − 레드 놓친 몫 3분위 | 경기 | 평균 차 | Elo·진영만 기대 블루 승률 | 실제 블루 승률 | 실제 − 기대 |", "|---|---|---|---|---|---|"]
        b3 = pd.qcut(g2["dmiss"], 3, labels=["블루가 덜 놓침", "비슷", "블루가 더 놓침"])
        for b, q in g2.groupby(b3, observed=True):
            e = sigm(q["side_a"] + q["elo_b"] * q["elo_diff"]).mean()
            L.append(f"| {b} | {len(q)} | {q['dmiss'].mean():+.1f}%p | {e:.1%} | {q['blue_win'].mean():.1%} | {q['blue_win'].mean() - e:+.1%} |")
        L += ["", "## ③ 일치율 (참고, 모든 팀 픽 차례)", "", "| 방식 | 1순위 일치 | 상위 5 안 |", "|---|---|---|",
              f"| 평소 확률 (M4) | {t['habit_top1'].mean():.1%} | {t['habit_top5'].mean():.1%} |",
              f"| 추천 (굴리기 + 그 시점 이길 가치) | {t['rec_top1'].mean():.1%} | {t['rec_top5'].mean():.1%} |", "",
              f"- 실제 픽이 추천 후보 안 {t['in_cand'].mean():.1%} · 후보 수 평균 {t['n_cand'].mean():.1f}",
              f"- T1 픽 차례만 ({(t['team'] == 'T1').sum()}번): 추천 1순위 {t.loc[t['team'] == 'T1', 'rec_top1'].mean():.1%} · 상위 5 {t.loc[t['team'] == 'T1', 'rec_top5'].mean():.1%} "
              f"· 놓친 몫 평균 {t.loc[t['team'] == 'T1', 'miss'].mean():.2f}%p"]
    (OUT / "m5_rec_check.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(L))


def export_replay():
    """대시보드 '실제 경기 대조' 자료 → rec_check/replay.json (원문 없음: 드래프트·승패·추천 숫자만)."""
    G, T = [], []
    for name in ALL_SETS:
        f = OUT / f"raw_{name}.pkl"
        if f.exists():
            g, t = pd.read_pickle(f)
            G += g
            T += t
    if not G or "acts" not in G[0]:
        return
    tm = {}
    for t in T:
        tm[(t["gid"], t["step"])] = {"wp_actual": t["wp_actual"], "wp_best": t["wp_best"], "rank": None if pd.isna(t["rank"]) else int(t["rank"]),
                                     "in_cand": bool(t["in_cand"]), "n_cand": t["n_cand"], "top": [{"cid": c, "wp": w} for c, w in t["top"]],
                                     "habit": [{"cid": c, "p": p} for c, p in t["habit"]]}
    out = []
    for g in G:
        if pd.isna(g["blue_win"]):
            continue
        z_elo = g["side_a"] + g["elo_b"] * (0.0 if pd.isna(g["elo_diff"]) else g["elo_diff"])
        steps = []
        for s, c, team, ac, ro in g["acts"]:
            steps.append({"step": s, "cid": c, "team": team, "action": ac, "role": ro, "rec": tm.get((g["gid"], s))})
        miss = {k: sum((x["rec"]["wp_best"] - x["rec"]["wp_actual"]) * 100 for x in steps if x["rec"] and x["team"] == k) for k in (g["blue"], g["red"])}
        out.append({"gid": g["gid"], "set": g["set"], "day": g["day"], "n_game": g["n_game"], "blue": g["blue"], "red": g["red"],
                    "fp": g["fp"], "lp": g["lp"], "winner": g["blue"] if g["blue_win"] == 1 else g["red"],
                    "p_elo_blue": float(sigm(z_elo)), "p_draft_blue": float(sigm(z_elo + g["draft_diff"] / PP)),
                    "draft_diff_blue": g["draft_diff"], "miss": miss, "players": g["players"], "fearless": g["fearless"], "steps": steps})
    out.sort(key=lambda x: (x["day"], x["gid"]))
    (OUT / "replay.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log.info("대조 자료 %d경기 → %s", len(out), OUT / "replay.json")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
        export_replay()
    else:
        main()
