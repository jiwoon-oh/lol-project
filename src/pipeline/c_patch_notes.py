"""C. Riot 공식 패치노트(KR) — common.YEAR 챔피언 변경 내역.

출력
  data/raw/patch_notes/<slug>.html                원본 HTML
  data/processed/c_patches.csv                    패치 1행: riot_patch, patch(OE 표기), ddragon_version, 게시일, URL
  data/processed/c_patch_champion_changes.csv     변경 1행: 패치 x 챔피언 x 스킬 x 항목, before/after, 방향
  data/processed/c_patch_champion_summary.csv     패치 x 챔피언 1행: buff/nerf/adjust 라벨, 요약, 의도(context)

buff/nerf 라벨은 휴리스틱:
  각 변경 줄의 '⇒' 앞/뒤 첫 수치 묶음 평균을 비교, '재사용 대기시간·소모·비용' 등은 낮을수록 버프로 뒤집음.
  챔피언 단위로 +만 있으면 buff, -만 있으면 nerf, 섞이거나 수치 없음(신규/제거/리워크)이면 adjust.
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd
from bs4 import BeautifulSoup

from common import PROCESSED, RAW, YEAR, ensure_dir, get_logger, http_get, save_csv

log = get_logger("C")

BASE = "https://www.leagueoflegends.com"
TAG_URL = f"{BASE}/ko-kr/news/tags/patch-notes/"
YY = YEAR % 100  # 25

# 값이 낮아질수록 챔피언에게 유리한 항목
LOWER_IS_BETTER = ["재사용 대기시간", "소모", "비용", "시전 시간", "충전 시간", "쿨다운", "지연 시간",
                   "받는 피해", "감소 시간", "기력", "선딜레이"]
NUM = r"-?\d+(?:\.\d+)?"


def patch_slugs() -> list[tuple[str, str]]:
    """태그 페이지에서 YEAR 패치노트 (riot_patch, path) 목록."""
    html = http_get(TAG_URL).text
    paths = set(re.findall(r'/ko-kr/news/game-updates/(?:league-of-legends-)?patch-[a-z0-9-]+', html))
    out = []
    for p in paths:
        m = re.search(rf"patch-(?:20)?{YY}-(s1-)?(\d+)-notes", p)
        if not m:
            continue
        n = int(m.group(2))
        riot = f"{YY}.S1.{n}" if m.group(1) else f"{YY}.{n:02d}"
        out.append((riot, p.rstrip("/") + "/"))
    # 패치 번호순 (2025는 S1.1~S1.3 = 1~3번째)
    def order(x):
        r = x[0].split(".")
        return int(r[-1])
    return sorted(set(out), key=order)


def patch_index(riot_patch: str) -> int:
    return int(riot_patch.split(".")[-1])


def fetch(path: str) -> str:
    slug = path.strip("/").split("/")[-1]
    f = ensure_dir(RAW / "patch_notes") / f"{slug}.html"
    if f.exists():
        return f.read_text(encoding="utf-8")
    html = http_get(BASE + path).text
    f.write_text(html, encoding="utf-8")
    return html


def nums(s: str) -> list[float]:
    # '(11레벨에서 최소)', '(레벨에 따라)' 같은 레벨 표기는 수치 비교에서 제외
    s = re.sub(r"\([^()]*레벨[^()]*\)", "", s.replace(",", ""))
    return [float(x) for x in re.findall(NUM, s)]


def direction(stat: str, text: str) -> int | None:
    """+1 버프, -1 너프, 0 동일, None 판단불가."""
    if "⇒" not in text:
        return None
    before, after = text.split("⇒", 1)
    b, a = nums(before), nums(after)
    if not b or not a:
        return None
    # 앞쪽은 '⇒' 직전 수치 묶음만 (스탯명에 숫자가 섞인 경우 대비해 같은 길이로 맞춤)
    k = min(len(b), len(a))
    b, a = b[-k:], a[:k]
    diff = sum(a) / k - sum(b) / k
    if abs(diff) < 1e-9:
        return 0
    sign = 1 if diff > 0 else -1
    if any(w in stat for w in LOWER_IS_BETTER):
        sign = -sign
    return sign


def champ_id_from_block(h3) -> str | None:
    for img in h3.find_all_previous("img", limit=1):
        m = re.search(r"/img/champion/([A-Za-z0-9]+)\.png", img.get("src", ""))
        if m:
            return m.group(1)
    a = h3.find("a", href=True)
    if a:
        m = re.search(r"/champions/([a-z0-9-]+)/?", a["href"])
        if m:
            return m.group(1)
    return None


def parse(html: str, riot_patch: str) -> tuple[dict, list[dict], list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    t = soup.find("time")
    published = t["datetime"][:10] if t and t.has_attr("datetime") else None

    changes, summaries = [], []
    # 패치마다 마크업이 다름(id/class 유무) → 첫 번째 '챔피언' h2 ~ 다음 h2 사이의 h3를 챔피언 블록으로 본다.
    # (뒤쪽 '챔피언' h2는 무작위 총력전/신속 대전 등 모드별 조정이라 제외)
    champ_h2 = next((h for h in soup.find_all("h2")
                     if h.get("id") == "patch-champions" or h.get_text(strip=True) == "챔피언"), None)
    blocks = []
    if champ_h2:
        for el in champ_h2.find_all_next(["h2", "h3"]):
            if el.name == "h2":
                break
            blocks.append(el)
    for h3 in blocks:
        cid = champ_id_from_block(h3)
        name_ko = h3.get_text(strip=True)
        # 같은 블록(이 h3 ~ 첫 h4 전)에 있는 요약/의도만 채택
        summary = h3.find_next("p")
        context = h3.find_next("blockquote")
        if summary and (summary.find_parent("li") or summary.find_previous(["h3", "h4"]) is not h3):
            summary = None
        if context and context.find_previous(["h3", "h4"]) is not h3:
            context = None
        dirs = []
        # 다음 h3 전까지의 h4(스킬) / li(항목)
        ability = None
        for el in h3.find_all_next(["h3", "h4", "li", "h2"]):
            if el.name in ("h3", "h2"):
                break
            if el.name == "h4":
                ability = el.get_text(" ", strip=True)
                continue
            text = el.get_text(" ", strip=True)
            stat = el.find("strong").get_text(" ", strip=True) if el.find("strong") else ""
            before = after = None
            if "⇒" in text:
                before, after = [x.strip(" :") for x in text.split("⇒", 1)]
                if stat and before.startswith(stat):
                    before = before[len(stat):].strip(" :")
            d = direction(stat, text)
            dirs.append(d)
            changes.append({"riot_patch": riot_patch, "champion_id": cid, "name_ko": name_ko,
                            "ability": ability, "stat": stat, "before": before, "after": after,
                            "change_text": text, "direction": d})
        known = [d for d in dirs if d]
        if known and all(d > 0 for d in known) and None not in dirs:
            label = "buff"
        elif known and all(d < 0 for d in known) and None not in dirs:
            label = "nerf"
        elif known and sum(known) > 0:
            label = "adjust_buff"
        elif known and sum(known) < 0:
            label = "adjust_nerf"
        else:
            label = "adjust"
        summaries.append({"riot_patch": riot_patch, "champion_id": cid, "name_ko": name_ko,
                          "label": label, "n_changes": len(dirs),
                          "n_buff_lines": sum(1 for d in dirs if d and d > 0),
                          "n_nerf_lines": sum(1 for d in dirs if d and d < 0),
                          "summary": summary.get_text(" ", strip=True) if summary else None,
                          "context": context.get_text(" ", strip=True) if context else None})
    return {"published": published}, changes, summaries


def main():
    slugs = patch_slugs()
    log.info("%d patches found: %s", YEAR, [s[0] for s in slugs])
    patches, all_changes, all_summ = [], [], []
    for riot, path in slugs:
        meta, ch, sm = parse(fetch(path), riot)
        idx = patch_index(riot)
        patches.append({"riot_patch": riot, "patch": f"{YEAR - 2010}.{idx:02d}",
                        "ddragon_version": f"{YEAR - 2010}.{idx}.1",
                        "published": meta["published"], "url": BASE + path,
                        "n_champions_changed": len(sm)})
        all_changes += ch
        all_summ += sm
        log.info("%s: %d champions, %d lines", riot, len(sm), len(ch))

    pdf = pd.DataFrame(patches)
    save_csv(pdf, PROCESSED / "c_patches.csv")
    key = pdf[["riot_patch", "patch"]]
    save_csv(pd.DataFrame(all_changes).merge(key, on="riot_patch"), PROCESSED / "c_patch_champion_changes.csv")
    summ = pd.DataFrame(all_summ).merge(key, on="riot_patch")
    save_csv(summ, PROCESSED / "c_patch_champion_summary.csv")
    log.info("labels: %s", summ["label"].value_counts().to_dict())


if __name__ == "__main__":
    main()
