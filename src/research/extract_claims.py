"""S4. 주장 뽑기 — Claude Code 구독(하위 에이전트)으로 실행하는 하네스.

LLM 호출은 이 스크립트가 하지 않는다. 코드는 입력 준비와 출력 검증만 맡는다 (토큰 절약).

  prepare  S2 원문(index.csv 에 있는 기사) + S3 표본에서 별칭 주변 조각만 잘라 묶음(batch, 10~15건)을 만들고,
           묶음 여러 개를 에이전트 1개 몫(bundle)으로 합친다 → 에이전트는 파일 1개 읽고 1개 씀 (도구 호출 최소)
           v1(research/claims/_v1)에서 이미 S4 를 돌린 항목은 묶음에 넣지 않고 v1 주장을 그대로 씀 (재사용)
           research/claims/{player}/bundles/agent_NN.json, manifest.json
  (에이전트) research/config/s4_prompt.md 를 따라 bundles/agent_NN.json → out/agent_NN.json
  collect  out/*.json 형식 검증(묶음 단위) + 재사용 주장 합치기 + 주장마다 patch(코드가 날짜로 배정)
           → research/claims/{player}.jsonl, 탈락·누락 목록 출력
  usage    에이전트 1회의 토큰·시간을 research/logs/usage.csv 에 기록 (runctl.py usage 와 같음)
  중단 후 이어하기: prepare 를 다시 하지 말고 `runctl.py status` 로 남은 에이전트만 다시 실행 (prepare 는 --force 없으면 거부)

사용
  python extract_claims.py prepare --player Doran
  python extract_claims.py collect --player Doran
  python extract_claims.py usage --player Doran --agent a1 --batches 1-6 --tokens 51234 --seconds 210
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

from common import ARTICLES, LOGS, RESEARCH, SAMPLES, AliasMatcher, get_logger, load_config, players, read_jsonl, write_jsonl, slug, targets
from patch_windows import patch_of, windows

log = get_logger("S4")
CLAIMS = RESEARCH / "claims"
PROMPT = RESEARCH / "config" / "s4_prompt.md"
SOURCE_TYPES = {"article", "interview", "article_en", "interview_en", "opgg",
                "inven", "youtube_lck", "youtube_team", "youtube_analysis"}
STANCES = {"positive", "negative", "mixed", "neutral_fact", "sarcastic"}
SPEAKERS = {"self", "journalist", "analyst", "fan"}
REQUIRED = ["player", "source_type", "source_id", "source_url", "date", "aspect", "stance", "claim", "quote", "speaker"]
GAP = "\n[…]\n"

# 문장 경계: 문장부호(+닫는 따옴표) 뒤 공백, 또는 줄바꿈
_BOUND = re.compile(r"[.!?…]+[\"”’')\]]*\s+|\n+")


def sentence_spans(text: str) -> list[tuple[int, int]]:
    cuts = [0] + [m.end() for m in _BOUND.finditer(text)] + [len(text)]
    spans = []
    for a, b in zip(cuts, cuts[1:]):
        seg = text[a:b]
        if seg.strip():
            lead = len(seg) - len(seg.lstrip())
            spans.append((a + lead, a + len(seg.rstrip())))
    return spans


def snippet(text: str, matcher: AliasMatcher, ctx: int, cap: int) -> str:
    """별칭이 있는 문장 ± ctx 문장만, 원문 조각을 그대로 잘라 이어 붙임 (S5 원문 대조가 되도록 글자 변경 없음)."""
    if len(text) <= cap:
        return text
    spans = sentence_spans(text)
    hits = matcher.spans(text)
    keep = set()
    for i, (a, b) in enumerate(spans):
        if any(a <= s < b for s, _ in hits):
            keep.update(range(max(0, i - ctx), min(len(spans), i + ctx + 1)))
    if not keep:  # 제목에만 이름이 있는 글 등 → 앞부분
        keep = set(range(min(3, len(spans))))
    runs, cur = [], []
    for i in sorted(keep):
        if cur and i != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(i)
    runs.append(cur)
    parts, total = [], 0
    for r in runs:
        piece = text[spans[r[0]][0]:spans[r[-1]][1]]
        if total + len(piece) > cap:
            if not parts:
                parts.append(piece[:cap])
            break
        parts.append(piece)
        total += len(piece) + len(GAP)
    return GAP.join(parts)


def load_items(pid: str, pcfg: dict, cfg: dict) -> list[dict]:
    m = AliasMatcher(pcfg)
    wins = windows("research")
    items = []
    with open(ARTICLES / slug(pid) / "index.csv", encoding="utf-8-sig") as fh:
        ids = [r["id"] for r in csv.DictReader(fh)]  # 이번 S1 선택분만 (폴더에 남은 v1 기사 제외)
    for aid in ids:
        f = ARTICLES / slug(pid) / f"{aid}.json"
        if not f.exists():
            log.warning("%s: 기사 %s 본문 파일 없음 — S2 먼저", pid, aid)
            continue
        a = json.loads(f.read_text(encoding="utf-8"))
        icfg = cfg["interview"] if a["type"] in ("interview", "interview_en") else cfg
        items.append({
            "source_id": f"article:{a['id']}", "source_type": a["type"], "url": a["url"],
            "date": a["published"], "title": a.get("title_full") or a["title"],
            "text_source": a["text_source"], "patch": a.get("patch") or patch_of(a["published"], wins),
            "text": snippet(a["text"], m, icfg["context_sentences"], icfg["max_snippet_chars"]),
        })
    for r in read_jsonl(SAMPLES / f"{slug(pid)}.jsonl"):
        it = {"source_id": r["id"], "source_type": r["source"], "url": r["url"], "date": r["date"], "patch": r["patch"]}
        if r["source"] in ("inven", "opgg"):
            it["title"] = r["title"]
            body = snippet(r["body"], m, cfg["context_sentences"], cfg["max_snippet_chars"]) if r["body"] else ""
            it["text"] = r["title"] + ("\n" + body if body else "")
        else:
            it["video_title"] = r.get("video_title")
            it["channel"] = r.get("channel")
            it["likes"] = r.get("likes")
            it["text"] = snippet(r["text"], m, cfg["context_sentences"], cfg["max_snippet_chars"])
        items.append(it)
    return items


def v1_extracted(pid: str, cfg: dict) -> tuple[set, dict]:
    """v1 에서 S4 를 돌린 항목 id 와 그 주장 (source_id → [주장]). 주장 0개 항목도 '처리됨'."""
    root = RESEARCH.parent / cfg["reuse_from"]
    ids, claims = set(), {}
    for f in (root / slug(pid) / "batches").glob("*.json") if (root / slug(pid) / "batches").exists() else []:
        ids.update(i["source_id"] for i in json.loads(f.read_text(encoding="utf-8"))["items"])
    if (root / f"{slug(pid)}.jsonl").exists():
        for c in read_jsonl(root / f"{slug(pid)}.jsonl"):
            claims.setdefault(c["source_id"], []).append(c)
    return ids, claims


def cmd_prepare(pid: str, force: bool = False):
    cfg = load_config("budget")["s4"]
    done = list((CLAIMS / slug(pid) / "out").glob("*.json")) if (CLAIMS / slug(pid) / "out").exists() else []
    if done and not force:  # 중단 후 다시 prepare 하면 끝난 에이전트 결과까지 지워져 토큰 낭비
        raise SystemExit(f"{pid}: 이미 결과 파일 {len(done)}개가 있음 — 이어하려면 `runctl.py status` 로 남은 에이전트만 "
                         f"다시 실행. 정말 처음부터면 --force")
    pcfg = targets()[pid]
    items = load_items(pid, pcfg, cfg)
    v1_ids, _ = v1_extracted(pid, cfg) if cfg.get("reuse_from") else (set(), {})
    reused = [i["source_id"] for i in items if i["source_id"] in v1_ids]
    todo = [i for i in items if i["source_id"] not in v1_ids]
    total = sum(len(i["text"]) + len(i.get("title") or i.get("video_title") or "") for i in todo)
    if total > cfg["max_input_chars_per_player"]:
        raise SystemExit(f"입력 {total:,}자 > 상한 {cfg['max_input_chars_per_player']:,}자 — budget.yaml 확인")
    d = CLAIMS / slug(pid)
    for sub_dir in ("bundles", "out"):
        (d / sub_dir).mkdir(parents=True, exist_ok=True)
        for old in (d / sub_dir).glob("*.json"):
            old.unlink()
    n, per_agent = cfg["batch_size"], cfg["batches_per_agent"]
    target = {"id": pid, "kind": pcfg["kind"], "name_ko": pcfg["name_ko"], "real_name": pcfg["real_name"],
              "team": pcfg["team"], "position": pcfg["position"]}
    if pcfg["kind"] == "team":
        target["players"] = pcfg.get("players", [])
    batches = [{"batch": f"batch_{k // n + 1:03d}", "items": todo[k:k + n]} for k in range(0, len(todo), n)]
    agents = []
    for a in range(0, len(batches), per_agent):
        aid = f"agent_{a // per_agent + 1:02d}"
        part = batches[a:a + per_agent]
        (d / "bundles" / f"{aid}.json").write_text(
            json.dumps({"agent": aid, "target": target, "batches": part}, ensure_ascii=False, indent=1), encoding="utf-8")
        agents.append({"agent": aid, "batches": [b["batch"] for b in part], "n_items": sum(len(b["items"]) for b in part),
                       "chars": sum(len(i["text"]) for b in part for i in b["items"])})
    manifest = {"player": pid, "created": datetime.now().isoformat(timespec="seconds"), "n_items": len(items),
                "n_reused": len(reused), "n_todo": len(todo), "input_chars": total,
                "est_tokens": round(total / cfg["chars_per_token"]), "reused": reused,
                "items_patch": {i["source_id"]: i["patch"] for i in items}, "agents": agents}
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("%s: 자료 %d건 (v1 재사용 %d, 새로 추출 %d) → 묶음 %d개 / 에이전트 %d개, 입력 %s자 (추정 %s토큰)",
             pid, len(items), len(reused), len(todo), len(batches), len(agents), f"{total:,}",
             f"{manifest['est_tokens']:,}")


def validate(claim: dict, pid: str, by_id: dict) -> str | None:
    for k in REQUIRED:
        if not isinstance(claim.get(k), str) or not claim[k].strip():
            return f"missing_{k}"
    if claim["player"] != pid:
        return "wrong_player"
    if claim["source_type"] not in SOURCE_TYPES:
        return "bad_source_type"
    if claim["stance"] not in STANCES:
        return "bad_stance"
    if claim["speaker"] not in SPEAKERS:
        return "bad_speaker"
    item = by_id.get(claim["source_id"])
    if item is None:
        return "unknown_source_id"
    if claim["date"] != item["date"]:
        return "date_not_copied"
    if claim["source_type"] != item["source_type"]:
        return "source_type_not_copied"
    return None


def cmd_collect(pid: str):
    d = CLAIMS / slug(pid)
    cfg = load_config("budget")["s4"]
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    ok, bad, missing, broken = [], [], [], []
    for ag in manifest["agents"]:
        aid = ag["agent"]
        bundle = json.loads((d / "bundles" / f"{aid}.json").read_text(encoding="utf-8"))
        out = d / "out" / f"{aid}.json"
        if not out.exists():
            missing.append(aid)
            continue
        try:
            results = {r["batch"]: r["claims"] for r in json.loads(out.read_text(encoding="utf-8"))["results"]}
        except Exception as e:  # 형식이 틀리면 에이전트 몫 전체를 버리고 재시도 대상
            broken.append(f"{aid} ({type(e).__name__})")
            continue
        for b in bundle["batches"]:
            bid = b["batch"]
            claims = results.get(bid)
            if not isinstance(claims, list):
                missing.append(f"{aid}/{bid}")
                continue
            by_id = {i["source_id"]: i for i in b["items"]}  # 묶음 밖 항목 주장은 unknown_source_id 로 탈락
            for c in claims:
                why = validate(c, pid, by_id) if isinstance(c, dict) else "not_object"
                row = {**c, "batch": bid} if isinstance(c, dict) else {"raw": c, "batch": bid}
                (bad if why else ok).append({**row, "reject": why} if why else row)
    # v1 재사용 주장 (이번 표본에 다시 뽑힌 항목만)
    _, v1_claims = v1_extracted(pid, cfg) if cfg.get("reuse_from") else (set(), {})
    n_reused = 0
    for sid in manifest["reused"]:
        for c in v1_claims.get(sid, []):
            ok.append({k: v for k, v in c.items() if k not in ("claim_id", "batch")} | {"batch": "v1"})
            n_reused += 1
    wins = windows("research")
    for c in ok:
        c["patch"] = patch_of(c["date"], wins)
    for i, c in enumerate(ok):
        c["claim_id"] = f"{pid}-{i + 1:04d}"
    write_jsonl(CLAIMS / f"{slug(pid)}.jsonl", ok)
    write_jsonl(d / "rejected_schema.jsonl", bad)
    log.info("%s: 주장 %d개 (새로 %d, v1 재사용 %d), 형식 탈락 %d개, 출력 없음 %s, JSON 깨짐 %s",
             pid, len(ok), len(ok) - n_reused, n_reused, len(bad), missing, broken)
    from collections import Counter
    if bad:
        log.info("형식 탈락 사유: %s", dict(Counter(b["reject"] for b in bad)))
    if ok:
        log.info("source_type: %s", dict(Counter(c["source_type"] for c in ok)))
        log.info("stance: %s", dict(Counter(c["stance"] for c in ok)))


def cmd_usage(pid: str, agent: str, batches: str, tokens: int, seconds: float, note: str):
    from runctl import log_usage  # 기록은 runctl 한 곳에서 (run 칸 포함)
    log_usage("S4", pid, agent, tokens, seconds, note, batches)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "collect"):
        p = sub.add_parser(name)
        p.add_argument("--player", required=True)
        if name == "prepare":
            p.add_argument("--force", action="store_true", help="결과 파일이 있어도 지우고 처음부터")
    u = sub.add_parser("usage")
    u.add_argument("--player", required=True)
    u.add_argument("--agent", required=True)
    u.add_argument("--batches", required=True)
    u.add_argument("--tokens", type=int, required=True)
    u.add_argument("--seconds", type=float, required=True)
    u.add_argument("--note", default="")
    a = ap.parse_args()
    if a.cmd == "prepare":
        cmd_prepare(a.player, a.force)
    elif a.cmd == "collect":
        cmd_collect(a.player)
    else:
        cmd_usage(a.player, a.agent, a.batches, a.tokens, a.seconds, a.note)


if __name__ == "__main__":
    main()
