"""I. 팀원 메타 점수 → 우리 이름 규칙으로 바꾼 가공본 (docs/PIPELINE.md 7-7 "팀원 메타 점수", 2026-11-06)

원본: data/raw/teammate_meta/ (팀원 handoff 2026-11-06, README.md·handoff_check.md 그대로 보관, 수정하지 않음)
출력: data/processed/2026/i_meta_score_daily.csv, i_champ_meta_daily.csv

이름 바꾸기
- 팀원 A·B·C (팀원 문서의 "M5·M5c" — 우리 M5 와 다름) → meta_pre · meta_lck · meta_score
- split train/test → period research/validation (우리 기간 이름: 08-25 까지 리서치, 08-26~ 검증)
- source pro_LCK … soloq_KR → LCK · LCKC · LPL · LEC · LCS · LCP · CBLOL · SOLOQ_KR (OE league 칸과 같은 표기)
- 포지션 빈칸(그날 챔피언 합계, 밴은 여기에만) → "ALL"
- champion → champion_name (champion_id 는 Data Dragon id 그대로, 우리 d_champions 와 같음)
- oe_patch(16.xx) 추가 — 경기 기록(OE·Leaguepedia) 쪽과 붙일 때

    python src/pipeline/i_teammate_meta.py
"""
from __future__ import annotations

import pandas as pd

from common import PROCESSED, RAW, get_logger, save_csv

log = get_logger("I")
SRC = RAW / "teammate_meta"

SCORE_RENAME = {
    "champion": "champion_name", "A": "meta_pre", "B": "meta_lck", "B_pick": "meta_lck_pick", "B_ban": "meta_lck_ban",
    "C": "meta_score", "k": "meta_k", "main_position": "pro_main_position", "n_positions_25": "pro_n_positions_25",
    "split": "period",
}
SOURCE = {"pro_LCK": "LCK", "pro_LCKC": "LCKC", "pro_LPL": "LPL", "pro_LEC": "LEC", "pro_LCS": "LCS",
          "pro_LCP": "LCP", "pro_CBLOL": "CBLOL", "soloq_KR": "SOLOQ_KR"}


def oe_patch(p: pd.Series) -> pd.Series:
    a = p.str.split(".")
    return (a.str[0].astype(int) - 10).astype(str) + "." + a.str[1]


def main():
    ids = set(pd.read_csv(PROCESSED / "d_champions.csv")["champion_id"])
    s = pd.read_csv(SRC / "meta_score_daily.csv", dtype={"patch": str, "asof_date": str, "valid_for": str})
    s = s.rename(columns=SCORE_RENAME)
    s["period"] = s["period"].map({"train": "research", "test": "validation"})
    s.insert(s.columns.get_loc("patch") + 1, "oe_patch", oe_patch(s["patch"]))
    miss = sorted(set(s["champion_id"]) - ids)
    save_csv(s, PROCESSED / "i_meta_score_daily.csv")

    c = pd.read_csv(SRC / "champ_meta_daily.csv", dtype={"patch": str, "date": str})
    c = c.rename(columns={"champion": "champion_name"})
    c["source"] = c["source"].map(SOURCE).fillna(c["source"])
    c["position"] = c["position"].fillna("ALL")
    c = c.drop(columns=[x for x in ("mentions", "pos_mentions", "neg_mentions", "n_docs") if c[x].isna().all()])
    c.insert(c.columns.get_loc("patch") + 1, "oe_patch", oe_patch(c["patch"]))
    save_csv(c, PROCESSED / "i_champ_meta_daily.csv")
    log.info("i_meta_score_daily %d행 (경기일 %d, 챔피언 %d) · i_champ_meta_daily %d행 · 우리 d_champions 에 없는 id %s",
             len(s), s["valid_for"].nunique(), s["champion_id"].nunique(), len(c), miss or "없음")


if __name__ == "__main__":
    main()
