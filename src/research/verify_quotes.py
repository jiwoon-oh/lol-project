"""S5. 인용 자동 확인 — 코드만 (토큰 0).

S4 주장마다 CLAUDE.md 5장 S5 네 가지를 확인한다. 원문은 LLM에 넘긴 조각이 아니라 저장된 전체 원문.
  quote_not_found        공백·따옴표 정리 후 원문에 그대로 있나. 없으면 rapidfuzz 부분 유사도 ≥ 90 만 통과
  player_not_near_quote  인용이 걸친 문장 ± near_sentences 문장에 선수 별칭이 있나 (인벤은 제목도 첫 문장으로 봄)
  date_mismatch          주장 날짜 = 자료 발행·작성일, 그리고 리서치 패치 창 안인가 (26.16 검증 패치·비시즌·MSI 밖이면 탈락)
  source_missing         source_id 가 S2·S3 저장본에 있나
status: passed / needs_review(player_not_near_quote 하나만 걸림 → S7 우선 검토, 버리지 않음) / failed
한계: "인용이 주장을 뒷받침하는가"는 확인하지 못함 → S6·S7

입력  research/claims/{player}.jsonl, research/raw/articles/{player}/*.json, research/samples/{player}.jsonl
출력  research/verified/{player}.jsonl (주장 + passed + reasons + match), research/logs/s5_summary.csv

사용: python verify_quotes.py [--players Doran,Oner,Faker]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter

from rapidfuzz import fuzz

from common import (ARTICLES, LOGS, RESEARCH, SAMPLES, AliasMatcher, get_logger, nfc, players,
                    read_jsonl, write_csv, write_jsonl, slug, targets)
from extract_claims import sentence_spans
from patch_windows import patch_of, windows

log = get_logger("S5")
VERIFIED = RESEARCH / "verified"
FUZZ_MIN = 90
NEAR_SENTENCES = 1

_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "`": "'", " ": " "})
_ZW = re.compile(r"[​-‍﻿]")
_WS = re.compile(r"[ \t\r\f\v]+")


def normalize(s: str) -> str:
    """공백·따옴표 정리. 줄바꿈은 문장 경계 판단에 쓰므로 하나로만 줄임."""
    s = _ZW.sub("", nfc(s or "")).translate(_QUOTES)
    s = _WS.sub(" ", s)
    return re.sub(r"\s*\n\s*", "\n", s).strip()


def flat(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def load_sources(pid: str) -> dict[str, dict]:
    """source_id → {text, date}. 기사는 저장된 원문 전체, 커뮤니티는 S3 표본 원문."""
    src = {}
    for f in (ARTICLES / slug(pid)).glob("*.json"):
        a = json.loads(f.read_text(encoding="utf-8"))
        src[f"article:{a['id']}"] = {"text": a["text"], "date": a["published"]}
    for r in read_jsonl(SAMPLES / f"{slug(pid)}.jsonl"):
        src[r["id"]] = {"text": r["text"], "date": r["date"]}
    return src


def locate(quote: str, text: str) -> tuple[int, int, str, float] | None:
    """원문(normalize된)에서 인용 위치. (start, end, 방법, 점수) 또는 None."""
    q = normalize(quote)
    if not q:
        return None
    i = text.find(q)
    if i >= 0:
        return i, i + len(q), "exact", 100.0
    # 줄바꿈·공백 차이만 있는 경우: 공백을 모두 한 칸으로 본 문자열에서 찾고 위치를 되돌림
    ft, fq = flat(text), flat(q)
    j = ft.find(fq)
    if j >= 0:
        # flat()은 길이를 줄이지 않음(공백 1글자 → 공백 1글자, 연속 공백은 normalize에서 이미 1개)
        return j, j + len(fq), "exact_ws", 100.0
    al = fuzz.partial_ratio_alignment(fq, ft)
    if al and al.score >= FUZZ_MIN:
        return al.dest_start, al.dest_end, "fuzzy", round(al.score, 1)
    return None


def near_alias(text: str, start: int, end: int, matcher: AliasMatcher, k: int) -> bool:
    spans = sentence_spans(text)
    idx = [i for i, (a, b) in enumerate(spans) if a < end and b > start]
    if not idx:
        return matcher.found(text[start:end])
    lo, hi = max(0, idx[0] - k), min(len(spans), idx[-1] + k + 1)
    return matcher.found(text[spans[lo][0]:spans[hi - 1][1]])


def verify(pid: str) -> dict:
    matcher = AliasMatcher(targets()[pid])
    src = load_sources(pid)
    wins = windows("research")
    out, reasons = [], Counter()
    for c in read_jsonl(RESEARCH / "claims" / f"{slug(pid)}.jsonl"):
        why, match = [], None
        s = src.get(c["source_id"])
        if s is None:
            why.append("source_missing")
        else:
            text = normalize(s["text"])
            loc = locate(c["quote"], text)
            if loc is None:
                why.append("quote_not_found")
            else:
                st, en, method, score = loc
                match = {"method": method, "score": score}
                if not near_alias(text, st, en, matcher, NEAR_SENTENCES):
                    why.append("player_not_near_quote")
            if c["date"] != s["date"] or not patch_of(c["date"], wins):
                why.append("date_mismatch")
        reasons.update(why)
        # 이름 거리만 걸린 경우는 버리지 않고 S7 우선 검토로 (인터뷰 본인 발언은 이름에서 2~3문장 떨어지는 게 보통)
        status = "passed" if not why else ("needs_review" if why == ["player_not_near_quote"] else "failed")
        out.append({**c, "passed": not why, "status": status, "reasons": why, "match": match})
    VERIFIED.mkdir(parents=True, exist_ok=True)
    write_jsonl(VERIFIED / f"{slug(pid)}.jsonl", out)
    n_ok = sum(r["passed"] for r in out)
    by_type = Counter((r["source_type"], r["passed"]) for r in out)
    row = {"player": pid, "claims": len(out), "passed": n_ok,
           "pass_rate": round(n_ok / len(out), 3) if out else 0,
           "needs_review": sum(r["status"] == "needs_review" for r in out),
           "failed": sum(r["status"] == "failed" for r in out),
           "fuzzy_pass": sum(1 for r in out if r["passed"] and r["match"] and r["match"]["method"] == "fuzzy"),
           **{k: reasons.get(k, 0) for k in ("quote_not_found", "player_not_near_quote", "date_mismatch", "source_missing")},
           "by_source": " ".join(f"{t}:{by_type[(t, True)]}/{by_type[(t, True)] + by_type[(t, False)]}"
                                 for t in sorted({t for t, _ in by_type}))}
    log.info("%s", row)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", default=None)
    args = ap.parse_args()
    rows = [verify(p) for p in (args.players.split(",") if args.players else list(targets()))]
    write_csv(LOGS / "s5_summary.csv", rows, list(rows[0].keys()))


if __name__ == "__main__":
    main()
