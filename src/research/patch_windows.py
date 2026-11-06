"""대회 패치 대응표 — 리서치 단위(패치)와 기간을 경기 기록에서 만든다 (코드만, 토큰 0).

패치 표기는 26.xx 로 통일. OE 는 클라이언트 버전 16.xx 를 쓰므로 변환해서 둠 (oe_patch 칸).
CSV 를 pandas 로 읽을 때 패치를 숫자로 읽으면 26.10 → 26.1 로 깨짐 → 항상 문자열로 읽을 것 (read_patch_csv).

창(window): 그 패치로 LCK 경기를 한 첫날 ~ 다음 대회 패치 첫날 전날.
  단, 다음 패치까지 공백이 GAP_DAYS 보다 길면 (비시즌·MSI) 마지막 경기 + REACTION_DAYS 일까지만.
  → 26.04·05(LCK 미사용, 비시즌), 26.12·13(MSI) 기간의 글·댓글은 자연히 빠짐
역할(role): research = 프로필·여론 리서치용 / validation = 밴픽 모델 검증용 (리서치에 쓰지 않음)
  (research/config/period.yaml 의 research_last_patch, validation_playoff_patches)
  validation_playoff_patches 에 있는 패치는 정규시즌 경기(research)와 플레이오프 경기(validation)로 나눠 행 2개
  (예: 26.16 = 정규 08-19~08-23 → 리서치, 플레이오프 08-29~ → 검증, 2026-11-03 결정)

출력  research/config/tournament_patches.csv
사용  python patch_windows.py
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from common import CONFIG, DATA, get_logger, load_config, write_csv

log = get_logger("patch")
PROC = DATA / "processed" / "2026"
GAP_DAYS = 7
REACTION_DAYS = 3
OUT = CONFIG / "tournament_patches.csv"
PATCH_COLS = {"patch": str, "Patch": str, "riot_patch": str}


def read_patch_csv(path, **kw) -> pd.DataFrame:
    """패치 칸을 문자열로 읽음 (26.10 이 26.1 로 깨지지 않게)."""
    return pd.read_csv(path, dtype=PATCH_COLS, **kw)


def to_riot(oe_patch: str) -> str:
    """OE 클라이언트 버전 16.xx → 라이엇 패치 이름 26.xx."""
    major, minor = oe_patch.split(".")
    return f"{int(major) + 10}.{int(minor):02d}"


def build() -> list[dict]:
    cfg = load_config("period")
    tg = read_patch_csv(PROC / "a_team_games.csv")
    tg["d"] = (pd.to_datetime(tg["date"]) + pd.Timedelta(hours=9)).dt.date
    tg["per"] = tg["split"] + tg["playoffs"].map({0: "", 1: " PO"})
    split_po = set(cfg.get("validation_playoff_patches", []))
    tg["seg"] = ["PO" if to_riot(pt) in split_po and po == 1 else "" for pt, po in zip(tg["patch"], tg["playoffs"])]
    g = tg.groupby(["patch", "seg"]).agg(first=("d", "min"), last=("d", "max"), games=("gameid", "nunique"),
                                         periods=("per", lambda s: ", ".join(dict.fromkeys(s)))).sort_values("first")
    notes = read_patch_csv(PROC / "c_patches.csv").set_index("riot_patch")["published"]
    rows = []
    items = list(g.itertuples())
    for i, r in enumerate(items):
        nxt = items[i + 1].first if i + 1 < len(items) else None
        if nxt is not None and (nxt - r.last).days <= GAP_DAYS:
            to = nxt - timedelta(days=1)
        else:
            to = r.last + timedelta(days=REACTION_DAYS)
        oe, seg = r.Index
        p = to_riot(oe)
        role = ("validation" if seg == "PO" else "research" if p <= cfg["research_last_patch"] else "excluded")
        rows.append({"patch": p, "oe_patch": oe, "segment": "playoffs" if seg else "", "role": role, "window_from": r.first.isoformat(),
                     "window_to": to.isoformat(), "first_game": r.first.isoformat(), "last_game": r.last.isoformat(),
                     "games": r.games, "periods": r.periods, "notes_published": notes.get(p, "")})
    return rows


def windows(role: str = "research") -> list[tuple[str, str, str]]:
    """(patch, window_from, window_to) 목록. 다른 단계가 날짜 → 패치 배정에 씀."""
    df = pd.read_csv(OUT, dtype=str)
    df = df[df["role"] == role]
    return list(df[["patch", "window_from", "window_to"]].itertuples(index=False, name=None))


def patch_of(day: str, wins: list[tuple[str, str, str]]) -> str | None:
    """YYYY-MM-DD 가 속한 패치 창. 어느 창에도 없으면 None (비시즌·MSI·월즈 등 → 리서치 제외)."""
    for p, a, b in wins:
        if a <= day <= b:
            return p
    return None


def main():
    rows = build()
    write_csv(OUT, rows, list(rows[0].keys()))
    for r in rows:
        log.info("%s (%s) %-10s %s ~ %s  %3d경기  %s", r["patch"], r["oe_patch"], r["role"],
                 r["window_from"], r["window_to"], r["games"], r["periods"])
    cnt = {k: sum(r["role"] == k for r in rows) for k in ("research", "validation", "excluded")}
    log.info("리서치 %d패치, 검증 %d패치, 제외 %d패치 → %s", cnt["research"], cnt["validation"], cnt["excluded"], OUT)


if __name__ == "__main__":
    main()
