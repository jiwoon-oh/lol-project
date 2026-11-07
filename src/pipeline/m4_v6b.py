"""M4 P3 확인 — LightGBM 이득이 자료(5리그) 때문인가 모델(비선형) 때문인가 (2026-11-07)

m4_v6.py 의 특징 표 캐시(m4/m4_v6_rows.pkl)로 같은 LightGBM 설정을 LCK 만 / 5리그(LCK 가중 1·3·10)로 학습해 LCK 리허설 비교.
설정 고정 (잎 31 · 학습률 0.05 · 300회 · 잎 최소 100) — 리허설로 설정을 고르지 않음

    python src/pipeline/m4_v6b.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v6 as V6  # noqa: E402

OUT = B.OUT
PARAMS = {"objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [5], "learning_rate": 0.05, "num_leaves": 31,
          "min_data_in_leaf": 100, "feature_fraction": 0.8, "verbose": -1, "lambdarank_truncation_level": 10}


def train_lgb(sid, y, X, lck, w_lck, only_lck):
    import lightgbm as lgb
    m = (lck == 1) if only_lck else np.ones(len(y), bool)
    order = np.argsort(sid[m], kind="stable")
    Xl = np.hstack([X[m], lck[m][:, None]])[order]
    _, cnt = np.unique(sid[m][order], return_counts=True)
    wq = np.where(lck[m][order] == 1, w_lck, 1.0)
    return lgb.train(PARAMS, lgb.Dataset(Xl, label=y[m][order], group=cnt, weight=wq), num_boost_round=300)


def main():
    rows, a, fear, ds_days = pd.read_pickle(OUT / "m4_v6_rows.pkl")
    a_lck = a[a["league"] == "LCK"]
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    sets = B.eval_sets(a_lck)
    variants = {"LightGBM · LCK 만": (1.0, True), "LightGBM · 5리그 w=1": (1.0, False),
                "LightGBM · 5리그 w=3": (3.0, False), "LightGBM · 5리그 w=10": (10.0, False)}
    res = []
    for name, gids in sets.items():
        start = min(gday[g] for g in gids)
        tr = [r for r in rows if gday[r[1]] < start]
        te = [r for r in rows if r[1] in gids]
        for action in ("pick", "ban"):
            for lo, hi in MM.SEGMENTS[action]:
                trg = [r for r in tr if r[2] == action and lo <= r[5] <= hi]
                teg = [r for r in te if r[2] == action and lo <= r[5] <= hi]
                sid, y, X, lck = V6.mats(trg, action)
                s2, y2, X2, _ = V6.mats(teg, action)
                meta = {r[0]: r for r in teg}
                for vname, (w, only) in variants.items():
                    gbm = train_lgb(sid, y, X, lck, w, only)
                    sc = gbm.predict(np.hstack([X2, np.ones((len(X2), 1))]))
                    for k, rk in MM.rank_list(s2, y2, sc):
                        res.append({"set": name, "model": vname, "action": action, "team": meta[k][7], "rank": rk})
        H.log.info("  %s 끝", name)
    R = pd.DataFrame(res)
    H.save_csv(R, OUT / "m4_v6b_ranks.csv")

    def t(g):
        return f"{(g['rank'] <= 1).mean():.1%} · {(g['rank'] <= 3).mean():.1%} · {(g['rank'] <= 5).mean():.1%} · {(1 / g['rank']).mean():.3f}"
    L = ["# LightGBM — LCK 만 vs 5리그 (LCK 리허설)", "", "- 같은 특징 · 같은 설정, 학습 자료만 다름. 값 = top1 · top3 · top5 · MRR (3세트 합)", "",
         "| 모델 | 픽 | 밴 | 강팀 픽 top5 | 세트별 픽 top5 (26.14 / 26.15 / RTM) |", "|---|---|---|---|---|"]
    for mname in variants:
        g = R[R["model"] == mname]
        gp = g[g["action"] == "pick"]
        per = " / ".join(f"{(gp[gp['set'] == s]['rank'] <= 5).mean():.1%}" for s in sets)
        L.append(f"| {mname} | {t(gp)} | {t(g[g['action'] == 'ban'])} | {(gp[gp['team'].isin(V6.STRONG)]['rank'] <= 5).mean():.1%} | {per} |")
    (OUT / "m4_v6b_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
