"""M5 밴 검증 — 밴 추천에서 벗어난 만큼 졌나 (2026-11-09)

m5_rec_check.py(픽) 와 같은 학습 밖 틀: 세트(LCK 26.07 ~ 26.16 · RTM)마다 세트 시작 전 자료로 이길 가치 표(설정 C)·M4 를 다시 만듦.
밴 차례마다
  후보 = 상대 위협 상위 12 (상대가 남은 픽에서 고를 확률 × 상대 빈 포지션 중 최고 값) + 그 팀 평소 밴(M4) 상위 3 + 실제 밴
  후보를 밴한 채 남은 드래프트를 R 번 굴림 (공통 난수) → 밴 가치 = 상대 완성 조합 가치를 얼마나 낮추나
  (대시보드 m5_sim.mc_rank 와 같이 우리 쪽 값은 밴 없이 굴린 평균으로 고정, 1수 앞보기·숙련 δ 없음)
  놓친 몫 = 밴 1순위 − 실제 밴 굴리기 예상 승률 (%p)
기준 (docs/decisions.md 2026-11-09, 돌리기 전): 블루 승 ~ Elo + (블루 − 레드 밴 놓친 몫)/25 기울기 구간 상한 < 0
참고: 픽 놓친 몫(m5_rec_check raw)과 같이 넣은 모형 · 밴 1순위가 풀렸을 때 상대가 가져간 비율 vs 예측 · 일치율
플레이오프 안 봄.

    python src/pipeline/m5_ban_check.py [--sets 26.16] [--R 300] [--report]
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
import m5_rec_check as RC  # noqa: E402
import m5_replay as RP  # noqa: E402
import m5_state as ST  # noqa: E402
import m5_value as MV  # noqa: E402

OUT, log, PP, POS = RC.OUT, RC.log, RC.PP, RC.POS
R = RC.R
SETS = RC.SETS


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
    side = lck.drop_duplicates(["GameId", "team"]).set_index(["GameId", "team"])["side"]
    G, S = MV.load_cached()
    sets = RC.eval_sets(a_full)
    log.info("자료 준비 (%.0f초)", time.time() - t0)
    for name, gids in sets.items():
        fo = OUT / f"ban_{name}.pkl"
        if fo.exists():
            log.info("  %s 이미 있음 → 건너뜀", name)
            continue
        start = min(gday[g] for g in gids)
        val, meta = RC.value_asof(G, S, start, name)
        VAL = np.array([[val.get(f"{c}@{r}", 0.0) for r in POS] for c in champs])
        tr = [r for r in rows_all if gday[r[1]] < start]
        pm = CAL.train_pick([r for r in tr if r[2] == "pick"])
        bms = [((lo, hi), CAL.train_ban([r for r in tr if r[2] == "ban" and lo <= r[5] <= hi], gday)) for lo, hi in MM.SEGMENTS["ban"]]
        log.info("  %s M4 학습 (%.0f초)", name, time.time() - t0)
        turns = []
        for gid in sorted(gids, key=lambda g: gday[g]):
            recs = sb.a[sb.a["GameId"] == gid].sort_values("step")
            if len(recs) != 20 or recs["cid"].isna().any():
                continue
            fp = recs.loc[recs["pick_order"] == "fp", "team"].iloc[0]
            lp = recs.loc[recs["pick_order"] == "lp", "team"].iloc[0]
            blue = fp if side.get((gid, fp)) == "Blue" else lp
            D = recs["day"].iloc[0]
            share, role_ok, role_any = ST.role_arrays(sb.day(D), champs)
            mid, ng = recs["MatchId"].iloc[0], int(recs["N_GameInMatch"].iloc[0])
            fear = set(sb.a[(sb.a["MatchId"] == mid) & (sb.a["N_GameInMatch"] < ng) & (sb.a["action"] == "pick")]["cid"])
            act = {int(r.step): (r.cid, r.team, r.action, r.role if isinstance(r.role, str) else None) for r in recs.itertuples()}
            for t in range(1, 21):
                c_t, tm_t, ac_t, _ = act[t]
                if ac_t != "ban" or c_t not in ci:
                    continue
                opp = lp if tm_t == fp else fp
                sc = RP.score_rows(sb.replay(gid, t), pm, bms)
                FP = {}
                for s, (cands, z) in sc.items():
                    p = CAL.probs(z, calib[CAL.PHASE(s)]["tau"], calib[CAL.PHASE(s)]["eps"])
                    v = np.zeros(n)
                    p = np.asarray(p)
                    v[[ci[c] for c in cands if c in ci]] = p[[k for k, c in enumerate(cands) if c in ci]]
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
                seed = zlib.crc32(f"{gid}/{t}".encode())
                # 기준 굴리기 = 이 밴 차례를 평소 밴 확률(M4)로 뽑음 → 우리 쪽 값 평균(고정) · 상대가 남은 픽에서 고를 확률
                _, v0, E0 = ST.rollout(FP, t, fp, lp, taken0, open0, share, role_ok, role_any, R, seed, VAL=VAL, val0=val0)
                opp_steps = [s for s in range(t + 1, 21) if V6.ORDER[s - 1][0] == "pick" and (fp if V6.ORDER[s - 1][1] == "fp" else lp) == opp]
                p_opp = E0[opp_steps].sum(0)
                us0 = float(v0[tm_t].mean())
                best_v = np.where((np.asarray(role_ok) > 0) & open0[opp][None, :], VAL, -np.inf).max(1)
                best_v = np.where(np.isfinite(best_v), best_v, 0.0)
                threat = np.where(taken0, -np.inf, p_opp * best_v)
                cand = [j for j in np.argsort(-threat)[:12] if np.isfinite(threat[j])]
                hb = FP[t]
                habit = [j for j in np.argsort(-hb) if not taken0[j]][:3]
                cand = list(dict.fromkeys(cand + habit))
                jt = ci[c_t]
                in_cand = jt in cand
                if not in_cand:
                    cand.append(jt)
                wp, vo = {}, {}
                for j in cand:
                    _, vv, _ = ST.rollout(FP, t, fp, lp, taken0, open0, share, role_ok, role_any, R, seed, forced=j, VAL=VAL, val0=val0)
                    vo[j] = float(vv[opp].mean())
                    wp[j] = float((1 / (1 + np.exp(-(us0 - vv[opp]) / PP))).mean())
                rec_c = [j for j in cand if in_cand or j != jt]
                by = sorted(rec_c, key=lambda j: -wp[j])
                top = by[0]
                later = [act[s] for s in range(t + 1, 21)]
                turns.append({"set": name, "gid": gid, "step": t, "team": tm_t, "is_blue": tm_t == blue, "in_cand": in_cand, "n_cand": len(rec_c),
                              "wp_actual": wp[jt], "wp_best": wp[top], "rank": by.index(jt) + 1 if jt in by else np.nan,
                              "rec_top1": jt == top, "rec_top5": jt in by[:5],
                              "habit_top1": bool(habit) and jt == habit[0], "habit_top5": jt in [j for j in np.argsort(-hb) if not taken0[j]][:5],
                              "threat_top1": jt == cand[0],
                              "top": champs[top], "top_p_opp": float(p_opp[top]),
                              "top_opp_picked": any(c == champs[top] and tm == opp and ac == "pick" for c, tm, ac, _ in later),
                              "top_banned_later": any(c == champs[top] and ac == "ban" for c, tm, ac, _ in later),
                              "top_us_picked": any(c == champs[top] and tm == tm_t and ac == "pick" for c, tm, ac, _ in later),
                              "actual": c_t, "actual_p_opp": float(p_opp[jt]),
                              "opp_drop": vo[jt] - np.mean([vo[j] for j in rec_c])})
            log.info("    %s %s 끝 (%.0f초)", name, gid, time.time() - t0)
        pd.to_pickle(turns, fo)
    report()


def report():
    T = []
    for name in RC.ALL_SETS:
        f = OUT / f"ban_{name}.pkl"
        if f.exists():
            T += pd.read_pickle(f)
    t = pd.DataFrame(T)
    GP, TP = [], []
    for name in RC.ALL_SETS:
        f = OUT / f"raw_{name}.pkl"
        if f.exists():
            g_, t_ = pd.read_pickle(f)
            GP += g_
            TP += t_
    g = pd.DataFrame(GP).dropna(subset=["blue_win", "elo_diff"]).sort_values("gid").reset_index(drop=True).set_index("gid")
    t["miss"] = (t["wp_best"] - t["wp_actual"]) * 100
    per = t.groupby(["gid", "is_blue"])["miss"].sum().unstack()
    tp = pd.DataFrame(TP)
    tp["miss"] = (tp["wp_best"] - tp["wp_actual"]) * 100
    perp = tp.groupby(["gid", "is_blue"])["miss"].sum().unstack()
    g2 = g.join(per.rename(columns={True: "bb", False: "br"}), how="inner").join(perp.rename(columns={True: "pb", False: "pr"}), how="left")
    g2 = g2.sort_index()
    g2["db"], g2["dp"] = g2["bb"] - g2["br"], g2["pb"] - g2["pr"]
    y = g2["blue_win"].to_numpy(float)
    f1 = RC.logit_fit(np.column_stack([g2["elo_diff"], g2["db"] / PP]), y)
    c1 = f1.conf_int()
    ok = c1[2][1] < 0
    m2 = g2["dp"].notna().to_numpy()
    f2 = RC.logit_fit(np.column_stack([g2["elo_diff"][m2], g2["db"][m2] / PP, g2["dp"][m2] / PP]), y[m2])
    c2 = f2.conf_int()
    L = ["# M5 밴 검증 — 밴 추천에서 벗어난 만큼 졌나 (학습 밖)", "",
         f"- LCK {len(g2)}경기 · 밴 차례 {len(t)}번. 세트마다 그 전 자료로 만든 이길 가치 표·M4. 굴리기 {R}번 (후보끼리 공통 난수)",
         "- 밴 가치 = 그 챔피언을 뺀 채 굴렸을 때 상대 완성 조합 가치가 얼마나 낮아지나 (우리 쪽 값은 고정, 대시보드와 같음). 숙련 δ·1수 앞보기 없음",
         "- 관찰 자료라 인과 아님. 기준 (돌리기 전): 밴 놓친 몫 기울기 구간 상한 < 0", "",
         "## ① 밴 놓친 몫 → 실제 승패", "",
         f"- 밴 차례당 놓친 몫 평균 {t['miss'].mean():.2f}%p (중앙 {t['miss'].median():.2f}) · 경기당 팀 합 평균 {pd.concat([g2['bb'], g2['br']]).mean():.1f}%p",
         f"- 블루 승 ~ Elo + (블루 − 레드 밴 놓친 몫)/25: 기울기 **{f1.params[2]:.2f}** [95% {c1[2][0]:.2f}, {c1[2][1]:.2f}], p = {f1.pvalues[2]:.3f}",
         f"- 픽 놓친 몫과 같이 ({int(m2.sum())}경기): 밴 {f2.params[2]:.2f} [{c2[2][0]:.2f}, {c2[2][1]:.2f}] · 픽 {f2.params[3]:.2f} [{c2[3][0]:.2f}, {c2[3][1]:.2f}]",
         f"- **판단: {'밴 추천이 실제 승패와 같은 방향 → 유지' if ok else '기준 미달 → 밴 추천은 근거 없음으로 표시'}**", "",
         "| 블루 − 레드 밴 놓친 몫 3분위 | 경기 | 평균 차 | Elo·진영만 기대 블루 승률 | 실제 블루 승률 | 실제 − 기대 |", "|---|---|---|---|---|---|"]
    b3 = pd.qcut(g2["db"], 3, labels=["블루가 덜 놓침", "비슷", "블루가 더 놓침"])
    for b, q in g2.groupby(b3, observed=True):
        e = RC.sigm(q["side_a"] + q["elo_b"] * q["elo_diff"]).mean()
        L.append(f"| {b} | {len(q)} | {q['db'].mean():+.1f}%p | {e:.1%} | {q['blue_win'].mean():.1%} | {q['blue_win'].mean() - e:+.1%} |")
    ph = t["step"].map(lambda s: "1밴 (1~6)" if s <= 6 else "2밴 (13~16)")
    L += ["", "## ② 밴 1순위가 풀렸을 때 (예측 점검)", "",
          "| 단계 | 밴 차례 | 1순위를 실제로 밴 | 1순위가 끝까지 남아 상대가 픽 | 그때 예측 확률 평균 | 뒤에서 누군가 밴 | 우리가 픽 |", "|---|---|---|---|---|---|---|"]
    for lab, q in t.groupby(ph):
        o = q[~q["rec_top1"]]
        L.append(f"| {lab} | {len(q)} | {q['rec_top1'].mean():.1%} | {o['top_opp_picked'].mean():.1%} | {o['top_p_opp'].mean():.1%} | {o['top_banned_later'].mean():.1%} | {o['top_us_picked'].mean():.1%} |")
    o = t[~t["rec_top1"]]
    L += ["", f"- 1순위를 안 밴한 {len(o)}번: 상대가 실제로 그 챔피언을 가져감 {o['top_opp_picked'].mean():.1%} vs 굴리기 예측 {o['top_p_opp'].mean():.1%}",
          "", "## ③ 일치율 (참고)", "", "| 방식 | 1순위 일치 | 상위 5 안 |", "|---|---|---|",
          f"| 평소 밴 (M4) | {t['habit_top1'].mean():.1%} | {t['habit_top5'].mean():.1%} |",
          f"| 밴 추천 (상대 조합 가치 감소) | {t['rec_top1'].mean():.1%} | {t['rec_top5'].mean():.1%} |",
          f"| 상대 위협 1위 | {t['threat_top1'].mean():.1%} | — |", "",
          f"- 실제 밴이 후보 안 {t['in_cand'].mean():.1%} · 후보 수 평균 {t['n_cand'].mean():.1f}",
          f"- 밴 1순위 상위 5 비중 {100 * t['top'].value_counts().head(5).sum() / len(t):.1f}% ({' · '.join(f'{c} {100 * v / len(t):.1f}%' for c, v in t['top'].value_counts().head(3).items())})",
          f"- T1 밴 차례 ({(t['team'] == 'T1').sum()}번): 추천 1순위 {t.loc[t['team'] == 'T1', 'rec_top1'].mean():.1%} · 상위 5 {t.loc[t['team'] == 'T1', 'rec_top5'].mean():.1%} · 놓친 몫 평균 {t.loc[t['team'] == 'T1', 'miss'].mean():.2f}%p"]
    (OUT / "m5_ban_check.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        main()
