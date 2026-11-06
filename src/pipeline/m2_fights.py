"""M2 교전 자료 (2026-11-06) — 솔랭 킬 기록을 교전(15초 묶음)으로 묶어 교전마다 양 팀 킬 수.

입력: data/raw/riot/timeline_killdmg/{matchId}.json.gz  ({"kills": [[초, 킬러 id, 희생자 id, 피해 기록], ...]})
      참가자 id 1~5 = 블루(100), 6~10 = 레드(200). 킬러 0 = 포탑·미니언 처형 → 희생자 상대 팀 킬로 셈
      교전 시점 골드 = 타임라인 10·15·20·25분 팀 골드 차이를 직선으로 이음 (0분 = 0, 25분 뒤 = 25분 값)
출력: m2/m2_fights.pkl  교전 1행: matchId, t (시작 초), blue_k, red_k, n_kills, gold_diff (블루 − 레드, 천 단위)
"""
from __future__ import annotations

import glob
import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402

KDMG_DIR = H.RAW / "riot" / "timeline_killdmg"
CACHE = H.PROCESSED / "m2" / "m2_fights.pkl"
FIGHT_GAP = 15


def team_gold() -> pd.DataFrame:
    import m0_attributes as M0
    fr, _, _ = M0.timeline_rows()
    t = fr.groupby(["matchId", "minute", "teamId"])["gold"].sum().unstack("teamId")
    return ((t[100] - t[200]) / 1000).unstack("minute")


def fights() -> pd.DataFrame:
    if CACHE.exists():
        return pd.read_pickle(CACHE)
    gold = team_gold()
    mins = np.array([0] + list(gold.columns), dtype=float) * 60
    rows = []
    files = sorted(glob.glob(str(KDMG_DIR / "*.json.gz")))
    for i, f in enumerate(files, 1):
        mid = Path(f).name[:-8]
        try:
            kills = sorted(json.loads(gzip.decompress(Path(f).read_bytes()))["kills"], key=lambda x: x[0])
        except Exception:
            continue
        g = gold.loc[mid].to_numpy(float) if mid in gold.index else None
        cur, last = [], None

        def flush(c):
            if not c:
                return
            bk = rk = 0
            for _, killer, victim, _h in c:
                blue_victim = 1 <= victim <= 5
                if blue_victim:
                    rk += 1
                else:
                    bk += 1
            t0 = c[0][0]
            gd = np.nan
            if g is not None:
                ok = ~np.isnan(np.r_[0.0, g])  # 일찍 끝난 판은 뒤 분 값이 없음 → 있는 분만으로 잇기
                gd = np.interp(t0, mins[ok], np.r_[0.0, g][ok])
            rows.append((mid, t0, bk, rk, len(c), gd))

        for kl in kills:
            if last is not None and kl[0] - last > FIGHT_GAP:
                flush(cur)
                cur = []
            cur.append(kl)
            last = kl[0]
        flush(cur)
        if i % 5000 == 0:
            H.log.info("교전 묶기 %d/%d", i, len(files))
    d = pd.DataFrame(rows, columns=["matchId", "t", "blue_k", "red_k", "n_kills", "gold_diff"])
    pd.to_pickle(d, CACHE)
    return d


if __name__ == "__main__":
    d = fights()
    print(len(d), d["matchId"].nunique(), (d["blue_k"] != d["red_k"]).mean())
    print(d.describe().round(2))
