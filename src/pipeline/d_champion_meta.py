"""D. 챔피언 메타정보 (Data Dragon) + C의 버전별 기본 스탯.

출력
  data/raw/ddragon/<version>/champion_<lang>.json          원본
  data/processed/d_champions.csv            챔피언 1행: id, key, 한/영 이름, 역할 태그, 딜 타입
  data/processed/d_champion_name_map.csv    별칭(정규화 이름) -> champion_id  (OE/Leaguepedia/패치노트 이름 매핑용)
  data/processed/c_champion_base_stats.csv  YEAR 버전별 기본 스탯 (version x champion)

딜 타입
  1순위 Meraki Analytics `adaptiveType` (PHYSICAL/MAGIC) — Data Dragon 에 없는 정보라 보조 소스로 사용
  2순위 Data Dragon info.attack vs info.magic (차이 <=2 이면 Hybrid)
"""
from __future__ import annotations

import re
import unicodedata

import pandas as pd

from common import PROCESSED, RAW, YEAR, get_logger, http_get, save_csv, save_json, ensure_dir

log = get_logger("D")

DD = "https://ddragon.leagueoflegends.com"
MERAKI = "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/champions.json"
# Meraki 역할 기반 조합 피처: VANGUARD=한타 이니시, CATCHER=픽(그랩/속박) 이니시
ENGAGE_ROLES = {"VANGUARD"}
CATCH_ROLES = {"CATCHER"}

# 대회 데이터에서 쓰이는 표기 -> Data Dragon id
EXTRA_ALIASES = {
    "nunu": "Nunu", "nunuwillump": "Nunu", "wukong": "MonkeyKing", "monkeyking": "MonkeyKing",
    "renata": "Renata", "renataglasc": "Renata", "leblanc": "Leblanc", "drmundo": "DrMundo",
    "kaisa": "Kaisa", "khazix": "Khazix", "chogath": "Chogath", "velkoz": "Velkoz",
    "belveth": "Belveth", "ksante": "KSante", "reksai": "RekSai", "kogmaw": "KogMaw",
    "fiddlesticks": "Fiddlesticks", "jarvaniv": "JarvanIV", "masteryi": "MasterYi",
    "missfortune": "MissFortune", "tahmkench": "TahmKench", "twistedfate": "TwistedFate",
    "xinzhao": "XinZhao", "aurelionsol": "AurelionSol", "leesin": "LeeSin",
}


def norm(name: str) -> str:
    """대소문자/공백/기호 무시한 비교용 키 (한글 유지)."""
    s = unicodedata.normalize("NFKC", str(name)).lower()
    return re.sub(r"[^0-9a-z가-힣]", "", s)


def versions_of_year() -> list[str]:
    vs = http_get(f"{DD}/api/versions.json").json()
    major = str(YEAR - 2010)  # 2026 -> "16"
    return sorted((v for v in vs if v.split(".")[0] == major), key=lambda v: [int(x) for x in v.split(".")])


def champion_json(version: str, lang: str) -> dict:
    path = RAW / "ddragon" / version / f"champion_{lang}.json"
    if path.exists():
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    data = http_get(f"{DD}/cdn/{version}/data/{lang}/champion.json").json()
    save_json(data, path)
    return data


def damage_type(info: dict, meraki: dict | None) -> str:
    if meraki:
        t = meraki.get("adaptiveType")
        if t == "PHYSICAL_DAMAGE":
            return "AD"
        if t == "MAGIC_DAMAGE":
            return "AP"
    a, m = info.get("attack", 0), info.get("magic", 0)
    if abs(a - m) <= 2:
        return "Hybrid"
    return "AD" if a > m else "AP"


def main():
    versions = versions_of_year()
    latest = versions[-1]
    log.info("%d Data Dragon versions: %s", YEAR, versions)

    try:
        meraki = http_get(MERAKI).json()
        save_json(meraki, RAW / "meraki" / "champions.json")
    except Exception as e:  # 보조 소스라 실패해도 진행
        log.warning("Meraki 실패 (%s) — Data Dragon info로 딜 타입 추정", e)
        meraki = {}

    en = champion_json(latest, "en_US")["data"]
    ko = champion_json(latest, "ko_KR")["data"]

    rows, aliases = [], []
    for cid, c in en.items():
        mk = meraki.get(cid) or {}
        roles = mk.get("roles") or []
        rows.append({
            "champion_id": cid,                      # Data Dragon id (e.g. MonkeyKing)
            "champion_key": int(c["key"]),           # 숫자 키 (Riot API championId)
            "name_en": c["name"],
            "name_ko": ko[cid]["name"],
            "tags": "|".join(c["tags"]),             # Fighter/Tank/Mage/Assassin/Marksman/Support
            "primary_tag": c["tags"][0] if c["tags"] else None,
            "damage_type": damage_type(c["info"], mk),
            "info_attack": c["info"]["attack"], "info_defense": c["info"]["defense"],
            "info_magic": c["info"]["magic"], "info_difficulty": c["info"]["difficulty"],
            "meraki_roles": "|".join(roles),
            "is_engage": bool(ENGAGE_ROLES & set(roles)),
            "is_catcher": bool(CATCH_ROLES & set(roles)),
            "attack_range": c["stats"]["attackrange"],
            "is_ranged": c["stats"]["attackrange"] > 300,
            "partype": c["partype"],
            "ddragon_version": latest,
        })
        for a in {cid, c["name"], ko[cid]["name"]}:
            aliases.append({"alias": norm(a), "champion_id": cid})
    for a, cid in EXTRA_ALIASES.items():
        aliases.append({"alias": a, "champion_id": cid})

    champs = pd.DataFrame(rows).sort_values("name_en")
    save_csv(champs, PROCESSED / "d_champions.csv")
    amap = pd.DataFrame(aliases).drop_duplicates("alias")
    save_csv(amap, PROCESSED / "d_champion_name_map.csv")
    log.info("champions=%d, aliases=%d, damage_type=%s", len(champs), len(amap),
             champs["damage_type"].value_counts().to_dict())

    # --- C용: 버전별 기본 스탯
    stat_rows = []
    for v in versions:
        for cid, c in champion_json(v, "en_US")["data"].items():
            stat_rows.append({"ddragon_version": v, "patch": f"{v.split('.')[0]}.{int(v.split('.')[1]):02d}",
                              "champion_id": cid, **c["stats"]})
    stats = pd.DataFrame(stat_rows)
    save_csv(stats, PROCESSED / "c_champion_base_stats.csv")
    log.info("base stats rows=%d (versions=%d)", len(stats), len(versions))


if __name__ == "__main__":
    ensure_dir(PROCESSED)
    main()
