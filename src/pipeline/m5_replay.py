"""M5 리허설 드래프트 재생 검증 (2026-11-08)

리허설 세트(LCK 26.14 · 26.15 · Road to MSI)의 실제 경기를 단계마다 '그 직전 상태'로 되돌려
M5 시험판이 쓰는 계산(지금 상태에서 남은 단계 M4 확률 → 남은 드래프트 굴리기)을 실제 결과와 비교.
  - 모델: 세트마다 그 세트 시작 전 자료로만 최종 설정 그대로 다시 학습 (m5_calibrate 와 같음) → 학습 밖 점수
  - 확률: calibration.json 페이즈별 τ (같은 리허설 단계로 맞춘 값이라 다음 단계 확률은 조금 낙관적. 아래 비교는 같은 확률을 쓰므로 공정)
  - 상태 → 남은 단계 행: m5_state.StateBuilder (유령 기록, 학습 표와 특징이 완전히 같음을 --parity 로 확인)
재는 것
  ① 거리별: 단계 s 의 실제 선택 확률을 상태 t 에서 미리 낸 값 (s − t = 0 이면 다음 단계). 원래 확률 vs 굴리기 주변 확률(중복 제외·빈 포지션 반영)
     굴리기 확률 = 굴리기마다 그 단계 '뽑기 분포'의 평균 (선택 횟수로 세면 500번에 안 나온 챔피언이 0 근처가 돼 로그 손실이 부풀음 — 2026-11-08 첫 실행에서 고침)
  ② 남은 픽: "팀 X 가 이번 페이즈(또는 이번 경기) 남은 픽에서 챔피언 c 를 고름" — 예전 독립 합 1 − Π(1 − p_s) vs 굴리기 비율
     로그 손실·브라이어·합(기대 개수 / 실제 개수)·보정 곡선
  ③ (참고) T1 픽 차례: 실제 T1 픽이 추천 상위 5 안에 있나 — 추천(굴리기 + 이길 가치) vs T1 평소 확률. 이길 가치 표는 08-25 까지 전부로 만든 값이라 겹침
플레이오프(08-26~)는 안 봄.

    python src/pipeline/m5_replay.py [--R 500]
"""
from __future__ import annotations

import json
import sys
import time
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v6 as V6  # noqa: E402
import m4_v7 as V7  # noqa: E402
import m5_calibrate as CAL  # noqa: E402
import m5_state as ST  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m5"
PHASE = CAL.PHASE
POS = ST.V.POS
R = int(sys.argv[sys.argv.index("--R") + 1]) if "--R" in sys.argv else 500
US = "T1"
# --sets 26.08,26.09,26.10 = 리허설에 안 쓴 정규시즌 LCK 패치로 재생 (2026-11-08: 포지션 보정을 리허설 재생을 보고 바꿨으므로 처음 보는 경기로 다시 확인)
SETS = sys.argv[sys.argv.index("--sets") + 1].split(",") if "--sets" in sys.argv else None
ROLE_MODE = "hard" if "--hard-role" in sys.argv else "ratio"    # 비교용: 예전 포지션 거르기
TAG = ("" if SETS is None else "_" + "_".join(SETS)) + ("_hard" if ROLE_MODE == "hard" else "")


def pick_sets(a_full):
    lck = a_full[a_full["league"] == "LCK"]
    if SETS is None:
        return B.eval_sets(lck)
    g = lck.drop_duplicates("GameId")
    rtm = g["OverviewPage"].astype(str).str.contains("Road to MSI")
    # 드래프트 표 Patch 칸은 숫자로 읽혀 26.10 → "26.1" 로 남아 있음 (CLAUDE.md 0장 주의) → 둘 다 맞춤
    return {p: set(g[g["Patch"].astype(str).isin({p, p.rstrip("0")}) & ~rtm]["GameId"]) for p in SETS}


def score_rows(rows, pm, bms):
    """행 목록 → {단계: (후보, 점수)}."""
    out = {}
    pr = [r for r in rows if r[2] == "pick"]
    if pr:
        sid, _, X, _ = V7.mats(pr, "pick", False)
        sc = pm.predict(np.hstack([X, np.ones((len(X), 1))]))
        for r in pr:
            out[r[5]] = (r[8]["cands"], sc[sid == r[0]])
    for (lo, hi), bm in bms:
        br = [r for r in rows if r[2] == "ban" and lo <= r[5] <= hi]
        if not br:
            continue
        sid, _, X, _ = V7.mats(br, "ban", True)
        lin = ((X - bm["mu"]) / bm["sd"]) @ bm["coef"]
        gb = bm["gbm"].predict(np.hstack([X, np.ones((len(X), 1))]))
        sc = V6.zscore_within(sid, lin) + V6.zscore_within(sid, gb)
        for r in br:
            out[r[5]] = (r[8]["cands"], sc[sid == r[0]])
    return out


def main():
    t0 = time.time()
    sb = ST.StateBuilder()
    champs = list(sb.champs)
    ci = {c: i for i, c in enumerate(champs)}
    n = len(champs)
    calib = json.loads((OUT / "calibration.json").read_text(encoding="utf-8"))["phase"]
    rows_all, a_full, *_ = pd.read_pickle(B.OUT / "m4_v6_rows.pkl")
    if "is_winner" not in a_full.columns:
        a_full["is_winner"] = np.nan
    rows_all = V7.series_features(rows_all, a_full, V7.winners(a_full))
    gday = a_full.drop_duplicates("GameId").set_index("GameId")["day"]
    sets = pick_sets(a_full)
    val = pd.read_csv(OUT / "m5_value_champion.csv").set_index("key")["value_pro"].to_dict()
    VAL = np.array([[val.get(f"{c}@{r}", 0.0) for r in POS] for c in champs])
    log.info("자료 준비 (%.0f초)", time.time() - t0)

    hor, ev, rec, nxt = [], defaultdict(list), [], []
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        tr = [r for r in rows_all if gday[r[1]] < start]
        pm = CAL.train_pick([r for r in tr if r[2] == "pick"])
        bms = [((lo, hi), CAL.train_ban([r for r in tr if r[2] == "ban" and lo <= r[5] <= hi], gday)) for lo, hi in MM.SEGMENTS["ban"]]
        log.info("  %s 모델 학습 (%.0f초)", name, time.time() - t0)
        for gid in sorted(gids, key=lambda g: gday[g]):
            recs = sb.a[sb.a["GameId"] == gid].sort_values("step")
            if len(recs) != 20 or recs["cid"].isna().any():
                continue
            fp = recs.loc[recs["pick_order"] == "fp", "team"].iloc[0]
            lp = recs.loc[recs["pick_order"] == "lp", "team"].iloc[0]
            D = recs["day"].iloc[0]
            share, role_ok, role_any = ST.role_arrays(sb.day(D), champs)
            mid, ng = recs["MatchId"].iloc[0], int(recs["N_GameInMatch"].iloc[0])
            fear = set(sb.a[(sb.a["MatchId"] == mid) & (sb.a["N_GameInMatch"] < ng) & (sb.a["action"] == "pick")]["cid"])
            act = {int(r.step): (r.cid, r.team, r.action, r.role if isinstance(r.role, str) else None) for r in recs.itertuples()}
            for t in range(1, 21):
                sc = score_rows(sb.replay(gid, t), pm, bms)
                FP = {}
                for s, (cands, z) in sc.items():
                    p = CAL.probs(z, calib[PHASE(s)]["tau"], calib[PHASE(s)]["eps"])
                    v = np.zeros(n)
                    v[[ci[c] for c in cands]] = p
                    FP[s] = v
                taken0 = np.zeros(n, bool)
                for c in fear:
                    if c in ci:
                        taken0[ci[c]] = True
                open0 = {fp: np.ones(5, bool), lp: np.ones(5, bool)}
                for s in range(1, t):
                    c, tm, ac, ro = act[s]
                    taken0[ci[c]] = True
                    if ac == "pick" and ro in POS:
                        open0[tm][POS.index(ro)] = False
                seed = zlib.crc32(f"{gid}/{t}".encode())
                _, _, E = ST.rollout(FP, t, fp, lp, taken0, open0, share, role_ok, role_any, R, seed, role_mode=ROLE_MODE)
                # ① 거리별
                for s in range(t, 21):
                    j = ci[act[s][0]]
                    hor.append((name, t, s, PHASE(s), FP[s][j], E[s, j]))
                # ② 남은 픽 (팀 · 이번 페이즈 / 이번 경기)
                cand_t = [ci[c] for c in sc[t][0]]
                for tm in (fp, lp):
                    po = "fp" if tm == fp else "lp"
                    end_ph = 12 if t <= 12 else 20
                    for hz, end in (("페이즈", end_ph), ("경기", 20)):
                        S = [s for s in range(t, end + 1) if V6.ORDER[s - 1][0] == "pick" and V6.ORDER[s - 1][1] == po]
                        if not S:
                            continue
                        y = np.zeros(n)
                        for s in S:
                            y[ci[act[s][0]]] = 1
                        ind = 1 - np.prod([1 - FP[s] for s in S], axis=0)
                        mc = np.minimum(E[S].sum(0), 1.0)
                        k = np.array(cand_t)
                        ev[hz].append((ind[k], mc[k], y[k], len(S), tm == US))
                # ③ T1 픽 차례 추천 (참고)
                c_t, tm_t, ac_t, ro_t = act[t]
                if tm_t == US and ac_t == "pick":
                    p_us = FP[t]
                    order = np.argsort(-p_us)
                    cand = [j for j in order if role_ok[j][open0[US]].any()]
                    cand = [j for i, j in enumerate(cand) if i < 3 or p_us[j] >= 0.03][:15]
                    opp = lp if US == fp else fp
                    val0 = {fp: 0.0, lp: 0.0}
                    for s in range(1, t):
                        c, tm, ac, ro = act[s]
                        if ac == "pick" and ro in POS:
                            val0[tm] += VAL[ci[c], POS.index(ro)]
                    wp = {}
                    for j in cand:
                        _, vv, _ = ST.rollout(FP, t, fp, lp, taken0, open0, share, role_ok, role_any, 300, seed, forced=j, VAL=VAL, val0=val0)
                        wp[j] = float((1 / (1 + np.exp(-(vv[US] - vv[opp]) / 25.0))).mean())
                    by_mc = sorted(wp, key=lambda j: -wp[j])
                    jt = ci[c_t]
                    rec.append({"set": name, "gid": gid, "step": t, "in_cand": jt in cand, "rec_top1": jt == by_mc[0],
                                "rec_top5": jt in by_mc[:5], "habit_top5": jt in list(order[:5]), "habit_top1": jt == order[0],
                                "n_cand": len(cand), "wp_actual": wp.get(jt, np.nan) * 100, "wp_best": wp[by_mc[0]] * 100})
            log.info("    %s %s 끝 (%.0f초)", name, gid, time.time() - t0)
    pd.to_pickle((hor, dict(ev), rec), OUT / f"m5_replay_raw{TAG}.pkl")
    report(hor, ev, rec, sets)
    log.info("끝 (%.0f초)", time.time() - t0)


def ll(p):
    return float(-np.log(np.clip(p, 1e-6, 1)).mean())


def bern(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()), float(((p - y) ** 2).mean())


def report(hor, ev, rec, sets):
    hd = pd.DataFrame(hor, columns=["set", "t", "s", "phase", "raw", "mc"])
    hd["d"] = hd["s"] - hd["t"]
    hd["db"] = pd.cut(hd["d"], [-1, 0, 1, 3, 6, 20], labels=["0 (다음 단계)", "1", "2~3", "4~6", "7+"])
    L = ["# M5 드래프트 재생 검증" + ("" if SETS is None else f" — 리허설에 안 쓴 정규시즌 패치 {' · '.join(SETS)}"), "",
         f"- LCK {sum(len(v) for v in sets.values())}경기 ({' · '.join(sets)}), 경기마다 단계 1~20 직전 상태로 되돌림. 모델은 세트 시작 전 자료로 다시 학습 (학습 밖)",
         f"- 남은 드래프트 굴리기 {R}번 (m5_sim 과 같은 엔진: 중복 없이 · 그 팀 빈 포지션만). 확률 = calibration.json τ", "",
         "## ① 거리별 — 단계 s 실제 선택의 로그 손실을 상태 t 에서 (낮을수록 좋음)", "",
         "| s − t | 단계 수 | 지금 상태 M4 확률 그대로 | 굴리기 주변 확률 (중복·포지션 반영) |", "|---|---|---|---|"]
    for b, g in hd.groupby("db", observed=True):
        L.append(f"| {b} | {len(g)} | {ll(g['raw']):.3f} | {ll(g['mc']):.3f} |")
    L += ["", "| 페이즈 (s 기준, s − t ≥ 1) | 단계 수 | 그대로 | 굴리기 |", "|---|---|---|---|"]
    for ph, g in hd[hd["d"] >= 1].groupby("phase", sort=False):
        L.append(f"| {ph} | {len(g)} | {ll(g['raw']):.3f} | {ll(g['mc']):.3f} |")
    L += ["", "## ② 남은 픽 — \"팀 X 가 남은 픽에서 c 를 고름\" (상태마다 그때 남은 후보 전체)", "",
          "| 범위 | 사건 수 (상태 × 팀) | 방식 | 로그 손실 | 브라이어 | 예측 합 / 실제 개수 | 상위 k 안 실제 비율 (k = 남은 픽 수) |", "|---|---|---|---|---|---|---|"]
    cal_rows = {}
    for hz, items in ev.items():
        ind = np.concatenate([x[0] for x in items])
        mc = np.concatenate([x[1] for x in items])
        y = np.concatenate([x[2] for x in items])
        for nm, P in (("예전 독립 합", ind), ("굴리기", mc)):
            l_, b_ = bern(P, y)
            ratio = np.mean([x[0 if nm == "예전 독립 합" else 1].sum() / x[3] for x in items])
            hit = np.mean([x[2][np.argsort(-x[0 if nm == "예전 독립 합" else 1])[:x[3]]].sum() / max(1, x[2].sum()) for x in items])
            L.append(f"| 이번 {hz} | {len(items)} | {nm} | {l_:.4f} | {b_:.5f} | {ratio:.2f} | {hit:.1%} |")
            cal_rows[(hz, nm)] = (P, y)
    L += ["", "### 보정 곡선 (이번 페이즈)", "", "| 예측 구간 | 예전 독립 합: 개수 · 평균 예측 · 실제 | 굴리기: 개수 · 평균 예측 · 실제 |", "|---|---|---|"]
    bins = [0, 0.01, 0.03, 0.1, 0.2, 0.35, 0.5, 0.7, 1.01]
    for lo, hi in zip(bins[:-1], bins[1:]):
        cells = []
        for nm in ("예전 독립 합", "굴리기"):
            P, y = cal_rows[("페이즈", nm)]
            m = (P >= lo) & (P < hi)
            cells.append(f"{int(m.sum())} · {P[m].mean():.1%} · {y[m].mean():.1%}" if m.sum() else "—")
        L.append(f"| {lo:.0%}~{min(hi, 1):.0%} | {cells[0]} | {cells[1]} |")
    if rec:
        rd = pd.DataFrame(rec)
        L += ["", f"## ③ (참고) T1 픽 차례 {len(rd)}번 — 실제 T1 픽이 상위 안에 있나", "",
              "| 방식 | 1순위 일치 | 상위 5 안 |", "|---|---|---|",
              f"| T1 평소 확률 (M4) | {rd['habit_top1'].mean():.1%} | {rd['habit_top5'].mean():.1%} |",
              f"| 추천 (굴리기 + 이길 가치, 후보 = 평소 확률 3% 이상) | {rd['rec_top1'].mean():.1%} | {rd['rec_top5'].mean():.1%} |", "",
              f"- 실제 픽이 추천 후보 안에 있었던 비율 {rd['in_cand'].mean():.1%} · 후보 수 평균 {rd['n_cand'].mean():.1f}",
              f"- 굴리기 예상 승률: 실제 픽 {rd['wp_actual'].mean():.1f}% vs 추천 1순위 {rd['wp_best'].mean():.1f}% (후보 안에 있던 경우, 이길 가치 합만 · 팀 실력 차 0)",
              "- 일치율은 참고만: 높아도 '하던 걸 따라 함', 낮아도 추천이 틀렸다는 뜻이 아님. 이길 가치 표가 08-25 까지 전부로 만든 값이라 리허설과 겹침"]
    (OUT / f"m5_replay{TAG}.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    if "--report" in sys.argv:
        hor, ev, rec = pd.read_pickle(OUT / f"m5_replay_raw{TAG}.pkl")
        a_full = pd.read_pickle(B.OUT / "m4_v6_rows.pkl")[1]
        report(hor, ev, rec, pick_sets(a_full))
    else:
        main()
