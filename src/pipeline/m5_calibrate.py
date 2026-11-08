"""M5 1단계 — M4 점수 → 확률 보정 (2026-11-08)

문제: m5_sim 시험판은 온도 τ 를 08-19~08-25 경기로 맞췄는데, 그 경기는 최종 모델 학습 범위 안 → 과신(τ 가 너무 작음)
방법: 최종 설정 그대로(m4_freeze.py 와 같은 특징·하이퍼파라미터) 리허설 세트(LCK 26.14 · 26.15 · RTM) **시작 전 자료로만** 학습
      → 리허설 단계의 학습 밖 점수 → 소프트맥스 p = (1 − ε)·softmax(s / τ) + ε / 후보 수 의 τ·ε 를 로그 손실로 맞춤
  - 페이즈별(1밴 · 1픽 · 2밴 · 2픽) τ 와 전체 τ 를 비교. 세트 하나를 빼고 맞춰 그 세트로 평가(세트 교차)해 과적합 확인
  - 같은 리허설 단계를 최종 모델(학습 안)으로 맞춘 τ 도 같이 보고 → 차이 = 학습 안 보정의 과신 정도
  - 점수 크기 비교: 픽 LightGBM 은 300회 고정이라 학습 자료 양에 따라 점수 폭이 다를 수 있음 → 단계 안 점수 표준편차를 최종·리허설 모델 학습 자료에서 비교
  - 플레이오프(08-26~)는 쓰지 않음
출력: m5/calibration.json (m5_sim 이 읽음), m5/calibration.md

    python src/pipeline/m5_calibrate.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v6 as V6  # noqa: E402
import m4_v6b as V6B  # noqa: E402
import m4_v7 as V7  # noqa: E402
import m4_validate as MV  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m5"
PHASE = lambda s: "1밴" if s <= 6 else ("1픽" if s <= 12 else ("2밴" if s <= 16 else "2픽"))  # noqa: E731
TAUS = np.exp(np.linspace(np.log(0.1), np.log(5.0), 80))
EPS = [0.0, 0.002, 0.005, 0.01, 0.02, 0.05]


def train_pick(trg):
    import lightgbm as lgb
    sid, y, X, lck = V7.mats(trg, "pick", False)
    order = np.argsort(sid, kind="stable")
    _, cnt = np.unique(sid[order], return_counts=True)
    d = lgb.Dataset(np.hstack([X, lck[:, None]])[order], label=y[order], group=cnt, weight=np.where(lck[order] == 1, 3.0, 1.0))
    return lgb.train(V6B.PARAMS, d, num_boost_round=300)


def train_ban(trg, gday):
    sid, y, X, lck = V7.mats(trg, "ban", True)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    m = lck == 1
    b = MM.fit_off(sid[m], y[m], (X[m] - mu) / sd, np.zeros(m.sum()), 1.0)
    gb, best = V7.lgb_es(trg, gday, "ban", True)
    return {"mu": mu, "sd": sd, "coef": b, "gbm": gb, "rounds": best}


def score_pick(model, rr):
    sid, y, X, _ = V7.mats(rr, "pick", False)
    return sid, y, model.predict(np.hstack([X, np.ones((len(X), 1))]))


def score_ban(m, rr):
    sid, y, X, _ = V7.mats(rr, "ban", True)
    lin = ((X - m["mu"]) / m["sd"]) @ m["coef"]
    gb = m["gbm"].predict(np.hstack([X, np.ones((len(X), 1))]))
    return sid, y, V6.zscore_within(sid, lin) + V6.zscore_within(sid, gb)


def steps_of(sid, y, s):
    """단계별 (점수 배열, 정답 위치) 목록."""
    out = []
    order = np.argsort(sid, kind="stable")
    sid, y, s = sid[order], y[order], s[order]
    cut = np.flatnonzero(np.diff(sid)) + 1
    for ys, ss in zip(np.split(y, cut), np.split(s, cut)):
        if ys.sum() == 1:
            out.append((ss, int(ys.argmax())))
    return out


def pack(steps):
    """단계 목록 → (점수 행렬, 정답 위치, 후보 수). 빈 칸은 −inf."""
    n = max(len(s) for s, _ in steps)
    S = np.full((len(steps), n), -np.inf)
    for i, (s, _) in enumerate(steps):
        S[i, :len(s)] = s
    return S, np.array([j for _, j in steps]), np.array([len(s) for s, _ in steps], float)


def nll(steps, tau, eps, packed=None):
    S, J, N = packed if packed is not None else pack(steps)
    Z = S / tau
    mx = Z.max(1, keepdims=True)
    lse = np.log(np.exp(Z - mx).sum(1)) + mx[:, 0]
    pj = np.exp(Z[np.arange(len(J)), J] - lse)
    return float(-np.log((1 - eps) * pj + eps / N).mean())


def fit(steps):
    pk = pack(steps)
    best = None
    for e in EPS:
        for t in TAUS:
            v = nll(steps, t, e, pk)
            if best is None or v < best[0]:
                best = (v, float(t), e)
    return {"nll": best[0], "tau": best[1], "eps": best[2]}


def probs(s, tau, eps):
    z = s / tau
    p = np.exp(z - z.max())
    p /= p.sum()
    return (1 - eps) * p + eps / len(s)


def reliability(steps, tau, eps):
    """후보 전체를 예측 확률 구간으로 묶어 실제 선택 비율과 비교 + top1 확률 vs 적중."""
    P, Y, T1, A1 = [], [], [], []
    for s, j in steps:
        p = probs(s, tau, eps)
        y = np.zeros(len(s))
        y[j] = 1
        P.append(p)
        Y.append(y)
        T1.append(p.max())
        A1.append(float(p.argmax() == j))
    P, Y = np.concatenate(P), np.concatenate(Y)
    bins = [0, 0.01, 0.03, 0.07, 0.15, 0.3, 0.5, 1.0]
    rows = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (P >= lo) & (P < hi)
        if m.sum():
            rows.append((f"{lo:.0%}~{hi:.0%}", int(m.sum()), float(P[m].mean()), float(Y[m].mean())))
    ece = sum(n * abs(pm - ym) for _, n, pm, ym in rows) / len(P)
    return rows, float(np.mean(T1)), float(np.mean(A1)), ece


def within_sd(sid, s):
    df = pd.DataFrame({"sid": sid, "s": s})
    return float(df.groupby("sid")["s"].std().mean())


def main():
    t0 = time.time()
    OUT.mkdir(exist_ok=True)
    rows, a, fear, ds_days = pd.read_pickle(B.OUT / "m4_v6_rows.pkl")
    if "is_winner" not in a.columns:
        a["is_winner"] = np.nan
    rows = V7.series_features(rows, a, V7.winners(a))
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    sets = B.eval_sets(a[a["league"] == "LCK"])
    spec, fpick, fbans = MV.load_final()
    log.info("자료 준비 (%.0f초)", time.time() - t0)

    oos = {}       # (세트, 페이즈) → 단계 목록 (학습 밖)
    ins = {}       # 같은 단계, 최종 모델 점수 (학습 안)
    sd_cmp = []
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        tr = [r for r in rows if gday[r[1]] < start]
        te = [r for r in rows if r[1] in gids]
        # 픽
        trg = [r for r in tr if r[2] == "pick"]
        teg = [r for r in te if r[2] == "pick"]
        pm = train_pick(trg)
        sid, y, s = score_pick(pm, teg)
        sid_f, y_f, s_f = score_pick(fpick, teg)
        st_of = {r[0]: r[5] for r in teg}
        for ph in ("1픽", "2픽"):
            m = np.array([PHASE(st_of[k]) == ph for k in sid])
            oos[(name, ph)] = steps_of(sid[m], y[m], s[m])
            ins[(name, ph)] = steps_of(sid_f[m], y_f[m], s_f[m])
        # 점수 폭 비교 (각자 학습 자료에서): 리허설 모델 vs 최종 모델
        s_tr = score_pick(pm, trg[-3000:])
        allp = [r for r in rows if r[2] == "pick"]
        s_fl = score_pick(fpick, allp[-3000:])
        sd_cmp.append((name, "픽", within_sd(s_tr[0], s_tr[2]), within_sd(s_fl[0], s_fl[2]), within_sd(sid, s), within_sd(sid_f, s_f)))
        # 밴
        for (lo, hi), (seg, fgbm) in zip(MM.SEGMENTS["ban"], fbans):
            trg = [r for r in tr if r[2] == "ban" and lo <= r[5] <= hi]
            teg = [r for r in te if r[2] == "ban" and lo <= r[5] <= hi]
            bm = train_ban(trg, gday)
            sid, y, s = score_ban(bm, teg)
            fm = {"mu": np.array(seg["linear"]["mu"]), "sd": np.array(seg["linear"]["sd"]), "coef": np.array(seg["linear"]["coef"]), "gbm": fgbm}
            sid_f, y_f, s_f = score_ban(fm, teg)
            ph = PHASE(lo)
            oos[(name, ph)] = steps_of(sid, y, s)
            ins[(name, ph)] = steps_of(sid_f, y_f, s_f)
        log.info("  %s 끝 (%.0f초)", name, time.time() - t0)

    phases = ["1밴", "1픽", "2밴", "2픽"]
    act = {"1밴": "ban", "2밴": "ban", "1픽": "pick", "2픽": "pick"}
    names = list(sets)
    res = {"phase": {}, "action": {}, "insample": {}, "cv": [], "sd": sd_cmp}
    # 페이즈별 · 종류별 (리허설 세 세트 합)
    for ph in phases:
        st = sum((oos[(n, ph)] for n in names), [])
        res["phase"][ph] = fit(st) | {"steps": len(st)}
        sti = sum((ins[(n, ph)] for n in names), [])
        res["insample"][ph] = fit(sti) | {"steps": len(sti)}
    for ac in ("pick", "ban"):
        st = sum((oos[(n, ph)] for n in names for ph in phases if act[ph] == ac), [])
        res["action"][ac] = fit(st) | {"steps": len(st)}
    # 세트 교차: 한 세트를 빼고 맞춘 τ·ε 로 그 세트 평가 (페이즈별 vs 종류별 vs 고정 τ=1)
    for held in names:
        for ph in phases:
            tr_st = sum((oos[(n, ph)] for n in names if n != held), [])
            tr_ac = sum((oos[(n, q)] for n in names if n != held for q in phases if act[q] == act[ph]), [])
            te = oos[(held, ph)]
            fp_, fa_ = fit(tr_st), fit(tr_ac)
            res["cv"].append({"held": held, "phase": ph, "n": len(te), "per_phase": nll(te, fp_["tau"], fp_["eps"]),
                              "per_action": nll(te, fa_["tau"], fa_["eps"]), "tau1": nll(te, 1.0, 0.0),
                              "insample_tau": nll(te, res["insample"][ph]["tau"], res["insample"][ph]["eps"])})
    cv = pd.DataFrame(res["cv"])
    w = cv.groupby("phase").apply(lambda g: pd.Series({c: np.average(g[c], weights=g["n"]) for c in ("per_phase", "per_action", "tau1", "insample_tau")}))
    use_phase = bool(np.average(cv["per_phase"], weights=cv["n"]) < np.average(cv["per_action"], weights=cv["n"]))
    final = {ph: (res["phase"][ph] if use_phase else res["action"][act[ph]]) for ph in phases}
    out = {"created": time.strftime("%Y-%m-%dT%H:%M:%S"), "method": "p = (1-eps)*softmax(s/tau) + eps/N, 리허설(LCK 26.14·26.15·RTM) 학습 밖 점수로 로그 손실 최소",
           "per_phase": use_phase, "phase": {ph: {"tau": final[ph]["tau"], "eps": final[ph]["eps"]} for ph in phases},
           "insample_tau_0819_0825": json.loads((OUT / "tau.json").read_text()) if (OUT / "tau.json").exists() else None}
    (OUT / "calibration.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # 보고
    L = ["# M5 1단계 — M4 점수 → 확률 보정", "",
         "- 리허설 세트(LCK 26.14 · 26.15 · RTM)마다 **그 세트 시작 전 자료로만** 최종 설정 그대로 다시 학습 → 학습 밖 점수로 τ·ε 맞춤",
         "- p = (1 − ε)·softmax(s / τ) + ε / 후보 수. 로그 손실 = 실제 선택의 −log p (낮을수록 좋음, 무작위 약 log 140 ≈ 4.9)",
         "- 플레이오프(08-26~)는 쓰지 않음", "",
         "## 맞춘 값 (리허설 세 세트 합)", "", "| 페이즈 | 단계 | τ | ε | 로그 손실 | (참고) 최종 모델로 같은 단계를 맞춘 τ — 학습 안 |", "|---|---|---|---|---|---|"]
    for ph in phases:
        r, i = res["phase"][ph], res["insample"][ph]
        L.append(f"| {ph} | {r['steps']} | {r['tau']:.2f} | {r['eps']} | {r['nll']:.3f} | τ {i['tau']:.2f} · 로그 손실 {i['nll']:.3f} |")
    for ac in ("pick", "ban"):
        r = res["action"][ac]
        L.append(f"| {'픽 전체' if ac == 'pick' else '밴 전체'} | {r['steps']} | {r['tau']:.2f} | {r['eps']} | {r['nll']:.3f} | |")
    L += ["", "## 세트 교차 (한 세트를 빼고 맞춘 값으로 그 세트 평가, 로그 손실)", "",
          "| 페이즈 | 페이즈별 τ | 종류별 τ | τ = 1 | 학습 안 τ |", "|---|---|---|---|---|"]
    for ph in phases:
        r = w.loc[ph]
        L.append(f"| {ph} | {r['per_phase']:.3f} | {r['per_action']:.3f} | {r['tau1']:.3f} | {r['insample_tau']:.3f} |")
    L += ["", f"- 채택: **{'페이즈별' if use_phase else '종류별(픽·밴)'} τ·ε** (세트 교차 평균 로그 손실이 더 낮은 쪽)", "",
          "## 보정 곡선 (채택 값, 리허설 세 세트 합 · 후보 전체)", ""]
    for ph in phases:
        st = sum((oos[(n, ph)] for n in names), [])
        rows_, mt1, a1, ece = reliability(st, final[ph]["tau"], final[ph]["eps"])
        L += [f"### {ph} — 1순위 평균 확률 {mt1:.1%} vs 1순위 적중 {a1:.1%}, ECE {ece:.4f}", "",
              "| 예측 확률 구간 | 후보 수 | 평균 예측 | 실제 선택 비율 |", "|---|---|---|---|"]
        L += [f"| {b} | {n} | {pm:.1%} | {ym:.1%} |" for b, n, pm, ym in rows_]
        L.append("")
    L += ["## 점수 폭 (단계 안 표준편차, 픽 LightGBM)", "", "| 세트 | 리허설 모델·자기 학습 자료 | 최종 모델·자기 학습 자료 | 리허설 모델·리허설 단계(학습 밖) | 최종 모델·리허설 단계(학습 안) |", "|---|---|---|---|---|"]
    for n_, _, a_, b_, c_, d_ in sd_cmp:
        L.append(f"| {n_} | {a_:.3f} | {b_:.3f} | {c_:.3f} | {d_:.3f} |")
    L += ["", "- 리허설·최종 모델의 '자기 학습 자료' 점수 폭이 비슷하면 리허설에서 맞춘 τ 를 최종 모델에 그대로 써도 됨. 밴은 단계 안 표준화 점수라 폭이 같음"]
    (OUT / "calibration.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("끝 (%.0f초) → %s", time.time() - t0, OUT / "calibration.md")


if __name__ == "__main__":
    main()
