"""Leaguepedia Cargo API 클라이언트 (B, F 공용).

- 기본은 익명 호출. 익명은 레이트리밋이 빡빡함 → 요청 간 sleep + 'ratelimited' 응답 시 지수 백오프.
- .env(또는 환경변수)에 LP_USER / LP_PASS (Special:BotPasswords 에서 발급한 봇 비밀번호)가 있으면
  로그인해서 호출 → 한도가 크게 늘어남. LP_USER 형식: '<Fandom 계정>@<봇 이름>'
- 응답은 data/raw/leaguepedia/<name>.json 에 캐시 (재실행 시 API 호출 생략, --refresh 로 무시).
"""
from __future__ import annotations

import hashlib
import json
import time

import requests

from common import HEADERS, RAW, YEAR, LP_LEAGUE, ensure_dir, env, get_logger

log = get_logger("LP")

API = "https://lol.fandom.com/api.php"
PAGE = 500  # 익명 cargoquery 최대 limit
MIN_INTERVAL = 4.0  # 요청 간 최소 간격(초)


class Cargo:
    def __init__(self, refresh: bool = False):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.refresh = refresh
        self._last = 0.0
        user, pw = env("LP_USER"), env("LP_PASS")
        if user and pw:
            self._login(user, pw)

    def _login(self, user: str, pw: str) -> None:
        tok = self.s.get(API, params={"action": "query", "meta": "tokens", "type": "login", "format": "json"},
                         timeout=60).json()["query"]["tokens"]["logintoken"]
        r = self.s.post(API, data={"action": "login", "lgname": user, "lgpassword": pw,
                                   "lgtoken": tok, "format": "json"}, timeout=60).json()
        result = r.get("login", {}).get("result")
        if result != "Success":
            raise SystemExit(f"Leaguepedia 로그인 실패: {r.get('login')}")
        log.info("Leaguepedia 로그인 성공 (%s)", user)

    def _call(self, params: dict) -> dict:
        for attempt in range(10):
            wait = MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            r = self.s.get(API, params=params, timeout=60)
            data = r.json() if r.ok else {"error": {"code": f"http{r.status_code}"}}
            err = data.get("error", {}).get("code")
            if not err:
                return data
            if err in ("ratelimited", "http429", "http503"):
                sleep = min(30 * 2 ** attempt, 900)
                log.warning("%s — %ds 대기 후 재시도 (%d/10)", err, sleep, attempt + 1)
                time.sleep(sleep)
                continue
            raise RuntimeError(f"cargo error: {data['error']}")
        raise RuntimeError("Leaguepedia 레이트리밋이 풀리지 않음 — 나중에 다시 실행하거나 .env 에 LP_USER/LP_PASS 설정 (캐시된 쿼리는 건너뜀)")

    def query(self, name: str, *, tables: str, fields: str, where: str = "", join_on: str = "",
              order_by: str = "", group_by: str = "") -> list[dict]:
        """전체 페이지를 모아 list[dict] 로 반환 (캐시 사용)."""
        key = hashlib.md5(json.dumps([tables, fields, where, join_on, order_by, group_by]).encode()).hexdigest()[:8]
        cache = ensure_dir(RAW / "leaguepedia") / f"{name}_{key}.json"
        if cache.exists() and not self.refresh:
            log.info("cached: %s", cache.name)
            return json.loads(cache.read_text(encoding="utf-8"))
        rows, offset = [], 0
        while True:
            params = {"action": "cargoquery", "format": "json", "tables": tables, "fields": fields,
                      "where": where, "join_on": join_on, "order_by": order_by, "group_by": group_by,
                      "limit": PAGE, "offset": offset}
            batch = [x["title"] for x in self._call({k: v for k, v in params.items() if v != ""}).get("cargoquery", [])]
            rows += batch
            log.info("%s: +%d (total %d)", name, len(batch), len(rows))
            if len(batch) < PAGE:
                break
            offset += PAGE
        cache.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        return rows


def lck_tournaments(cargo: Cargo) -> list[dict]:
    """YEAR LCK 1부 리그 대회 (LCK Cup, 정규시즌, Road to MSI, 플레이인/플레이오프 등). CL/아카데미 제외."""
    rows = cargo.query(
        "tournaments",
        tables="Tournaments",
        fields="Name, OverviewPage, League, Region, Year, DateStart, Date, Split, TournamentLevel, IsQualifier, IsPlayoffs",
        where=f'Year={YEAR} AND (League="{LP_LEAGUE}" OR League LIKE "LCK Cup%" OR Name LIKE "LCK Cup {YEAR}%")',
        order_by="DateStart",
    )
    bad = ("Challengers", "Academy", "CL ")
    return [r for r in rows if not any(b in (r["Name"] or "") for b in bad)]


def in_clause(field: str, values: list[str]) -> str:
    vals = ", ".join('"' + v.replace('"', '\\"') + '"' for v in values)
    return f"{field} IN ({vals})"
