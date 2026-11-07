"""M4 최종 모델 저장 (2026-11-07) — 픽 = 공통 + 이길 가치, 밴 = 1·2페이즈 + 이길 가치 + C2 + P1(같은 패치 5리그)

m4_v4.py 가 만든 특징 표 캐시(m4/m4_v4_rows.pkl)로 08-26 이전 LCK 전부를 학습해 m4/m4_model_final.json 저장.
리허설 성적 (m4_v4_eval.md): 픽 top5 62.6% (P1 없음), 밴 top5 67.7% (P1 5리그)

    python src/pipeline/m4_final.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v1 as V  # noqa: E402
import m4_v3 as V3  # noqa: E402
import m4_v4 as V4  # noqa: E402

OUT = B.OUT
SPEC = {"pick": {"extra": [], "p1": False}, "ban": {"extra": ["c2_hab"], "p1": True}}


def main():
    rows, a, gday, all_games = pd.read_pickle(OUT / "m4_v4_rows.pkl")
    model = {}
    for action, sp in SPEC.items():
        feats = MM.PICK_FEATS if action == "pick" else MM.BAN_FEATS
        names = feats + sp["extra"] + (V4.P1 if sp["p1"] else [])
        segs = []
        for lo, hi in MM.SEGMENTS[action]:
            rr = [r for r in rows if r[2] == action and lo <= r[5] <= hi]
            ix = [V.ALL.index(k) for k in feats]
            exi = [V3.EX_NAMES.index(k) for k in sp["extra"]]
            sid = np.concatenate([np.full(len(r[3]), r[0]) for r in rr])
            y = np.concatenate([r[3] for r in rr]).astype(float)
            X = np.vstack([np.hstack([r[4][:, ix], r[9][:, exi]] + ([r[10]] if sp["p1"] else [])) for r in rr]).astype(float)
            mu, sd = X.mean(0), X.std(0) + 1e-9
            b = MM.fit_off(sid, y, (X - mu) / sd, np.zeros(len(y)), 1.0)
            segs.append({"steps": [lo, hi], "features": names, "mu": mu.tolist(), "sd": sd.tolist(), "coef": b.tolist(),
                         "train_steps": int(len(rr))})
            H.log.info("%s %d-%d: 단계 %d, 칸 %d", action, lo, hi, len(rr), len(names))
        model[action] = segs
    model["note"] = ("M4 최종 (2026-11-07): 픽 = 공통 + 이길 가치, 밴 = 1·2페이즈 + 이길 가치 + C2 + P1(같은 패치 5리그). "
                     "학습 = 08-26 이전 LCK. 다른 리그 자료는 08-25 까지. 특징 정의는 m4_features.py · m4_v1.py · m4_v2.py(C2) · m4_v4.py(P1)")
    (OUT / "m4_model_final.json").write_text(json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
