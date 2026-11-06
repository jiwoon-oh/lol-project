"""G. 솔로랭크 데이터 (Riot Games API, KR), common.YEAR (오늘까지).

키: 환경변수 RIOT_API_KEY 또는 프로젝트 루트 .env 의 `RIOT_API_KEY=RGAPI-...`

단계
  1) apex   : KR 챌린저/그랜드마스터(옵션: 마스터) 솔랭 래더 스냅샷 (league-v4)
  2) pros   : f_rosters.csv 의 Leaguepedia SoloqueueIds + g_ttp_accounts.csv → Riot ID → PUUID (account-v1)
  3) matches: 대상 PUUID 들의 YEAR 솔랭(queue 420) 경기 목록 → 경기 상세 (match-v5)

출력
  data/raw/riot/matches/<matchId>.json.gz       경기 원본 (캐시, 재실행 시 재호출 안 함)
  data/raw/riot/matchids/<YEAR>/<puuid>.json    PUUID별 월별 경기 id 목록, 월당 최대 100개 (캐시)
  data/processed/g_kr_apex_ladder.csv           래더 스냅샷 (※ 조회 시점 기준, 과거 래더는 API로 못 받음)
  data/processed/g_pro_accounts.csv             LCK 선수 ↔ Riot ID ↔ PUUID
  data/processed/g_pro_account_check.csv        계정별 포지션 일치율 (타인 계정 검증)
  data/processed/g_soloq_participants.csv       경기 x 참가자 1행: 패치, 챔피언, 포지션, 승패, KDA, 골드, CS, 딜 비중 …
  data/processed/g_soloq_bans.csv               경기 x 팀 밴

개발 키 한도(20 req/1s, 100 req/2min, 라우팅 값별)를 응답 헤더 기준으로 지킨다. 중단돼도 캐시로 이어서 진행.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import pandas as pd
import requests

from common import PROCESSED, RAW, YEAR, ensure_dir, env, get_logger, save_csv

log = get_logger("G")

PLATFORM = "https://kr.api.riotgames.com"
REGION = "https://asia.api.riotgames.com"
QUEUE_SOLO = 420
KST = timezone(timedelta(hours=9))
START = int(datetime(YEAR, 1, 1, tzinfo=KST).timestamp())
END = min(int(datetime(YEAR + 1, 1, 1, tzinfo=KST).timestamp()), int(time.time()))  # 오늘까지

RIOT = ensure_dir(RAW / "riot")
MIN_CHECK_GAMES = 10   # 계정 검증에 필요한 최소 경기 수
MIN_ROLE_SHARE = 0.4   # 이보다 선수 포지션 비율이 낮으면 타인 계정 의심
LIST_PER_MONTH = 100   # 월별 목록은 한 번에 끝나도록 API 최대치로 받아 캐시 (--per-month 로 경기만 고름)


def load_key() -> str:
    key = env("RIOT_API_KEY")
    if not key:
        raise SystemExit("RIOT_API_KEY 가 없습니다. 프로젝트 루트 .env 에 RIOT_API_KEY=RGAPI-... 를 넣어주세요.")
    return key


class Riot:
    """라우팅 호스트별 슬라이딩 윈도우 레이트리미터 + 429 Retry-After 처리."""

    def __init__(self, key: str):
        self.s = requests.Session()
        self.s.headers["X-Riot-Token"] = key
        self.limits: dict[str, list[tuple[int, int]]] = {}
        self.hist: dict[str, deque] = {}

    def _throttle(self, host: str) -> None:
        limits = self.limits.get(host, [(20, 1), (100, 120)])
        h = self.hist.setdefault(host, deque())
        while True:
            now = time.time()
            wait = 0.0
            for n, sec in limits:
                recent = [t for t in h if now - t < sec]
                if len(recent) >= n - 1:  # 1개 여유
                    wait = max(wait, sec - (now - recent[-(n - 1)]) + 0.05)
            if wait <= 0:
                break
            time.sleep(wait)
        h.append(time.time())
        while h and time.time() - h[0] > 130:
            h.popleft()

    def get(self, url: str, params=None):
        host = url.split("/")[2]
        for attempt in range(8):
            self._throttle(host)
            r = self.s.get(url, params=params, timeout=30)
            if "X-App-Rate-Limit" in r.headers:
                self.limits[host] = [tuple(map(int, x.split(":"))) for x in r.headers["X-App-Rate-Limit"].split(",")]
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                raise SystemExit(f"Riot API {r.status_code}: 키가 없거나 만료됨 (개발 키는 24시간마다 재발급)")
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                log.warning("429 — %ds 대기", wait)
                time.sleep(wait)
                continue
            time.sleep(2 ** attempt)  # 5xx
        raise RuntimeError(f"failed: {url}")


# ---------------------------------------------------------------- 1) apex
def apex(riot: Riot, include_master: bool) -> pd.DataFrame:
    tiers = ["challenger", "grandmaster"] + (["master"] if include_master else [])
    rows, snap = [], datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for t in tiers:
        d = riot.get(f"{PLATFORM}/lol/league/v4/{t}leagues/by-queue/RANKED_SOLO_5x5")
        for e in d["entries"]:
            rows.append({"snapshot_kst": snap, "tier": d["tier"], "leaguePoints": e["leaguePoints"],
                         "wins": e["wins"], "losses": e["losses"], "puuid": e.get("puuid"),
                         "hotStreak": e.get("hotStreak"), "veteran": e.get("veteran")})
    df = pd.DataFrame(rows).sort_values("leaguePoints", ascending=False)
    df.insert(1, "ladder_rank", range(1, len(df) + 1))
    save_csv(df, PROCESSED / "g_kr_apex_ladder.csv")
    log.info("apex ladder: %d players (%s)", len(df), df["tier"].value_counts().to_dict())
    return df


# ---------------------------------------------------------------- 2) pros
def parse_soloq_ids(raw: str | None) -> list[str]:
    """Leaguepedia SoloqueueIds (위키 마크업 섞인 자유 텍스트) → ['이름#태그', ...] (KR 계정 위주)."""
    if not raw or not isinstance(raw, str):
        return []
    s = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)  # <br> = 계정 구분자
    s = re.sub(r"<[^>]+>|'''|''|\[\[|\]\]", " ", s)
    out = []
    # 'KR: a, b; EUW: c' 형태 → 지역 블록으로 나눠 KR 블록 우선
    blocks = re.split(r"\b([A-Z]{2,4})\s*:", s)
    pairs = [(blocks[i], blocks[i + 1]) for i in range(1, len(blocks) - 1, 2)] or [("KR", s)]
    for region, body in pairs:
        if region.upper() not in ("KR", "KOR"):
            continue
        for tok in re.split(r"[,;/\n]| or ", body):
            tok = tok.strip(" .")
            if not tok:
                continue
            out.append(tok if "#" in tok else f"{tok}#KR1")
    return list(dict.fromkeys(out))


def pros(riot: Riot) -> pd.DataFrame:
    src = PROCESSED / "f_rosters.csv"
    if not src.exists():
        log.warning("f_rosters.csv 없음 — F 먼저 실행. 프로 계정 단계 생략")
        return pd.DataFrame()
    r = pd.read_csv(src)
    # YEAR LCK 에 실제 출전한 선수만 (로스터 등록만 된 서브/코치 제외)
    r = r[~r["Role"].eq("Coach") & (r["games_played"] > 0)]
    r = r.sort_values("OverviewPage").drop_duplicates("Player", keep="last")

    # 후보 계정: Leaguepedia + trackingthepros (같은 계정은 소스를 합침, Riot ID는 대소문자 무시)
    cand: dict[tuple[str, str], dict] = {}
    for _, p in r.iterrows():
        for rid in parse_soloq_ids(p["SoloqueueIds"]):
            cand.setdefault((p["Player"], rid.lower()), {"player": p["Player"], "team": p["Team"], "role": p["Role"],
                                                         "riot_id": rid, "source": "leaguepedia"})
    ttp = PROCESSED / "g_ttp_accounts.csv"
    if ttp.exists():
        t = pd.read_csv(ttp)
        keep = dict(zip(r["Player"], zip(r["Team"], r["Role"])))
        for _, x in t[t["native_verified"]].iterrows():
            if x["player"] not in keep:
                continue
            c = cand.setdefault((x["player"], x["riot_id"].lower()),
                                {"player": x["player"], "team": keep[x["player"]][0], "role": keep[x["player"]][1],
                                 "riot_id": x["riot_id"], "source": ""})
            c["source"] = "+".join(filter(None, [c["source"], "ttp"]))
            c.update(ttp_rank=x["rank_now"], ttp_inactive=x["inactive"])

    cache_f = RIOT / "accounts.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    rows = []
    for c in cand.values():
        key = c["riot_id"].lower()
        if key not in cache:
            name, tag = c["riot_id"].rsplit("#", 1)
            acc = riot.get(f"{REGION}/riot/account/v1/accounts/by-riot-id/{quote(name.strip(), safe='')}/{quote(tag.strip(), safe='')}")
            cache[key] = acc
            cache_f.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        acc = cache[key]
        rows.append({**c, "puuid": acc["puuid"] if acc else None,
                     "riot_id_now": f"{acc.get('gameName')}#{acc.get('tagLine')}" if acc else None})
    df = pd.DataFrame(rows)
    save_csv(df, PROCESSED / "g_pro_accounts.csv")
    if len(df):
        log.info("pro accounts: %d ids, resolved %d, players covered %d/%d, sources=%s",
                 len(df), df["puuid"].notna().sum(), df.dropna(subset=["puuid"])["player"].nunique(), len(r),
                 df["source"].value_counts().to_dict())
    return df


def account_check(parts: pd.DataFrame, accts: pd.DataFrame) -> pd.DataFrame:
    """계정 주인 검증: 경기 포지션이 선수 포지션과 맞는지 본다. Riot ID 는 재사용될 수 있어 타인 계정이 섞인다."""
    pos = {"Top": "TOP", "Jungle": "JUNGLE", "Mid": "MIDDLE", "Bot": "BOTTOM", "Support": "UTILITY"}
    role = dict(zip(accts["player"], accts["role"]))
    rows = []
    own = parts[parts["pro_player"].notna() & ~parts["is_remake"]]
    for (player, puuid), g in own.groupby(["pro_player", "puuid"]):
        share = (g["teamPosition"] == pos.get(role.get(player))).mean()
        rows.append({"player": player, "role": role.get(player), "puuid": puuid,
                     "riot_ids": "|".join(sorted(g["riot_id"].unique())), "n_matches": g["matchId"].nunique(),
                     "role_share": round(share, 3),
                     "main_position": g["teamPosition"].mode().iat[0] if g["teamPosition"].notna().any() else None,
                     "win_rate": round(g["win"].mean(), 3),
                     "suspect": len(g) >= MIN_CHECK_GAMES and share < MIN_ROLE_SHARE})
    return pd.DataFrame(rows).sort_values(["player", "n_matches"], ascending=[True, False])


# ---------------------------------------------------------------- 3) matches
def month_start(month: int) -> int:
    """YEAR 의 month 월 1일 00:00 KST (13 이면 다음 해 1월)."""
    y, m = (YEAR + 1, 1) if month == 13 else (YEAR, month)
    return int(datetime(y, m, 1, tzinfo=KST).timestamp())


def month_lists(riot: Riot, puuid: str) -> list[list[str]]:
    """오늘까지의 월별 경기 id 목록 (월당 최대 LIST_PER_MONTH, 최신순)."""
    f = ensure_dir(RIOT / "matchids" / str(YEAR)) / f"{puuid}.json"
    if f.exists():
        return json.loads(f.read_text())
    out = []
    for m in range(1, 13):
        if month_start(m) >= END:
            break
        out.append(riot.get(f"{REGION}/lol/match/v5/matches/by-puuid/{puuid}/ids",
                            {"queue": QUEUE_SOLO, "startTime": month_start(m), "endTime": min(month_start(m + 1), END),
                             "start": 0, "count": LIST_PER_MONTH}) or [])
    f.write_text(json.dumps(out))
    return out


def pick(ids: list[str], per_month: int) -> list[str]:
    """한 달 목록에서 per_month 개를 고르게 뽑는다 (앞에서 자르면 월말 경기로 쏠림)."""
    return ids[:: max(1, len(ids) // per_month)][:per_month]


def match(riot: Riot, mid: str) -> dict | None:
    f = ensure_dir(RIOT / "matches") / f"{mid}.json.gz"
    if f.exists():
        return json.loads(gzip.decompress(f.read_bytes()))
    d = riot.get(f"{REGION}/lol/match/v5/matches/{mid}")
    if d:
        f.write_bytes(gzip.compress(json.dumps(d).encode()))
    return d


def flatten(matches: list[dict], pro_map: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    champs = pd.read_csv(PROCESSED / "d_champions.csv")
    key2id = dict(zip(champs["champion_key"], champs["champion_id"]))
    prow, brow = [], []
    for m in matches:
        info, mid = m["info"], m["metadata"]["matchId"]
        ver = info.get("gameVersion", "")
        patch = ".".join(f"{int(x):02d}" if i else x for i, x in enumerate(ver.split(".")[:2])) if ver else None
        start = datetime.fromtimestamp(info["gameStartTimestamp"] / 1000, KST).strftime("%Y-%m-%d %H:%M")
        team_dmg = {}
        for p in info["participants"]:
            team_dmg[p["teamId"]] = team_dmg.get(p["teamId"], 0) + p["totalDamageDealtToChampions"]
        for p in info["participants"]:
            cs = p["totalMinionsKilled"] + p["neutralMinionsKilled"]
            prow.append({
                "matchId": mid, "game_start_kst": start, "gameVersion": ver, "patch": patch,
                "gameDuration": info["gameDuration"], "is_remake": info["gameDuration"] < 300,
                "puuid": p["puuid"],
                "riot_id": f"{p.get('riotIdGameName')}#{p.get('riotIdTagline')}",
                "pro_player": pro_map.get(p["puuid"]),
                "side": "Blue" if p["teamId"] == 100 else "Red",
                "teamPosition": p.get("teamPosition"), "champion_id": key2id.get(p["championId"], p["championName"]),
                "win": p["win"], "kills": p["kills"], "deaths": p["deaths"], "assists": p["assists"],
                "kda": (p["kills"] + p["assists"]) / max(p["deaths"], 1),
                "goldEarned": p["goldEarned"], "cs": cs, "cspm": cs / max(info["gameDuration"] / 60, 1),
                "damageToChampions": p["totalDamageDealtToChampions"],
                "damageshare": p["totalDamageDealtToChampions"] / max(team_dmg[p["teamId"]], 1),
                "visionScore": p["visionScore"], "champLevel": p["champLevel"],
            })
        for t in info["teams"]:
            for b in t.get("bans", []):
                if b["championId"] > 0:
                    brow.append({"matchId": mid, "patch": patch, "side": "Blue" if t["teamId"] == 100 else "Red",
                                 "pickTurn": b["pickTurn"], "champion_id": key2id.get(b["championId"], b["championId"])})
    return pd.DataFrame(prow), pd.DataFrame(brow)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="apex,pros,matches", help="실행 단계 (쉼표 구분), ids-only 를 더하면 경기 상세는 생략")
    ap.add_argument("--include-master", action="store_true", help="래더에 마스터 포함")
    ap.add_argument("--apex-top", type=int, default=30, help="경기 수집 대상에 넣을 래더 상위 N명")
    ap.add_argument("--per-month", type=int, default=30, help="PUUID당 월별 솔랭 경기 최대 수")
    args = ap.parse_args()
    steps = set(args.steps.split(","))
    riot = Riot(load_key())

    ladder = apex(riot, args.include_master) if "apex" in steps else (
        pd.read_csv(PROCESSED / "g_kr_apex_ladder.csv") if (PROCESSED / "g_kr_apex_ladder.csv").exists() else pd.DataFrame())
    accts = pros(riot) if "pros" in steps else (
        pd.read_csv(PROCESSED / "g_pro_accounts.csv") if (PROCESSED / "g_pro_accounts.csv").exists() else pd.DataFrame())

    if "matches" not in steps:
        return
    pro_map = {}
    if len(accts):
        a = accts.dropna(subset=["puuid"])
        # 같은 계정이 두 선수에 등록돼 있으면 trackingthepros 쪽을 우선
        a = a.assign(_ttp=a["source"].str.contains("ttp")).sort_values("_ttp")
        pro_map = dict(zip(a["puuid"], a["player"]))
    targets = list(pro_map) + (ladder["puuid"].dropna().head(args.apex_top).tolist() if len(ladder) else [])
    targets = list(dict.fromkeys(targets))
    log.info("match targets: %d puuids (pros %d + apex top %d)", len(targets), len(pro_map), args.apex_top)

    all_ids = []
    for i, pu in enumerate(targets, 1):
        for ids in month_lists(riot, pu):
            all_ids += pick(ids, args.per_month)
        if i % 10 == 0:
            log.info("match ids: %d/%d players, %d ids", i, len(targets), len(set(all_ids)))
    all_ids = list(dict.fromkeys(all_ids))
    log.info("unique %d solo matches: %d", YEAR, len(all_ids))
    if "ids-only" in steps:
        return

    matches = []
    for i, mid in enumerate(all_ids, 1):
        d = match(riot, mid)
        if d and d["info"].get("participants"):  # 참가자가 빈 응답이 소수 있음 (queueId 0)
            matches.append(d)
        if i % 100 == 0:
            log.info("matches: %d/%d", i, len(all_ids))
    parts, bans = flatten(matches, pro_map)
    check = account_check(parts, accts)
    save_csv(check, PROCESSED / "g_pro_account_check.csv")
    bad = set(check.loc[check["suspect"], "puuid"])
    parts.loc[parts["puuid"].isin(bad), "pro_player"] = None  # 포지션이 안 맞는 계정은 선수 라벨 제거
    log.info("account check: %d accounts, suspect %d", len(check), len(bad))
    save_csv(parts, PROCESSED / "g_soloq_participants.csv")
    save_csv(bans, PROCESSED / "g_soloq_bans.csv")
    log.info("participants=%d, bans=%d, matches=%d", len(parts), len(bans), len(matches))


if __name__ == "__main__":
    main()
