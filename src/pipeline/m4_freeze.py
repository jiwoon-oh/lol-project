"""M4 최종 모델 고정 (2026-11-07) — 리허설에서 평가한 방식 그대로 08-25 까지 전부로 다시 학습해 저장

  픽 = LightGBM lambdarank, 5리그(LCK·LCKC·LPL·LEC·LCS) 학습, LCK 가중 3, 300회 고정, 특징 = 공통(이길 가치 없음) + LCK 표시
  밴 = 구간(1~6 · 13~16)마다 앙상블 = 선형(LCK 만) + LightGBM(5리그, LCK 가중 3, 학습 기간 마지막 15% 로 조기 종료)
       특징 = 공통(이길 가치 없음) + C2 + P1(같은 패치 5리그) + 시리즈·상대 전적 (정보 범위 A)
       앙상블 점수 = 단계 안 표준화(선형 점수) + 단계 안 표준화(LightGBM 점수)
리허설 (LCK 26.14 · 26.15 · RTM 합): 픽 top5 66.5% · 밴 top5 69.8% (선택 편향으로 1~2%p 낙관적일 수 있음)
저장: m4/final/ — pick_lgbm.txt, ban_lgbm_{구간}.txt, model_spec.json (특징 목록·선형 계수·표준화 값·설정·자료 범위·코드 상태)
특징 표 캐시: m4/m4_v6_rows.pkl (08-25 까지 5리그)

    python src/pipeline/m4_freeze.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402
import m4_baseline as B  # noqa: E402
import m4_model as MM  # noqa: E402
import m4_v4 as V4  # noqa: E402
import m4_v6b as V6B  # noqa: E402
import m4_v7 as V7  # noqa: E402

OUT = B.OUT / "final"
OUT.mkdir(exist_ok=True)


def main():
    import lightgbm as lgb
    rows, a, fear, ds_days = pd.read_pickle(B.OUT / "m4_v6_rows.pkl")
    if "is_winner" not in a.columns:
        a["is_winner"] = np.nan
    rows = V7.series_features(rows, a, V7.winners(a))
    gday = a.drop_duplicates("GameId").set_index("GameId")["day"]
    assert max(gday) <= "2026-08-25", "학습 자료에 08-26 이후 경기가 있음"
    spec = {"created": datetime.now().isoformat(timespec="seconds"),
            "data": {"leagues": ["LCK", "LCKC", "LPL", "LEC", "LCS"], "train_until": "2026-08-25",
                     "lck_games": int(a.loc[a["league"] == "LCK", "GameId"].nunique()), "all_games": int(a["GameId"].nunique()),
                     "info_scope": "경기 전날까지 + 같은 시리즈 앞 세트 밴·픽·승패 (정보 범위 A). 다른 리그 대회 기록은 08-25 까지"},
            "rehearsal": {"pick_top5": 0.665, "ban_top5": 0.698, "baseline_pick_top5": 0.456, "baseline_ban_top5": 0.501}}
    # 픽
    sid, y, X, lck = V7.mats([r for r in rows if r[2] == "pick"], "pick", False)
    order = np.argsort(sid, kind="stable")
    _, cnt = np.unique(sid[order], return_counts=True)
    d = lgb.Dataset(np.hstack([X, lck[:, None]])[order], label=y[order], group=cnt, weight=np.where(lck[order] == 1, 3.0, 1.0))
    gp = lgb.train(V6B.PARAMS, d, num_boost_round=300)
    gp.save_model(str(OUT / "pick_lgbm.txt"))
    spec["pick"] = {"model": "LightGBM lambdarank", "file": "pick_lgbm.txt", "rounds": 300, "lck_weight": 3.0, "params": V6B.PARAMS,
                    "features": V7.PICK_FEATS + ["is_lck"], "train_steps": int(len(cnt))}
    # 밴
    spec["ban"] = {"model": "앙상블 = z(선형 LCK 만) + z(LightGBM 5리그)", "segments": []}
    for lo, hi in MM.SEGMENTS["ban"]:
        rr = [r for r in rows if r[2] == "ban" and lo <= r[5] <= hi]
        sid, y, X, lck = V7.mats(rr, "ban", True)
        mu, sd = X.mean(0), X.std(0) + 1e-9
        m = lck == 1
        b = MM.fit_off(sid[m], y[m], (X[m] - mu) / sd, np.zeros(m.sum()), 1.0)
        gb, best = V7.lgb_es(rr, gday, "ban", True)
        f = OUT / f"ban_lgbm_{lo}-{hi}.txt"
        gb.save_model(str(f))
        names = V7.BAN_FEATS + ["c2_hab"] + V4.P1 + V7.SER
        spec["ban"]["segments"].append({"steps": [lo, hi], "features": names, "linear": {"mu": mu.tolist(), "sd": sd.tolist(), "coef": b.tolist()},
                                        "lgbm_file": f.name, "lgbm_rounds": int(best), "lgbm_features": names + ["is_lck"],
                                        "train_steps": int(len(np.unique(sid)))})
        H.log.info("밴 %d-%d: 단계 %d, LightGBM %d회", lo, hi, len(np.unique(sid)), best)
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=H.ROOT).stdout.strip()
        dirty = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, cwd=H.ROOT).stdout.strip().splitlines()
    except Exception:
        head, dirty = "?", []
    spec["code"] = {"git_head": head, "uncommitted": dirty,
                    "feature_code": ["m4_features.py (0차 특징 · DayStats)", "m4_v1.py (반응형 · 행 만들기)", "m4_v6.py (5리그 드래프트 · C2)",
                                     "m4_v4.py (P1)", "m4_v7.py (시리즈 · 상대 전적)", "m4_baseline.py (자료 불러오기)"]}
    (OUT / "model_spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    H.log.info("고정 끝 → %s", OUT)


if __name__ == "__main__":
    main()
