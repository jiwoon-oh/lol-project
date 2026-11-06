"""S3. 커뮤니티 표본 — 로컬 인벤·유튜브 + OP.GG 톡(collect_opgg.py) 원문에서 선수별 표본 추출 (토큰 0).
리서치 패치 창 안 자료만, 패치별 같은 몫 (sampling.yaml). v1 에서 S4 를 돌린 항목을 먼저 뽑아 재사용.

기존 사전 판정은 쓰지 않음. 별칭은 "선수 이름이 들어간 글"을 고르는 데에만 사용.

입력
  data/inven_2026/search/{inven_search}.csv   post_id,title,date
  data/inven_2026/bodies.jsonl                post_id,body,date
  data/youtube_2026/comments/*.jsonl          comment_id,video_id,parent_id,text,likes,published
  data/youtube_2026/videos.csv, channels.json
출력
  research/samples/{player}.jsonl
  research/logs/s3_summary.csv

사용: python sample_community.py [--players Doran,Oner,Faker]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter, defaultdict

from common import (INVEN, LOGS, RESEARCH, ROOT, SAMPLES, YOUTUBE, AliasMatcher, get_logger, load_config,
                    nfc, players, read_jsonl, write_csv, write_jsonl, slug, targets)
from patch_windows import patch_of, windows

log = get_logger("S3")
INVEN_URL = "https://www.inven.co.kr/board/lol/4625/{}"
YT_URL = "https://www.youtube.com/watch?v={}&lc={}"


def stratified(items: list[dict], n: int, rng: random.Random, order=None, date_key: str = "date",
               key=None) -> list[dict]:
    """칸(기본: 월, key 를 주면 그 값 — 예: 패치)별로 고르게 n개. 남는 몫은 자료가 남은 칸에 다시 나눔.
    order가 있으면 칸 안에서 그 순서대로(예: 좋아요 순), 없으면 무작위."""
    by_month: dict[str, list] = defaultdict(list)
    for it in items:
        by_month[key(it) if key else it[date_key][:7]].append(it)
    for m, lst in by_month.items():
        if order:
            lst.sort(key=order)
        else:
            rng.shuffle(lst)
    picked: dict[str, int] = {m: 0 for m in by_month}
    left = n
    while left > 0:
        open_months = [m for m in sorted(by_month) if picked[m] < len(by_month[m])]
        if not open_months:
            break
        share = max(1, left // len(open_months))
        for m in open_months:
            take = min(share, len(by_month[m]) - picked[m], left)
            picked[m] += take
            left -= take
            if left == 0:
                break
    out = [it for m in sorted(by_month) for it in by_month[m][:picked[m]]]
    return sorted(out, key=lambda r: r[date_key])


def per_patch(items: list[dict], n: int, rng: random.Random, patches: list[str], order=None,
              prefer: set | None = None) -> list[dict]:
    """리서치 패치마다 같은 몫(n / 패치 수). 모자란 패치를 다른 패치로 채우지 않음 (폼 추이 왜곡 방지).
    prefer 에 있는 id(v1 에서 이미 S4 를 돌린 항목)를 칸 안에서 먼저 뽑음 → S4 재사용."""
    prefer = prefer or set()
    by_patch: dict[str, list] = defaultdict(list)
    for it in items:
        by_patch[it["patch"]].append(it)
    out = []
    for i, p in enumerate(patches):
        lst = by_patch.get(p, [])
        rng.shuffle(lst)
        if order:
            lst.sort(key=order)
        lst.sort(key=lambda it: it["id"] not in prefer)  # 안정 정렬: 재사용 항목 먼저, 나머지 순서 유지
        quota = n // len(patches) + (1 if i < n % len(patches) else 0)
        out += lst[:quota]
    return sorted(out, key=lambda r: r["date"])


def with_patch(rec: dict, wins) -> dict | None:
    rec["patch"] = patch_of(rec["date"], wins)
    return rec if rec["patch"] else None


# ---------- 인벤 ----------

def inven_rows(pcfg: dict):
    """선수: 그 선수 검색 결과. 팀: 팀별 검색 파일이 없어서 모든 선수 검색 결과를 합친 것(제목으로 팀을 거름)."""
    if pcfg["kind"] == "team":
        files = sorted((INVEN / "search").glob("*.csv"))
    elif pcfg.get("inven_search"):
        files = [INVEN / "search" / f"{pcfg['inven_search']}.csv"]
    else:
        files = []  # 인벤 검색 데이터 없는 선수 (Sharvel·Loki)
    for path in files:
        with open(path, encoding="utf-8-sig") as f:
            yield from csv.DictReader(f)


def inven_samples(pid: str, pcfg: dict, matcher: AliasMatcher, bodies: dict, cfg: dict, rng, prefer) -> list[dict]:
    seen, with_body, without = set(), [], []
    if True:
        for r in inven_rows(pcfg):
            post = int(r["post_id"])
            if post in seen or not patch_of(r["date"], cfg["wins"]):
                continue
            seen.add(post)
            if not matcher.found(r["title"]):  # exclude(야구도란 등) 적용
                continue
            body = (bodies.get(post) or "").strip()
            rec = {
                "id": f"inven:{post}", "source": "inven", "channel_type": None, "channel": "inven",
                "date": r["date"], "title": r["title"], "body": body,
                "text": r["title"] + ("\n" + body if body else ""),
                "likes": None, "video_title": None, "url": INVEN_URL.format(post),
                "patch": patch_of(r["date"], cfg["wins"]),
            }
            (with_body if len(body) >= cfg["inven"]["min_body_chars"] else without).append(rec)
    n = cfg["inven"]["n"]
    pats = cfg["patches"]
    out = per_patch(with_body, n, rng, pats, prefer=prefer)
    if cfg["inven"]["fill_with_title_only"]:  # 본문 있는 글이 모자란 패치만 제목 글로 채움
        have = Counter(r["patch"] for r in out)
        for i, p in enumerate(pats):
            quota = n // len(pats) + (1 if i < n % len(pats) else 0)
            extra = [r for r in without if r["patch"] == p]
            rng.shuffle(extra)
            out += extra[:max(0, quota - have[p])]
    log.info("%s inven: 후보 %d (본문 있음 %d) → 표본 %d", pid, len(with_body) + len(without), len(with_body), len(out))
    return out


# ---------- 유튜브 ----------

_HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
_NORM = re.compile(r"[\s\W_]+")


def hangul_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return len(_HANGUL.findall(text)) / len(letters) if letters else 0.0


def load_youtube(cfg: dict):
    """모든 댓글을 한 번만 읽어 (채널, 유형, 댓글) 목록으로."""
    channels = json.loads((YOUTUBE / "channels.json").read_text(encoding="utf-8"))
    ctype = {nfc(k): v["type"] for k, v in channels.items()}
    videos = {}
    with open(YOUTUBE / "videos.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            videos[r["video_id"]] = r["title"]
    ycfg = cfg["youtube"]
    rows = []
    for path in sorted((YOUTUBE / "comments").glob("*.jsonl")):
        ch = nfc(path.stem)
        t = ctype.get(ch)
        if t is None:
            log.warning("channels.json에 없는 채널 파일: %s (건너뜀)", ch)
            continue
        for c in read_jsonl(path):
            text = (c.get("text") or "").strip()
            date = (c.get("published") or "")[:10]
            if not patch_of(date, cfg["wins"]) or len(text) < ycfg["min_chars"]:
                continue
            if hangul_ratio(text) < ycfg["min_hangul_ratio"]:
                continue
            rows.append((ch, t, c, text, date))
    log.info("유튜브 댓글 (리서치 패치 창·한국어·%d자 이상): %d", ycfg["min_chars"], len(rows))
    return rows, videos


def youtube_samples(pid: str, matcher: AliasMatcher, yt_rows, videos, cfg: dict, rng, prefer) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    type_to_group = {t: g for g, gc in cfg["youtube"]["groups"].items() for t in gc["channel_types"]}
    seen_text = set()
    # 좋아요 많은 것부터 보며 같은 문구는 1개만 남김
    for ch, t, c, text, date in sorted(yt_rows, key=lambda r: -(r[2].get("likes") or 0)):
        g = type_to_group.get(t)
        if g is None or not matcher.found(text):
            continue
        key = _NORM.sub("", text.lower())
        if key in seen_text:
            continue
        seen_text.add(key)
        groups[g].append({
            "id": f"yt:{c['comment_id']}", "source": f"youtube_{t}", "channel_type": t, "channel": ch,
            "date": date, "text": text, "likes": c.get("likes") or 0,
            "video_id": c["video_id"], "video_title": videos.get(c["video_id"]),
            "parent_id": c.get("parent_id"), "url": YT_URL.format(c["video_id"], c["comment_id"]),
            "patch": patch_of(date, cfg["wins"]),
        })
    out = []
    for g, gc in cfg["youtube"]["groups"].items():
        s = per_patch(groups[g], gc["n"], rng, cfg["patches"], order=lambda r: -r["likes"], prefer=prefer)
        log.info("%s youtube/%s: 후보 %d → 표본 %d", pid, g, len(groups[g]), len(s))
        out += s
    return out


# ---------- OP.GG 톡 ----------

def opgg_samples(pid: str, matcher: AliasMatcher, cfg: dict, rng, prefer) -> list[dict]:
    path = RESEARCH / "raw" / "opgg" / "posts.jsonl"
    if not path.exists():
        log.warning("%s opgg: %s 없음 — collect_opgg.py 먼저 (건너뜀)", pid, path)
        return []
    cands = []
    for r in read_jsonl(path):
        if pid not in r.get("players", []) or not matcher.found(r["title"]):
            continue
        rec = with_patch({"id": f"opgg:{r['post_id']}", "source": "opgg", "channel_type": None, "channel": "opgg",
                          "date": r["date"], "title": r["title"], "body": r["body"],
                          "text": r["title"] + ("\n" + r["body"] if r["body"] else ""),
                          "likes": None, "video_title": None, "url": r["url"]}, cfg["wins"])
        if rec:
            cands.append(rec)
    out = per_patch(cands, cfg["opgg"]["n"], rng, cfg["patches"], prefer=prefer)
    log.info("%s opgg: 후보 %d → 표본 %d", pid, len(cands), len(out))
    return out


def extracted_ids(pid: str, cfg: dict) -> set:
    """v1 에서 S4 를 이미 돌린 항목 id (주장이 0개였던 항목도 포함 — 다시 돌릴 필요 없음)."""
    d = ROOT / cfg["prefer_extracted"] / slug(pid) / "batches"
    ids = set()
    for f in (d.glob("*.json") if d.exists() else []):
        ids.update(i["source_id"] for i in json.loads(f.read_text(encoding="utf-8"))["items"])
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", default=None, help="쉼표 구분 (기본: players.yaml 전체)")
    args = ap.parse_args()
    cfg = load_config("sampling")
    cfg["wins"] = windows("research")
    cfg["patches"] = [p for p, _, _ in cfg["wins"]]
    allp = targets()
    names = args.players.split(",") if args.players else list(allp)

    bodies = {r["post_id"]: r.get("body") or "" for r in read_jsonl(INVEN / "bodies.jsonl")}
    yt_rows, videos = load_youtube(cfg)

    summary = []
    for pid in names:
        pcfg = allp[pid]
        rng = random.Random(f"{cfg['seed']}-{pid}")
        m = AliasMatcher(pcfg)
        prefer = extracted_ids(pid, cfg) if cfg.get("prefer_extracted") else set()
        rows = (inven_samples(pid, pcfg, m, bodies, cfg, rng, prefer) + opgg_samples(pid, m, cfg, rng, prefer)
                + youtube_samples(pid, m, yt_rows, videos, cfg, rng, prefer))
        for r in rows:
            r["player"] = pid
        write_jsonl(SAMPLES / f"{slug(pid)}.jsonl", rows)
        cnt = Counter(r["source"] for r in rows)
        pats = Counter(r["patch"] for r in rows)
        summary.append({"player": pid, "total": len(rows), **cnt, "reused": sum(r["id"] in prefer for r in rows),
                        "patches": " ".join(f"{k}:{v}" for k, v in sorted(pats.items()))})
    fields = ["player", "total", "inven", "opgg", "youtube_lck", "youtube_team", "youtube_analysis", "reused",
              "patches"]
    write_csv(LOGS / "s3_summary.csv", summary, fields)
    for s in summary:
        log.info("%s", s)


if __name__ == "__main__":
    main()
