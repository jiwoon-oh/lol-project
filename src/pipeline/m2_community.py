"""M2 조합 — 커뮤니티 동시 언급 (2026-11-06, docs/PIPELINE.md 7-7 "조합 찾기" [1] 보조)

"사람들이 이 두 챔피언을 조합으로 부르나"를 원문에서 셈. LLM 을 쓰지 않는 사전·규칙 기반.
  자료 (리서치 기간 08-25 까지, 로컬 원문만): 인벤 글 제목·본문, OP.GG 톡 제목·본문, 유튜브 댓글, 기사·인터뷰 본문(한국어·영어)
  사전: data d_champions 의 한글·영문 이름 + 띄어쓰기 없는 형태 + 흔한 별명 (research/config/champion_aliases.csv 로 저장, 사람이 고칠 수 있음)
  매칭: 이름 앞은 한글·영문이 아니어야 하고, 뒤는 조사(이·가·랑·와·과 …) 또는 한글이 아닌 글자 (예: "진짜"·"오른쪽"은 안 걸림)
  문장: 마침표·물음표·느낌표·줄바꿈으로 나눔
  셈: 두 챔피언이 한 문장에 → n_both, 거기에 조합 단어(조합·시너지·연계·콤보·궁합·듀오·케미·짝 …)도 → n_combo
      lift = (n_both + 1) / (기대 + 1), 기대 = 문장 수 × 각 챔피언 언급 비율 곱
출력: m2/combos/m2_combo_community.csv (챔피언 쌍, 포지션 없음), research/config/champion_aliases.csv

    python src/pipeline/m2_community.py
"""
from __future__ import annotations

import glob
import json
import re
import sys
import time
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import h_champion_attributes as H  # noqa: E402

PROCESSED, log = H.PROCESSED, H.log
OUT = PROCESSED / "m2" / "combos"
ALIAS_FILE = H.ROOT / "research" / "config" / "champion_aliases.csv"
DATA = H.ROOT / "data"
RAW_RES = H.ROOT / "research" / "raw"
END = "2026-08-25"

# 흔한 별명 (champion_id → 별명). 너무 짧거나 일반어와 겹치는 것은 넣지 않음 (예: 카사 = 카이사·카사딘 혼동)
NICK = {
    "LeeSin": ["리신", "리싄"], "JarvanIV": ["자르반", "잘반"], "TwistedFate": ["트페"], "Seraphine": ["세라"],
    "Blitzcrank": ["블츠", "블리츠"], "MissFortune": ["미포"], "Ezreal": ["이즈"], "Caitlyn": ["케틀"], "Orianna": ["오리"],
    "Renekton": ["레넥"], "AurelionSol": ["아솔"], "TahmKench": ["탐켄치", "켄치"], "DrMundo": ["문도"], "MasterYi": ["마이"],
    "XinZhao": ["신짜오", "짜오"], "Renata": ["레나타"], "Nautilus": ["노틸"], "Alistar": ["알리"], "Khazix": ["카직"],
    "Graves": ["그브"], "Hecarim": ["헤카"], "Fiddlesticks": ["피들"], "Malphite": ["말파"], "Maokai": ["마오"],
    "KSante": ["크산"], "Aphelios": ["아펠"], "Volibear": ["볼베"], "Viktor": ["빅토"], "Katarina": ["카타"],
    "Leblanc": ["르블"], "Irelia": ["이렐"], "Ambessa": ["암베"], "Taliyah": ["탈리"], "Nidalee": ["니달"],
    "Kindred": ["킨드"], "Vladimir": ["블라디"], "Heimerdinger": ["하이머"], "Kalista": ["칼리"], "Tristana": ["트타"],
    "Lissandra": ["리산"], "Yasuo": ["야스"], "Sylas": ["사일"], "Kassadin": ["카사딘"], "Cassiopeia": ["카시"],
    "Gragas": ["그라가스"], "MonkeyKing": ["오공"], "Nocturne": ["녹턴"], "Pantheon": ["판테"], "Mordekaiser": ["모데"],
    "Gangplank": ["갱플"], "Rumble": ["럼블"], "Gnar": ["나르"], "Camille": ["카밀"], "Galio": ["갈리"],
}
COMBO_WORDS = ["조합", "시너지", "연계", "콤보", "궁합", "듀오", "케미", "짝꿍", "호흡", "콤비",
               "synergy", "combo", "duo", "pairing", "comp"]
PARTICLE = r"(?:이랑|이나|하고|에게|한테|으로|이|가|은|는|을|를|와|과|랑|도|만|의|로|에|님|vs|[^가-힣]|$)"
SPLIT = re.compile(r"[.!?。\n]+|(?<=[다요죠음])\s")


def aliases() -> pd.DataFrame:
    if ALIAS_FILE.exists():
        return pd.read_csv(ALIAS_FILE, dtype=str, encoding="utf-8-sig")
    c = pd.read_csv(PROCESSED / "d_champions.csv", dtype=str, encoding="utf-8-sig")
    rows = []
    for r in c.itertuples():
        names = {r.name_ko, r.name_ko.replace(" ", ""), r.name_en}
        names |= set(NICK.get(r.champion_id, []))
        for n in sorted(names):
            if n:  # 한 글자 이름(진·렐·퀸·쉔·멜)은 따로 엄격하게 (ko1)
                rows.append({"champion_id": r.champion_id, "alias": n,
                             "kind": "en" if n.isascii() else ("ko1" if len(n) == 1 else "ko")})
    a = pd.DataFrame(rows).drop_duplicates()
    # 둘 이상의 챔피언에 걸리는 별명은 뺌
    dup = a.groupby("alias")["champion_id"].nunique()
    a = a[a["alias"].map(dup) == 1]
    a.to_csv(ALIAS_FILE, index=False, encoding="utf-8-sig")
    return a


def build_matcher(a: pd.DataFrame):
    """긴 이름부터 (애니비아가 애니보다 먼저). 한글: 앞 경계 + 뒤 조사. 영문: 단어 경계, 대소문자 무시."""
    a = a.assign(L=a["alias"].str.len()).sort_values("L", ascending=False)
    ko = a[a["kind"] == "ko"]
    en = a[a["kind"] == "en"]
    k1 = a[a["kind"] == "ko1"]
    k1_re = re.compile(r"(?<![가-힣0-9])(" + "|".join(map(re.escape, k1["alias"])) + r")(?=(?:이랑|하고|이|가|은|는|을|를|와|과|랑|도|의|\+|/|,|&|$))")
    m_k1 = dict(zip(k1["alias"], k1["champion_id"]))
    ko_re = re.compile(r"(?<![가-힣A-Za-z0-9])(" + "|".join(map(re.escape, ko["alias"])) + r")(?=" + PARTICLE + ")")
    en_re = re.compile(r"\b(" + "|".join(map(re.escape, en["alias"])) + r")\b", re.IGNORECASE)
    m_ko = dict(zip(ko["alias"], ko["champion_id"]))
    m_en = {k.lower(): v for k, v in zip(en["alias"], en["champion_id"])}

    def find(s: str) -> set:
        out = {m_ko[x] for x in ko_re.findall(s)} | {m_k1[x] for x in k1_re.findall(s)}
        out |= {m_en[x.lower()] for x in en_re.findall(s)}
        return out
    return find


def texts():
    """(출처, 날짜, 글) 생성. 리서치 기간만."""
    start = H.research_windows()["window_from"].min()
    ok = lambda d: isinstance(d, str) and start <= d[:10] <= END  # noqa: E731
    seen = set()
    for f in glob.glob(str(DATA / "inven_2026" / "search" / "*.csv")):
        for r in pd.read_csv(f, dtype=str).itertuples():
            if r.post_id not in seen and ok(r.date):
                seen.add(r.post_id)
                yield "inven", r.date, r.title or ""
    with open(DATA / "inven_2026" / "bodies.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if ok(d.get("date")):
                yield "inven", d["date"], d.get("body") or ""
    p = RAW_RES / "opgg" / "posts.jsonl"
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                d = json.loads(line)
                if ok(d.get("date")):
                    yield "opgg", d["date"], f"{d.get('title') or ''}\n{d.get('body') or ''}"
    for f in glob.glob(str(DATA / "youtube_2026" / "comments" / "*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                d = json.loads(line)
                if ok(d.get("published")):
                    yield "youtube", d["published"][:10], d.get("text") or ""
    for f in glob.glob(str(RAW_RES / "articles" / "*" / "*.json")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        if ok(d.get("published")):
            yield "article", d["published"][:10], f"{d.get('title') or ''}\n{d.get('text') or ''}"


def main():
    t0 = time.time()
    a = aliases()
    find = build_matcher(a)
    cw = re.compile("|".join(map(re.escape, COMBO_WORDS)), re.IGNORECASE)
    n_sent, ment, both, combo, src = 0, {}, {}, {}, {}
    seen_sent = set()
    for source, day, text in texts():
        for s in SPLIT.split(text):
            s = s.strip()
            if len(s) < 4:
                continue
            h = hash(s)
            if h in seen_sent:  # 같은 문장 반복(복사 댓글·여러 대상 기사)은 한 번만
                continue
            seen_sent.add(h)
            n_sent += 1
            cs = find(s)
            for c in cs:
                ment[c] = ment.get(c, 0) + 1
            if len(cs) < 2 or len(cs) > 4:  # 챔피언 나열(5명 이상)은 조합 언급으로 보기 어려움
                continue
            has_cw = bool(cw.search(s))
            for x, y in combinations(sorted(cs), 2):
                both[(x, y)] = both.get((x, y), 0) + 1
                if has_cw:
                    combo[(x, y)] = combo.get((x, y), 0) + 1
                    src.setdefault((x, y), set()).add(source)
    log.info("문장 %d, 챔피언 언급 문장 %d, 쌍 %d (%.0f초)", n_sent, sum(ment.values()), len(both), time.time() - t0)
    rows = []
    for (x, y), nb in both.items():
        e = n_sent * (ment[x] / n_sent) * (ment[y] / n_sent)
        rows.append({"a": x, "b": y, "n_both": nb, "n_combo": combo.get((x, y), 0), "exp_both": e,
                     "comm_lift": (nb + 1) / (e + 1), "sources": ",".join(sorted(src.get((x, y), [])))})
    d = pd.DataFrame(rows).sort_values("n_combo", ascending=False)
    H.save_csv(d, OUT / "m2_combo_community.csv")
    m = pd.Series(ment).rename("mentions").sort_values(ascending=False)
    H.save_csv(m.rename_axis("champion_id").reset_index(), OUT / "m2_combo_mentions.csv")
    log.info("커뮤니티 끝 (%.0f초)", time.time() - t0)


def examples(k: int = 3) -> None:
    """의미 있는 시너지 쌍(m2_combo_synergy.csv)마다 조합 단어가 같이 나온 원문 문장 예시 (출처·날짜).
    원문이라 개인 분석용 (data/ 아래, 공개 저장소에 안 올라감)."""
    syn = pd.read_csv(OUT / "m2_combo_synergy.csv")
    want = {}
    for r in syn.to_dict("records"):
        x, y = sorted(c.split("@")[0] for c in r["key"].split("+"))
        want.setdefault((x, y), f"{r['a']} + {r['b']}")
    find = build_matcher(aliases())
    cw = re.compile("|".join(map(re.escape, COMBO_WORDS)), re.IGNORECASE)
    got, seen = {}, set()
    for source, day, text in texts():
        for st in SPLIT.split(text):
            st = st.strip()
            if len(st) < 8 or st in seen or not cw.search(st):
                continue
            cs = find(st)
            if len(cs) < 2:
                continue
            for pr in want:
                if pr[0] in cs and pr[1] in cs and len(got.get(pr, [])) < k:
                    seen.add(st)
                    got.setdefault(pr, []).append((source, day, st[:160]))
    L = ["# 의미 있는 시너지 — 원문 예시 (개인 분석용, 공개 금지)", "",
         "- 조합 단어(조합·시너지·연계·콤보·궁합·듀오 …)와 두 챔피언이 한 문장에 나온 예. 쌍마다 최대 3개, 160자까지", ""]
    for pr, name in want.items():
        L += [f"## {name}", ""]
        ex = got.get(pr, [])
        L += [f"- [{s_} {d}] {t}" for s_, d, t in ex] if ex else ["- (조합 단어와 같이 나온 문장 없음)"]
        L += [""]
    (OUT / "m2_combo_evidence.md").write_text("\n".join(L), encoding="utf-8")
    log.info("예시 %d쌍 중 %d쌍", len(want), len(got))


if __name__ == "__main__":
    if "--examples" in sys.argv:
        examples()
    else:
        main()
