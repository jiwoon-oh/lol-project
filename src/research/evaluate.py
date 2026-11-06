"""S8. 평가 — 코드만 (토큰 0).

CLAUDE.md 5장 S8 지표를 대상별·전체로 계산한다. 출력에는 원문 인용을 넣지 않음 (집계만 → 공개 가능).
  인용 통과율        S5 passed / S4 전체                       research/verified/{대상}.jsonl
  주장 정확도        S7 supports=y / (y+n)                     research/reviews/{대상}.csv
  귀속 정확도        S7 target_ok=y / (y+n)                    research/reviews/{대상}.csv
  스타일 카드 일치도  초안 7장 대조표의 "일치" 칸                research/profiles/{대상}_draft.md
                     일치 / (일치+부분+어긋남), "판단·대조 불가"는 뺌. 한 칸에 "일치(A) / 어긋남(B)"면 둘로 셈
  출처 간 일치도      같은 aspect(공백 제거 후 같은 글자)에 방향 있는 주장(긍정·부정)이 있는 등급이 2개 이상일 때,
                     등급마다 (긍정−부정) 부호가 모두 같은 비율. 동률 등급은 뺌.
                     aspect 가 자유 서술이라 S6 의 측면 묶음보다 좁음 → 참고 지표
  비용·시간          usage.csv 에서 이번 run 의 대상별 S4·S6 토큰·분

S7 검토 기록 (research/reviews/{대상}.csv) — template 가 빈 칸으로 만들어 줌, 사람이 채움
  n, claim_id, status, grade, stance, aspect, claim, priority, supports, target_ok, aspect_ok, decision, memo
  supports·target_ok·aspect_ok = y / n / 빈칸(안 봄),  decision = keep / drop / edit
  priority = 검토 우선 사유 (needs_review · sarcastic · mixed · single_source · 초안 인용). 우선 항목이 위로

S7 표본 검토 (research/reviews/sample_review.csv) — 여러 대상을 한 파일로, 인용·앞뒤 문맥·링크까지 넣어 줌
  sample 로 만들고 → 사람이 엑셀에서 supports·target_ok·aspect_ok·decision·memo 를 채우고 → merge 로 대상별 파일에 반영
  원문이 들어 있으므로 공개 금지

출력  research/eval/per_target.csv, research/eval/summary.md

사용
  python evaluate.py                       전체 평가
  python evaluate.py template --player Oner   S7 검토 기록 틀 (이미 있으면 덮어쓰지 않음)
  python evaluate.py sample --players Doran,Oner,Faker,T1 [--n 40]   S7 표본 검토 파일
  python evaluate.py merge                 표본 검토 결과 → reviews/{대상}.csv
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict

from build_profile import COMMUNITY_GRADE, PROFILES, claims_used
from common import LOGS, RESEARCH, get_logger, load_config, read_jsonl, slug, targets, write_csv
from extract_claims import sentence_spans
from verify_quotes import load_sources, locate, normalize

log = get_logger("S8")
EVAL = RESEARCH / "eval"
REVIEWS = RESEARCH / "reviews"
QUALITY = {"quote_pass": 0.70, "claim_acc": 0.80, "attr_acc": 0.85}  # CLAUDE.md 5장 품질 기준
# 7장 "일치" 칸은 에이전트가 자유롭게 씀 ("부분 일치", "인벤과 불일치", "확인 불가" ...) → 앞에서부터 먼저 걸리는 쪽
CARD_RULES = (("unknown", re.compile(r"불가|보류|불명")), ("mismatch", re.compile(r"불일치|어긋|엇갈|일치 안")),
              ("partial", re.compile(r"부분|일부|대체로|대부분")), ("match", re.compile(r"일치")))
REVIEW_COLS = ["supports", "target_ok", "aspect_ok", "decision", "memo"]  # 사람이 채우는 칸
REVIEW_FIELDS = ["n", "claim_id", "status", "grade", "stance", "aspect", "claim", "priority", *REVIEW_COLS]
SAMPLE_FILE = "sample_review.csv"  # 원문 인용·문맥이 들어 있음 → 공개 금지
SAMPLE_FIELDS = ["target", "n", "claim_id", "grade", "stance", "date", "aspect", "claim", "quote", "context",
                 "source_url", "priority", *REVIEW_COLS]
CTX_CHARS = 200  # 문맥 칸: 인용 앞뒤 최대 글자 수


def ratio(a: int, b: int):
    return round(a / b, 3) if b else None


# ---------- 지표 ----------

def quote_pass(pid: str) -> dict:
    rows = list(read_jsonl(RESEARCH / "verified" / f"{slug(pid)}.jsonl"))
    st = Counter(r["status"] for r in rows)
    return {"claims": len(rows), "passed": st["passed"], "needs_review": st["needs_review"], "failed": st["failed"],
            "quote_pass": ratio(st["passed"], len(rows))}


def review_acc(pid: str) -> dict:
    path = REVIEWS / f"{slug(pid)}.csv"
    if not path.exists():
        return {"reviewed": 0, "claim_acc": None, "attr_acc": None}
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    yn = lambda col: Counter((r.get(col) or "").strip().lower() for r in rows)
    sup, tgt = yn("supports"), yn("target_ok")
    return {"reviewed": sum(1 for r in rows if (r.get("supports") or r.get("target_ok") or "").strip()),
            "claim_acc": ratio(sup["y"], sup["y"] + sup["n"]), "attr_acc": ratio(tgt["y"], tgt["y"] + tgt["n"])}


def card_agreement(pid: str) -> dict:
    """초안 7장 표의 마지막 칸("일치")을 셈."""
    path = PROFILES / f"{slug(pid)}_draft.md"
    cnt = Counter()
    if path.exists():
        sec = re.search(r"^## 7\..*?(?=^## 8\.|\Z)", path.read_text(encoding="utf-8"), re.S | re.M)
        for line in (sec.group(0).splitlines() if sec else []):
            cells = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(cells) < 3 or set(cells[-1]) <= set("-: ") or cells[0] == "항목":
                continue
            for part in cells[-1].split("/"):
                if part.strip() and part.strip() != "-":
                    cnt[next((k for k, p in CARD_RULES if p.search(part)), "other")] += 1
    judged = cnt["match"] + cnt["partial"] + cnt["mismatch"]
    return {"card_rows": sum(cnt.values()), "card_match": cnt["match"], "card_partial": cnt["partial"],
            "card_mismatch": cnt["mismatch"], "card_unknown": cnt["unknown"] + cnt["other"],
            "card_agree": ratio(cnt["match"], judged)}


def source_agreement(rows: list[dict]) -> dict:
    by = defaultdict(lambda: defaultdict(int))
    for c in rows:
        if c["stance"] in ("positive", "negative"):
            by[re.sub(r"\s+", "", c["aspect"])][c["grade"]] += 1 if c["stance"] == "positive" else -1
    n = agree = 0
    for grades in by.values():
        signs = {v > 0 for v in grades.values() if v}
        if sum(1 for v in grades.values() if v) >= 2:
            n += 1
            agree += len(signs) == 1
    return {"src_aspects": n, "src_agree": ratio(agree, n)}


def usage_by_target() -> dict:
    run = load_config("budget")["run"]["id"]
    out = defaultdict(lambda: {"tokens_s4": 0, "tokens_s6": 0, "seconds": 0.0, "agents": 0})
    with (LOGS / "usage.csv").open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["run"] != run or r["stage"] not in ("S4", "S6"):
                continue
            u = out[slug(r["player"])]  # 팀은 이름(Gen.G)과 slug(Nongshim_RedForce)가 섞여 기록돼 있음
            u["tokens_" + r["stage"].lower()] += int(float(r["total_tokens"] or 0))
            u["seconds"] += float(r["seconds"] or 0)
            u["agents"] += 1
    return out


# ---------- 실행 ----------

def evaluate() -> list[dict]:
    usage = usage_by_target()
    out = []
    for pid, cfg in targets().items():
        if not (RESEARCH / "verified" / f"{slug(pid)}.jsonl").exists():
            log.info("%s: S5 결과 없음 → 건너뜀", pid)
            continue
        u = usage.get(slug(pid), {"tokens_s4": 0, "tokens_s6": 0, "seconds": 0.0, "agents": 0})
        out.append({"target": pid, "kind": cfg["kind"], **quote_pass(pid), **review_acc(pid), **card_agreement(pid),
                    **source_agreement(claims_used(pid)), "tokens": u["tokens_s4"] + u["tokens_s6"],
                    "tokens_s4": u["tokens_s4"], "tokens_s6": u["tokens_s6"], "minutes": round(u["seconds"] / 60, 1),
                    "agents": u["agents"]})
    return out


def fmt(v) -> str:
    return "-" if v is None else f"{v:.1%}" if isinstance(v, float) else f"{v:,}"


def summary_md(rows: list[dict]) -> str:
    s = lambda k: sum(r[k] or 0 for r in rows)
    reviewed = [r for r in rows if r["reviewed"]]
    tot = {
        "quote_pass": ratio(s("passed"), s("claims")),
        "claim_acc": ratio(sum(round((r["claim_acc"] or 0) * r["reviewed"]) for r in reviewed), s("reviewed")) if reviewed else None,
        "attr_acc": ratio(sum(round((r["attr_acc"] or 0) * r["reviewed"]) for r in reviewed), s("reviewed")) if reviewed else None,
        "card_agree": ratio(s("card_match"), s("card_match") + s("card_partial") + s("card_mismatch")),
        "src_agree": ratio(sum(round((r["src_agree"] or 0) * r["src_aspects"]) for r in rows), s("src_aspects")),
    }
    ok = lambda k: "-" if tot[k] is None else ("✅" if tot[k] >= QUALITY[k] else "❌")
    low = sorted((r for r in rows if r["quote_pass"] is not None), key=lambda r: r["quote_pass"])[:5]
    L = [f"# S8 평가 요약 (자동 생성 — 손으로 고치지 말 것)", "",
         f"- 대상 {len(rows)}개 (선수 {sum(r['kind'] == 'player' for r in rows)} · 팀 {sum(r['kind'] == 'team' for r in rows)}) · "
         f"S7 검토 기록 있는 대상 {len(reviewed)}개", "",
         "| 지표 | 값 | 기준 | 판정 | 근거 수 |", "|---|---|---|---|---|",
         f"| 인용 통과율 | {fmt(tot['quote_pass'])} | 70% | {ok('quote_pass')} | 주장 {s('claims'):,} (검토필요 {s('needs_review'):,}, 탈락 {s('failed'):,}) |",
         f"| 주장 정확도 | {fmt(tot['claim_acc'])} | 80% | {ok('claim_acc')} | S7 검토 {s('reviewed'):,}건 |",
         f"| 귀속 정확도 | {fmt(tot['attr_acc'])} | 85% | {ok('attr_acc')} | S7 검토 {s('reviewed'):,}건 |",
         f"| 스타일 카드 일치도 | {fmt(tot['card_agree'])} | - | 참고 | 대조 {s('card_rows')}칸 (일치 {s('card_match')}, 부분 {s('card_partial')}, 어긋남 {s('card_mismatch')}, 판단 불가·기타 {s('card_unknown')}) |",
         f"| 출처 간 일치도 | {fmt(tot['src_agree'])} | - | 참고 | 등급 2개 이상인 aspect {s('src_aspects'):,}개 |",
         f"| 비용 | {s('tokens'):,} 토큰 | - | - | 대상당 평균 {s('tokens') // max(len(rows), 1):,} · 에이전트 {s('agents')}개 · {s('minutes'):.0f}분 |",
         "",
         "- 주장·귀속 정확도는 S7 검토 기록(`research/reviews/`)이 생기면 채워짐. 검토 틀: `python evaluate.py template --player <대상>`",
         "- 출처 간 일치도는 S4 자유 서술 aspect 글자가 같은 것끼리만 묶어서 셈 (S6 측면 묶음보다 좁음)",
         "- 비용 = 이번 run 의 S4·S6 기록 합 (v1 재사용분, 실패 재실행분 포함)", "",
         "## 인용 통과율 낮은 대상", "", "| 대상 | 통과율 | 주장 | 탈락 | 검토필요 |", "|---|---|---|---|---|",
         *[f"| {r['target']} | {fmt(r['quote_pass'])} | {r['claims']} | {r['failed']} | {r['needs_review']} |" for r in low], ""]
    return "\n".join(L)


def cmd_template(pid: str):
    path = REVIEWS / f"{slug(pid)}.csv"
    if path.exists():
        raise SystemExit(f"{path} 가 이미 있음 — 덮어쓰지 않음")
    rows = claims_used(pid)
    draft = PROFILES / f"{slug(pid)}_draft.md"
    cited = {int(n) for n in re.findall(r"\[#(\d+)", draft.read_text(encoding="utf-8"))} if draft.exists() else set()
    per_aspect = Counter(re.sub(r"\s+", "", c["aspect"]) for c in rows)
    out = []
    for c in rows:
        why = [w for w, hit in (("needs_review", c["status"] == "needs_review"),
                                ("sarcastic", c["stance"] == "sarcastic"), ("mixed", c["stance"] == "mixed"),
                                ("single_source", per_aspect[re.sub(r"\s+", "", c["aspect"])] == 1),
                                ("초안 인용", c["n"] in cited)) if hit]
        out.append({**{k: c[k] for k in ("n", "claim_id", "status", "grade", "stance", "aspect", "claim")},
                    "priority": " · ".join(why), "supports": "", "target_ok": "", "aspect_ok": "", "decision": "", "memo": ""})
    out.sort(key=lambda r: (-len([w for w in r["priority"].split(" · ") if w and w != "초안 인용"]),
                            "초안 인용" not in r["priority"], r["n"]))
    REVIEWS.mkdir(parents=True, exist_ok=True)
    write_csv(path, out, REVIEW_FIELDS)
    log.info("%s: 검토 틀 %d행 → %s", pid, len(out), path)


def context(src: dict, c: dict) -> str:
    """인용이 걸친 문장 ± 1문장, 인용 부분은 【 】로 표시."""
    s = src.get(c["source_id"])
    if not s:
        return ""
    text = normalize(s["text"])
    loc = locate(c["quote"], text)
    if loc is None:
        return ""
    st, en = loc[0], loc[1]
    spans = sentence_spans(text)
    idx = [i for i, (a, b) in enumerate(spans) if a < en and b > st] or [0]
    a, b = spans[max(0, idx[0] - 1)][0], spans[min(len(spans), idx[-1] + 2) - 1][1]
    a, b = max(a, st - CTX_CHARS), min(b, en + CTX_CHARS)
    return (text[a:st] + "【" + text[st:en] + "】" + text[en:b]).replace("\n", " ")


def cmd_sample(pids: list[str], n: int):
    """S7 표본 검토 파일 하나: 초안에 쓰인 위험 항목(검토필요·비꼼·혼합) → 초안에 쓰인 나머지 → 안 쓰인 위험 항목 순.
    같은 순위 안에서는 등급이 고르게 섞이도록 등급별로 번갈아 뽑음 (난수 없음, 다시 돌려도 같음)."""
    out = []
    for pid in pids:
        rows = claims_used(pid)
        by_n = {c["n"]: c for c in rows}
        cited = {int(x) for x in re.findall(r"\[#(\d+)", (PROFILES / f"{slug(pid)}_draft.md").read_text(encoding="utf-8"))}
        risky = lambda c: c["status"] == "needs_review" or c["stance"] in ("sarcastic", "mixed")
        rank = lambda c: (0 if c["n"] in cited and risky(c) else 1 if c["n"] in cited else 2 if risky(c) else 3)
        picked = []
        for r in range(4):
            pool = defaultdict(list)
            for c in rows:
                if rank(c) == r:
                    pool[c["grade"]].append(c)
            while pool and len(picked) < n:
                for g in list(pool):
                    picked.append(pool[g].pop(0))
                    if not pool[g]:
                        del pool[g]
                    if len(picked) >= n:
                        break
        src = load_sources(pid)
        for c in sorted(picked, key=lambda c: c["n"]):
            why = [w for w, hit in (("검토필요", c["status"] == "needs_review"), ("비꼼", c["stance"] == "sarcastic"),
                                    ("혼합", c["stance"] == "mixed"), ("초안 인용", c["n"] in cited)) if hit]
            out.append({"target": pid, "n": c["n"], "claim_id": c["claim_id"], "grade": c["grade"], "stance": c["stance"],
                        "date": c["date"], "aspect": c["aspect"], "claim": c["claim"], "quote": c["quote"].replace("\n", " "),
                        "context": context(src, by_n[c["n"]]), "source_url": c["source_url"], "priority": " · ".join(why),
                        **{k: "" for k in REVIEW_COLS}})
        log.info("%s: 표본 %d행 (초안 인용 %d)", pid, len(picked), sum(c["n"] in cited for c in picked))
    path = REVIEWS / SAMPLE_FILE
    if path.exists():
        raise SystemExit(f"{path} 가 이미 있음 — 덮어쓰지 않음 (검토 내용 보호). 지우고 다시 만들 것")
    REVIEWS.mkdir(parents=True, exist_ok=True)
    write_csv(path, out, SAMPLE_FIELDS)
    log.info("표본 %d행 → %s", len(out), path)


def cmd_merge():
    """표본 파일에 적은 검토 결과를 대상별 reviews/{대상}.csv 로 옮김 (claim_id 기준, 빈칸은 옮기지 않음)."""
    with (REVIEWS / SAMPLE_FILE).open(encoding="utf-8-sig", newline="") as f:
        done = [r for r in csv.DictReader(f) if any((r.get(k) or "").strip() for k in REVIEW_COLS)]
    for pid in dict.fromkeys(r["target"] for r in done):
        path = REVIEWS / f"{slug(pid)}.csv"
        if not path.exists():
            cmd_template(pid)
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        new = {r["claim_id"]: r for r in done if r["target"] == pid}
        hit = 0
        for r in rows:
            if r["claim_id"] in new:
                hit += 1
                for k in REVIEW_COLS:
                    v = (new[r["claim_id"]].get(k) or "").strip()
                    if v:
                        r[k] = v
        write_csv(path, rows, REVIEW_FIELDS)
        log.info("%s: 검토 %d행 반영 → %s", pid, hit, path)
    log.info("검토 적힌 행 %d개. 이제 python evaluate.py", len(done))


STANCES = "positive|negative|mixed|neutral_fact|sarcastic"
REF = r"\[#{n}†?\]†?|(?<!\[)#{n}†?(?!\d)"  # [#n] 과 서술 속 (#n, #m) 둘 다


def excluded(r: dict) -> str:
    """제외 사유 (빈 문자열이면 유지). 뒷받침 안 됨 / 대상 아님 / 사람이 drop."""
    why = [w for k, w in (("supports", "인용이 뒷받침 안 함"), ("target_ok", "대상 아님")) if r[k] == "n"]
    if r["decision"] == "drop":
        why.append("drop")
    return ", ".join(why)


def tidy(t: str) -> str:
    """번호를 지운 뒤 남은 빈 괄호·겹친 쉼표·공백 정리."""
    for a, b in ((r"\(\s*[,·]\s*", "("), (r"\s*[,·]\s*\)", ")"), (r"\s*\(\s*\)", ""), (r"([,·:])\s*[,·]", r"\1"),
                 (r"(\s)·", r"\1"), (r"  +", " "), (r" +([,.\]])", r"\1"), (r"†{2,}", "†")):
        t = re.sub(a, b, t)
    return t


def drop_refs(line: str, ns: set[str]) -> str:
    """줄에서 제외할 [#n] 을 지움. 표 칸의 '등급: 입장 [#a][#b] (k)' 조각은 개수를 줄이고, 번호가 다 빠지면 조각째 지움."""
    if not any(re.search(REF.format(n=n), line) for n in ns):
        return line
    pat = re.compile("|".join(REF.format(n=n) for n in ns))

    def part(p: str) -> str | None:
        had = len(re.findall(r"\[#\d+", p))
        p2, k = pat.subn("", p)
        if had and had == k:
            return None
        return tidy(re.sub(r"\((\d+)\)", lambda m: f"({max(int(m.group(1)) - k, had - k)})", p2, count=1)) if k else p2

    if line.startswith("|"):
        cells = line.split("|")
        for i, c in enumerate(cells):
            if not c.strip():
                continue
            parts = [x.strip() for x in (part(p) for p in c.split(" · ")) if x is not None]
            cells[i] = f" {' · '.join(parts)} " if parts else (" - " if c.strip() else c)
        return re.sub(r"  +", " ", "|".join(cells))
    return tidy(pat.sub("", line)).rstrip()


def cmd_finalize(pid: str, force: bool):
    """reviews/{대상}.csv 를 초안에 반영 → profiles/{대상}.md. 제외 번호는 본문에서 지우고 9장에 사유·입장 수정 메모를 모음.
    지운 번호가 있던 서술 문장은 뜻이 달라졌을 수 있으니 9장 '다시 볼 문장'을 보고 손으로 다듬음 (그래서 덮어쓰기 거부)."""
    out = PROFILES / f"{slug(pid)}.md"
    if out.exists() and not force:
        raise SystemExit(f"{out} 가 이미 있음 (손으로 다듬었을 수 있음). 다시 만들려면 --force")
    with (REVIEWS / f"{slug(pid)}.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if any((r.get(k) or "").strip() for k in REVIEW_COLS)]
    drop = {r["n"]: excluded(r) for r in rows if excluded(r)}
    draft = (PROFILES / f"{slug(pid)}_draft.md").read_text(encoding="utf-8").splitlines()
    body, touched = [], []
    for line in draft:
        new = drop_refs(line, set(drop))
        if new != line and not line.lstrip().startswith("- `†`"):  # 표 행도 포함 (칸의 서술이 지운 주장을 말할 수 있음)
            touched.append(new.strip().removeprefix("- "))
        body.append(new)
    if not rows:  # 대상별 검토 없이 확정 (표본 검토 품질로 갈음)
        body[0] = body[0].replace("(초안)", "(확정 · S7 미검토)")
        L = ["", "## 9. S7 검토 반영", "",
             "- 이 대상은 주장별 사람 검토를 하지 않음. 품질은 다른 대상의 표본 검토로 확인 (`research/eval/summary.md` 주장·귀속 정확도)",
             "- 표본 검토에서 나온 오류 경향: 귀속 오류는 대부분 커뮤니티 댓글(다른 선수 책임 전가·팬덤 얘기), 입장은 mixed 가 실제로는 negative 인 경우가 많고, 칭찬하는 반어("양아치" 등)를 비꼼으로 읽은 경우가 있음. "
             "커뮤니티 시각 칸의 '혼합'·'비꼼'은 이 점을 감안해 읽을 것", ""]
        out.write_text("\n".join(body + L), encoding="utf-8")
        log.info("%s: 검토 기록 없음 → 초안 그대로 확정 → %s", pid, out)
        return
    body[0] = body[0].replace("(초안)", "(S7 검토 반영)")
    fix = [(r, re.search(rf"({STANCES}\w*)\s*->\s*({STANCES})", r["memo"])) for r in rows if r["n"] not in drop]
    L = ["", "## 9. S7 검토 반영", "",
         f"- 검토 {len(rows)}행 (표본 검토) · 제외 {len(drop)}건 · 유지한 주장 메모 {sum(bool(r['memo']) for r, _ in fix)}건 "
         f"(그중 입장 수정 {sum(bool(m) for _, m in fix)}건). 제외한 번호는 본문에서 지웠고, 표의 (개수)도 그만큼 줄임",
         "- 본문의 입장 표기(긍정·부정·혼합, 폼 표의 +−±~ 개수)는 S4 원래 값. 사람이 고친 입장은 아래 표가 기준", "", "### 제외한 주장", "",
         "| # | 등급 | 주장 | 사유 | 메모 |", "|---|---|---|---|---|"]
    by_n = {r["n"]: r for r in rows}
    L += [f"| {n} | {by_n[n]['grade']} | {by_n[n]['claim']} | {why} | {by_n[n]['memo'] or '-'} |" for n, why in drop.items()]
    L += ["", "### 입장·내용 수정 메모 (유지한 주장)", "", "| # | 등급 | 원래 입장 | 수정 | 주장 | 메모 |", "|---|---|---|---|---|---|"]
    L += [f"| {r['n']} | {r['grade']} | {r['stance']} | {m.group(2) if m else '-'} | {r['claim']} | {r['memo']} |"
          for r, m in fix if r["memo"] or r["decision"] == "edit"]
    L += ["", "### 다시 볼 문장 (번호를 지운 서술)", "", *[f"- {t}" for t in touched or ["(없음)"]], ""]
    out.write_text("\n".join(body + L), encoding="utf-8")
    log.info("%s: 제외 %d건, 수정 메모 %d건, 다시 볼 문장 %d개 → %s", pid, len(drop),
             sum(bool(r["memo"]) for r, _ in fix), len(touched), out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("template").add_argument("--player", required=True)
    sp = sub.add_parser("sample")
    sp.add_argument("--players", required=True)
    sp.add_argument("--n", type=int, default=40)
    sub.add_parser("merge")
    fp = sub.add_parser("finalize")
    fp.add_argument("--players", help="쉼표로 구분. 생략하면 --all")
    fp.add_argument("--all", action="store_true", help="profiles/{대상}.md 가 아직 없는 대상 전부")
    fp.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "finalize":
        if a.all:
            todo = [p for p in targets() if not (PROFILES / f"{slug(p)}.md").exists()]
            log.info("확정할 대상 %d개 (이미 있는 것은 건너뜀)", len(todo))
            return [cmd_finalize(p, False) for p in todo]
        if not a.players:
            raise SystemExit("--players 또는 --all")
        return [cmd_finalize(p, a.force) for p in a.players.split(",")]
    if a.cmd == "template":
        return cmd_template(a.player)
    if a.cmd == "sample":
        return cmd_sample(a.players.split(","), a.n)
    if a.cmd == "merge":
        return cmd_merge()
    rows = evaluate()
    EVAL.mkdir(parents=True, exist_ok=True)
    write_csv(EVAL / "per_target.csv", rows, list(rows[0].keys()))
    (EVAL / "summary.md").write_text(summary_md(rows), encoding="utf-8")
    log.info("대상 %d개 → %s, %s", len(rows), EVAL / "per_target.csv", EVAL / "summary.md")


if __name__ == "__main__":
    main()
