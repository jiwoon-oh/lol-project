# LoL 밴픽 분석 데이터 파이프라인 (2026 · LCK)

범위: **2026년(수집일 2026-10-31까지) / LCK 1부 리그만** (LCK Cup, 정규시즌, Road to MSI, 플레이인·플레이오프 포함. LCK CL·아카데미 제외)
제외: E(해설·밴픽 리뷰 자막)
A·B: 2026년 버전을 이 저장소에서 직접 만들었음. A의 원본 CSV는 Drive 한도 때문에 사람이 받아 `data/inven_2026/games/`에 넣어야 해서 `run_all.py`에서는 실행하지 않고 따로 실행함
2025 데이터: 사용하지 않아 결과·원본 캐시 모두 삭제했습니다 (2026-11-02).

| # | 데이터 | 소스 | 스크립트 | 상태 |
|---|---|---|---|---|
| A | 대회 경기 기록 | Oracle's Elixir CSV (Google Drive) | `src/pipeline/a_oracles_elixir.py` | ✅ 502경기 (F의 503세트 중 1세트는 OE 원본에 없고 Leaguepedia에도 Riot 경기 ID가 없음, 이유는 미확인) |
| B | 픽밴 순서 | Leaguepedia Cargo `PicksAndBansS7` + `ScoreboardGames` | `src/pipeline/b_picks_bans.py` | ✅ 503경기, 드래프트 액션 10,060건 |
| C | 패치 변경 내역 | Riot 공식 패치노트(KR) + Data Dragon | `src/pipeline/c_patch_notes.py`, `src/pipeline/d_champion_meta.py` | ✅ 패치 21개(26.01~26.21) / 챔피언 변경 834건 / 기본 스탯 21버전 |
| D | 챔피언 메타정보 | Data Dragon (+ Meraki 보조) | `src/pipeline/d_champion_meta.py` | ✅ 챔피언 173명 |
| F | 로스터·일정 | Leaguepedia Cargo `TournamentPlayers`, `Players`, `MatchSchedule` | `src/pipeline/f_roster_schedule.py` | ✅ 2026 LCK 6개 대회, 10개 팀, 선수 67명(전원 실제 출전 확인)·코치 31명, 매치 188개, 세트 503개 |
| G | 솔로랭크 (KR) | Riot Games API `league-v4`, `account-v1`, `match-v5` (**키 필요**) + trackingthepros 계정 목록 | `src/pipeline/g_accounts_ttp.py`, `src/pipeline/g_riot_soloq.py` | ✅ 래더 1,000명, 2026 솔랭 18,270경기 (계정·월당 30경기). 선수 연결은 67명 중 65명 (6장 한계 참고) |

---

## 1. 실행

```bash
pip install -r requirements.txt
cd src/pipeline
python run_all.py            # D → C → F → TTP → G 순서, 실패한 단계가 있어도 나머지는 계속 실행
python run_all.py --refresh  # (F) Leaguepedia 캐시 무시하고 다시 조회

# 단계별로 실행
python d_champion_meta.py
python c_patch_notes.py
python f_roster_schedule.py [--refresh] [--as-of 2026-09-01]
python g_accounts_ttp.py
python g_riot_soloq.py [--steps apex,pros,matches] [--apex-top 30] [--per-month 30] [--include-master]
# --steps 에 ids-only 를 더하면 경기 id만 세고 상세는 받지 않음 (소요시간 계산용)

# A·B는 run_all에 없음. 따로 실행
python a_oracles_elixir.py [--force] [--file <직접 받은 CSV>]
python b_picks_bans.py [--refresh]
```

- 모든 원본은 `data/raw/`에 캐시됩니다. 재실행하면 다운로드나 API 호출 없이 캐시에서 다시 가공합니다.
- 산출물은 `data/processed/2026/*.csv` (UTF-8 BOM, 엑셀에서 바로 열림)에 저장됩니다.
- 설정은 `src/pipeline/common.py`에 있습니다: `YEAR=2026`, `OE_LEAGUES={"LCK"}`, `LP_LEAGUE="LoL Champions Korea"`. 출력 폴더는 `YEAR`를 따라갑니다.

## 2. 디렉터리

```
lol-project/                (전체 구조는 CLAUDE.md 4장)
├─ docs/PIPELINE.md
├─ requirements.txt
├─ src/pipeline/
│  ├─ common.py            경로·연도·리그 설정, 재시도 GET, CSV 저장
│  ├─ leaguepedia.py       Cargo API 클라이언트 (봇 로그인, 레이트리밋 백오프, 응답 캐시)
│  ├─ a_oracles_elixir.py
│  ├─ b_picks_bans.py
│  ├─ c_patch_notes.py
│  ├─ d_champion_meta.py   (D + C의 버전별 기본 스탯)
│  ├─ f_roster_schedule.py
│  ├─ g_accounts_ttp.py    trackingthepros 프로 계정 목록
│  ├─ g_riot_soloq.py
│  └─ run_all.py
└─ data/
   ├─ inven_2026/games/    <YEAR>_LoL_esports_match_data_from_OraclesElixir.csv (전 리그 원본, 하나만)
   ├─ logs/                2026 실행 로그
   ├─ raw/
   │  ├─ leaguepedia/      <쿼리명>_<해시>.json (Cargo 응답 캐시)
   │  ├─ patch_notes/      KR 패치노트 원본 HTML
   │  ├─ ddragon/<ver>/    champion_en_US.json, champion_ko_KR.json
   │  ├─ meraki/           champions.json
   │  ├─ trackingthepros/  선수 페이지 HTML
   │  └─ riot/             accounts.json, matchids/<YEAR>/<puuid>.json, matches/*.json.gz
   └─ processed/
      └─ 2026/             현재 산출물 (아래 설명 기준)
```

---

## 3. 단계별 상세

### A. Oracle's Elixir 경기 기록 — `a_oracles_elixir.py`

1. **다운로드**: `gdown`으로 Oracle's Elixir 공개 Drive 폴더(`1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH`)를 조회해 `2026_LoL_esports_match_data_from_OraclesElixir.csv`의 file id를 찾아 받습니다.
2. **필터**: `league ∈ {"LCK"}` 이고 `date`가 2026년인 행만 남깁니다. 실행 로그에 "LCK가 들어간 league 값" 목록이 찍히니, LCK Cup이 별도 값으로 나오면 `common.OE_LEAGUES`에 추가하세요.
3. **정규화**: `patch`는 `16.01` 같은 두 자리 문자열로 맞춥니다.
4. **분리**:
   - 선수 행: `position ∈ {top,jng,mid,bot,sup}` → `a_player_games.csv`
   - 팀 행: `position == "team"` → 포지션별 픽·선수를 붙여 넓은 형태로 → `a_team_games.csv`

| 산출물 | 단위 | 주요 컬럼 |
|---|---|---|
| `a_oe_lck_2026_raw.csv` | OE 원본 행 | 원본 전 컬럼 (LCK 2026만) |
| `a_team_games.csv` | 경기 × 사이드 | gameid, date, patch, side, teamname, opp_teamname, result, ban1~5, {top,jng,mid,bot,sup}_pick, {pos}_player, first* 오브젝트, golddiffat10/15 |
| `a_player_games.csv` | 경기 × 선수 | gameid, date, patch, side, teamname, position, playername, champion, result, golddiffat10/15, csdiffat10/15, xpdiffat10/15, damageshare, dpm, earnedgoldshare, vspm, kills/deaths/assists, **kda=(K+A)/max(D,1)** |

쓰는 곳: 승률 모델, 챔피언 티어, 상대팀 성향, 선수 숙련도.

수집 결과: 502경기, 2026-01-14 ~ 09-13, 전 경기 `datacompleteness=complete`, 팀 10개, 경기당 선수 10명·승리 팀 1개. 밴·픽·골드 차이 @15에 빈 값이 없습니다. 선수 전원이 F에 있습니다.
`LCKC`는 `OE_LEAGUES`에 넣지 않아 제외됩니다. LCK Cup도 `LCK` 값에 포함돼 있습니다.

### B. 픽밴 순서 — `b_picks_bans.py`

1. `Tournaments` 테이블에서 `Year=2026 AND League="LoL Champions Korea"`(또는 LCK Cup) 대회의 `OverviewPage`를 구합니다 → `lp_tournaments.csv`
2. `PicksAndBansS7 ⟕ ScoreboardGames (GameId)`를 조회합니다 (500행씩 페이지 단위).
3. **선픽과 진영을 따로 다룹니다.** 2026년 First Selection 규칙으로 진영 선택권을 가진 팀이 진영과 픽 순서 중 하나를 고르고, 상대가 나머지를 갖습니다. 그래서 2025까지처럼 `블루 = 선픽`이 성립하지 않습니다 (레드 선픽 372경기, 블루 선픽 131경기).
   - `PicksAndBansS7.Team1` = **선픽 팀**입니다 (OE `firstPick`, `MatchScheduleGame.FirstPick`과 전부 일치). `SwitchedSides=1`이면 선픽 팀이 레드입니다. `fp_*`(선픽 팀), `lp_*`(후픽 팀) 컬럼으로 저장합니다.
   - `MatchScheduleGame.Blue` = **블루**입니다 (503경기 전부 값이 있고, OE `side`, `ScoreboardPlayers.Side`와 502경기 전부 일치). `blue_team`, `red_team`, `fp_side`, `winner_side`는 여기서 나옵니다.
   - `MatchScheduleGame.FirstSelection`, `PickSelection`에서 선택권 보유 팀(`fs_team`)과 그 팀의 선택(`fs_choice`: `pick`/`side`)을 가져옵니다. 2세트 이후 315경기 전부 `fs_team`이 직전 세트 패자라서 규칙과 일치합니다.
4. 팀 이름은 F와 같은 표준명(`HANJIN BRION`, `Kiwoom DRX`)으로 통일합니다.
5. 표준 토너먼트 드래프트 순서로 1~20번째 액션을 펼칩니다 (F=선픽 팀, L=후픽 팀):

| step | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| phase | ban1 | | | | | | pick1 | | | | | | ban2 | | | | pick2 | | | |
| 팀 | F | L | F | L | F | L | F | L | L | F | F | L | L | F | L | F | L | F | F | L |
| 액션 | 밴1 | 밴1 | 밴2 | 밴2 | 밴3 | 밴3 | 픽1 | 픽1 | 픽2 | 픽2 | 픽3 | 픽3 | 밴4 | 밴4 | 밴5 | 밴5 | 픽4 | 픽4 | 픽5 | 픽5 |

> **밴 단계의 팀 간 순서: 레드 선픽 경기 VOD 1건으로 확인.** 위 표처럼 선픽 팀이 진영과 상관없이 밴도 먼저 합니다. [Gen.G(블루) vs Dplus Kia(레드·선픽)](https://youtu.be/QSxitEXHt20?t=14576)를 사람이 직접 봤을 때 Dplus Kia가 Nocturne으로 첫 밴, Gen.G가 Vi로 다음 밴을 했고, `b_draft_actions`의 step 1·2와 일치합니다. esports.gg 해설 요약과 Leaguepedia의 기록 방식(`SwitchedSides`)도 같은 방향입니다. 다만 Riot 공식 규정 문구는 찾지 못했고, 나머지 레드 선픽 371경기를 하나씩 확인한 것은 아닙니다.

| 산출물 | 단위 | 주요 컬럼 |
|---|---|---|
| `lp_tournaments.csv` | 대회 | Name, OverviewPage, DateStart, Date, Split, IsPlayoffs |
| `b_draft_games.csv` | 경기 | GameId, MatchId, RiotPlatformGameId, OverviewPage, Tab, N_GameInMatch, date_kst, Patch, fp_team, lp_team, fp_side, blue_team, red_team, fs_team, fs_choice, winner_side, winner_team, fp_ban1~5, lp_ban1~5, fp_pick1~5 (픽 순서), lp_pick1~5, {fp,lp}_pick{n}_role |
| `b_draft_actions.csv` | 경기 × step(1~20) | GameId, step, phase, pick_order(fp/lp), side, action(ban/pick), side_order, team, opponent, champion, role, is_winner |

쓰는 곳: 순서 기반 추천, 상대 픽 예측 모델 (`b_draft_actions`를 step 순으로 정렬하면 바로 시퀀스 입력이 됩니다).
2026 LCK도 **피어리스 드래프트**입니다. 188개 매치 전부에서 어느 팀이든 같은 챔피언을 다시 뽑은 경우가 없습니다. 모델 피처에 "이번 매치에서 이미 사용된 챔피언"을 넣으세요.
**모델 피처 주의**: 사이드와 픽 순서를 별개 변수로 넣어야 합니다. 2026 503경기 기준 블루 승률 56.3%, 선픽 팀 승률 48.7%입니다. 선택권 보유 팀은 366경기에서 픽 순서를, 135경기에서 진영을 골랐습니다. 진영을 고를 때는 135경기 모두 블루였고, 픽 순서를 고를 때는 284경기가 선픽, 82경기가 후픽이었습니다. 2025 드래프트 데이터를 같이 학습하면 규칙이 다르다는 점도 반영하세요.

수집 결과 검증 (OE와 대조, 502경기): 블루 팀·승자 불일치 0건, 팀별 픽 구성 불일치 0건, 팀별 밴 순서 불일치 0건, F의 매치 188개·세트 수와 전부 일치, 패치 전부 `c_patches`에 있음, 챔피언 미매핑 0건.
- `Gen.G vs Dplus Kia` 1세트(`LCK/2026 Season/Rounds 3-4_Week 10_8_1`)는 실제로 치러진 경기입니다 (VOD 있음, 몰수·다시하기 표시 없음, 매치 2-0 Dplus Kia). Riot 경기 ID와 `ScoreboardGames`가 없어 A의 경기 지표는 없습니다. B의 드래프트·진영·승자·선택권은 모두 있습니다 (Gen.G 블루, Dplus Kia 레드·선픽). `date_kst`는 실제 시작 시각이 아니라 매치 예정 시각(2026-08-01 19:00)이고, `Patch`는 매치 값 26.14이며 `Gamelength`는 비어 있습니다.
- `Cup_Finals_1_1`, `Season Playoffs_Finals_1_1`은 `FirstSelection`이 `N/A`입니다. 결승 1세트는 Gen.G가 진영과 픽 순서를 모두 정했으므로 `fs_team`, `fs_choice`가 비어 있습니다.
- `LCK/2026 Season/Cup_Week 2_2_1`의 Kiwoom DRX 픽 순서는 OE만 다릅니다 (OE는 Nocturne→Corki, Leaguepedia와 gol.gg는 Corki→Nocturne). B는 Leaguepedia 값을 그대로 씁니다. 두 픽은 선픽 팀의 연속 픽(step 10·11)이라 상대 픽 사이에 끼는 정보는 달라지지 않습니다.

### C. 패치 변경 내역 — `c_patch_notes.py` (+ `d_champion_meta.py`의 기본 스탯)

1. `https://www.leagueoflegends.com/ko-kr/news/tags/patch-notes/`에서 2026 패치노트 URL을 수집합니다 (26.01 ~ 26.21, 21개). 2026 주소는 `league-of-legends-patch-26-N-notes` 형식이라 2025의 `patch-25-N-notes`와 함께 잡도록 정규식을 넓혔습니다.
2. 각 HTML의 **첫 번째 `챔피언` h2 ~ 다음 h2** 구간만 파싱합니다. 뒤쪽 `챔피언` h2는 칼바람·신속 대전 같은 모드별 조정이라 제외합니다.
   - 챔피언 블록 = `h3`. 챔피언 id는 바로 앞 이미지(`/img/champion/<DDragonId>.png`)에서 가져옵니다.
   - 요약 `p`, 개발 의도 `blockquote`, 스킬 `h4`, 변경 항목 `li` (`스탯 : 이전 ⇒ 이후`)
   - 패치마다 마크업이 다릅니다 (class/id 유무, 25.19 이후는 요약 줄 없음). 그래서 class에 의존하지 않고 태그 순서로 파싱합니다.
3. **버프/너프 라벨 (휴리스틱)**
   - 항목별로 `⇒` 앞/뒤 수치 묶음의 평균을 비교합니다. `(11레벨에서 최소)` 같은 레벨 표기는 비교에서 뺍니다.
   - 재사용 대기시간·소모·비용·시전 시간 등은 값이 낮을수록 버프이므로 부호를 뒤집습니다.
   - 챔피언 단위 라벨: 모든 항목이 + → `buff`, 모두 − → `nerf`, 섞였으면 `adjust_buff`/`adjust_nerf`, 수치가 없으면(신규 효과·리워크) `adjust`.
   - 수치로 판단할 수 없는 항목은 `direction = NaN`입니다. 정확도가 중요하면 `summary`/`context` 텍스트로 검수하세요.
4. **패치 키 매핑** (`c_patches.csv`)

| Riot 패치 | OE `patch` | Data Dragon |
|---|---|---|
| 26.01 … 26.21 | 16.01 … 16.21 | 16.1.1 … 16.21.1 |

2025 패치(`25.xx` ↔ `15.xx`) 데이터는 삭제했습니다.

| 산출물 | 단위 | 주요 컬럼 |
|---|---|---|
| `c_patches.csv` | 패치 | riot_patch, patch, ddragon_version, published, url, n_champions_changed |
| `c_patch_champion_changes.csv` | 패치 × 챔피언 × 항목 | riot_patch, patch, champion_id, name_ko, ability, stat, before, after, change_text, direction(+1/−1/0/NaN) |
| `c_patch_champion_summary.csv` | 패치 × 챔피언 | label, n_changes, n_buff_lines, n_nerf_lines, summary, context |
| `c_champion_base_stats.csv` | DDragon 버전 × 챔피언 | hp, hpperlevel, armor, spellblock, attackdamage, attackspeed, movespeed, attackrange … |

쓰는 곳: 패치 영향 예측(직전 패치 대비 변경 + 이후 픽률·승률 변화), 티어 사전정보.

### D. 챔피언 메타정보 — `d_champion_meta.py`

1. Data Dragon `versions.json`에서 2026 버전(16.x, 21개)을 고르고, 최신 16.21.1의 `champion.json`을 en_US·ko_KR로 받습니다.
2. 딜 타입: Data Dragon에는 AD/AP 구분이 없어서 **Meraki Analytics `adaptiveType`**(키 불필요)을 1순위로 씁니다. 실패하면 DDragon `info.attack`과 `info.magic`을 비교해 정합니다 (차이 ≤2면 Hybrid).
3. 조합 피처: Meraki `roles`에 VANGUARD가 있으면 `is_engage`(한타 이니시), CATCHER가 있으면 `is_catcher`(픽 이니시).
4. 이름 매핑: 영문명·한글명·DDragon id를 정규화(소문자, 기호 제거)한 별칭 → `champion_id`. 대회 표기 예외(Wukong↔MonkeyKing, Nunu & Willump, Renata Glasc …)도 넣었습니다.

| 산출물 | 단위 | 주요 컬럼 |
|---|---|---|
| `d_champions.csv` | 챔피언 | champion_id, champion_key, name_en, name_ko, tags, primary_tag, damage_type(AD/AP), meraki_roles, is_engage, is_catcher, is_ranged, attack_range, info_* |
| `d_champion_name_map.csv` | 별칭 | alias, champion_id |

이름 매핑 사용법: `norm(name)` (`d_champion_meta.norm`)으로 키를 만든 뒤 `d_champion_name_map`과 조인합니다. OE·Leaguepedia·패치노트의 챔피언 이름을 이 방식으로 통일합니다.

### F. 로스터·일정 — `f_roster_schedule.py`

1. `Tournaments`에서 2026 LCK 대회 목록을 구합니다 (`leaguepedia.lck_tournaments`, 결과는 캐시). 현재 6개:
   `LCK/2026 Season/Cup`, `Rounds 1-2`, `Road to MSI`, `Rounds 3-4`, `Season Play-In`, `Season Playoffs`
2. 로스터: `TournamentPlayers ⟕ Players (TP.Link = P.OverviewPage)` → 대회별 팀·선수·포지션, 본명·국적·생일.
   2026 시즌 중 이름이 바뀐 팀은 `team_std`로 통일합니다 (`BRION` → `HANJIN BRION`, `DRX` → `Kiwoom DRX`). 안 그러면 10개 팀이 12개로 집계됩니다.
3. 일정: `MatchSchedule` → 매치 단위(Bo3/Bo5) 일시·스코어. 2026-01-14 ~ 09-13, 미래 경기는 없습니다.
4. 팀 관점 테이블: 팀마다 시간순으로 정렬해 `next_match_kst`, `next_opponent`를 붙입니다. `--as-of 날짜`를 주면 그 시점 기준 "다음 경기 상대"(`f_next_opponents.csv`)를 만듭니다. 이것이 에이전트의 스카우팅 대상 입력입니다.

| 산출물 | 단위 | 주요 컬럼 |
|---|---|---|
| `f_rosters.csv` | 대회 × 팀 × 인원 (**코치 포함**, 선수만 쓰려면 `is_coach == False`) | OverviewPage, Team, team_std, Player, Role, is_coach, games_played(그 대회에서 그 팀으로 뛴 세트 수), NativeName, Country, Birthdate, SoloqueueIds |
| `f_players_played.csv` | 대회 × 팀 × 선수 × 포지션 | OverviewPage, Team, Player, Role, Games (`ScoreboardPlayers` 실제 출전 기록) |
| `f_match_schedule.csv` | 매치 | MatchId, OverviewPage, Tab, Round, date_kst, BestOf, Team1, Team2, Team1Score, Team2Score, winner_team, Patch, IsTiebreaker |
| `f_team_schedule.csv` | 팀 × 매치 | team, opponent, date_kst, score, opp_score, result, next_match_kst, next_opponent (팀명은 표준명) |

알려진 빈 곳: Rounds 3-4의 Gen.G vs Dplus Kia 1세트는 `ScoreboardPlayers`에 선수 기록이 없습니다 (출전 기록 5,020행, 세트 기준 기대값 5,030행). 그래서 이 세트의 `games_played`는 집계되지 않습니다. 경기 일정과 스코어는 정상입니다.

---

## 4. 소스 간 조인 키

```
          ┌──────────── champion_id (d_champion_name_map, norm(name)) ───────────┐
          │                                                                       │
A a_*_games ── patch(16.xx) ── C c_patches ── ddragon_version ── C base_stats    D d_champions
     │
     │  (date, {팀1,팀2}, 세트 번호)  또는  gameid ↔ RiotPlatformGameId
     │
B b_draft_games ── MatchId ── F f_match_schedule
     │                          │
     └── team ──────────────── F f_rosters (OverviewPage, Team)
```

- **A ↔ B**: OE `gameid` = Leaguepedia `RiotPlatformGameId`로 502경기 모두 그대로 조인됩니다 (`LOLTMNT03_337058` 형식). B의 팀명은 표준명이라 A의 `teamname`과 같습니다. F의 `f_match_schedule`, `f_rosters.Team`은 원래 이름이고, `f_team_schedule`과 `team_std`가 표준명입니다.
- **B ↔ F**: `MatchId`가 공통입니다.
- **패치**: OE와 G는 `16.xx`, Leaguepedia `Patch`는 Riot 표기(`26.06`)입니다. `c_patches.csv`의 `riot_patch` ↔ `patch`로 맞춥니다. F에 나온 2026 대회 패치는 12개입니다: 26.01~03, 26.06~11, 26.14~16.

## 5. 알려진 이슈 / 주의

- **Oracle's Elixir Drive 한도**: 인기 공개 파일은 익명 다운로드가 일시 차단됩니다 (`Too many users have viewed or downloaded this file recently`). 2026 파일도 2026-11-01에 차단 상태였습니다. 보통 수 시간~24시간 안에 풀립니다. 2026 CSV는 약 79MB입니다. 우회 방법:
  1. Google에 로그인한 브라우저에서 파일을 직접 받기
  2. 폴더에서 해당 CSV를 "사본 만들기"로 내 드라이브에 복사한 뒤 그 사본을 받기
  3. 받은 파일을 `data/inven_2026/games/`에 두거나 `--file`로 지정하고 `a_oracles_elixir.py`를 다시 실행
- **Leaguepedia 레이트리밋**: 익명 Cargo API는 한도가 낮습니다. 클라이언트가 요청 간격 4초를 두고, `ratelimited` 응답이 오면 30s → 60s → … 최대 15분까지 기다렸다 재시도합니다. 성공한 쿼리는 캐시되니 중간에 끊겨도 재실행하면 이어서 진행됩니다.
  - **로그인으로 한도 늘리기 (선택)**: Fandom 계정으로 `https://lol.fandom.com/wiki/Special:BotPasswords`에서 봇 비밀번호를 만듭니다. 권한은 "High-volume (bot) access"와 "Basic rights"를 체크합니다. 그다음 `.env`에 `LP_USER=<계정>@<봇이름>`, `LP_PASS=<발급된 비밀번호>`를 넣으면 클라이언트가 자동으로 로그인합니다.
- **버프/너프 라벨은 휴리스틱**입니다. 비율(%)과 기본값이 반대로 움직이거나 메커니즘이 바뀐 경우는 `adjust*`로 분류됩니다.
- **딜 타입**: Meraki는 Data Dragon 외 커뮤니티 소스입니다(키 불필요). 하이브리드 챔피언(예: 카이사·코르키)은 주 딜 타입 하나로만 표기됩니다.
- **2026 시즌 초 사전정보**: 2025 결과는 삭제했으므로 시즌 초(LCK Cup) 티어 사전정보는 없습니다.

## 6. G. 솔로랭크 (Riot Games API, KR) — `g_riot_soloq.py`

**키 설정**: developer.riotgames.com에서 받은 키를 프로젝트 루트 `.env`에 넣습니다 (`.env.example` 참고, `.env`는 `.gitignore`에 포함). 환경변수 `RIOT_API_KEY`로 줘도 됩니다.

```
RIOT_API_KEY=RGAPI-...
```

1. **apex** (`league-v4`, platform `kr`): 챌린저·그랜드마스터(옵션으로 마스터) 솔랭 래더를 받습니다. **조회 시점 스냅샷**(2026-10-31)이며, 과거 시점 래더는 API로 받을 수 없습니다.
2. **계정 후보** (`g_accounts_ttp.py`): trackingthepros.com 선수 페이지에서 KR 계정을 가져옵니다. 페이지 설명문의 한글 본명을 F의 `NativeName`과 대조해 동명이인을 거르고, 자동 대조가 안 되는 선수는 `MANUAL_VERIFIED`에 사람이 확인해 넣었습니다. Leaguepedia `SoloqueueIds`는 옛 이름이 많아 이 단계로 보완합니다.
3. **pros** (`account-v1`, region `asia`): F의 `f_rosters.csv`에서 **2026 LCK에 실제 출전한 선수(`games_played > 0`, 코치 제외)**만 골라, Leaguepedia와 trackingthepros 계정 199개를 Riot ID → PUUID로 바꿉니다. 167개가 조회됐고 고유 PUUID는 164개입니다. 태그가 없는 옛 소환사명은 `#KR1`로 시도하고, 못 찾으면 `puuid` 빈칸으로 남깁니다.
4. **matches** (`match-v5`, region `asia`): (LCK 선수 PUUID + 래더 상위 `--apex-top`명)의 **2026-01-01 ~ 실행 시점 KST, queue 420(솔로랭크)** 경기 id를 월별로 최대 100개씩 받고, 그중 **PUUID·월당 `--per-month`개(기본 30)**를 고르게 뽑습니다 (`pick`). 중복을 제거한 뒤 경기 상세를 받습니다.
   - 월별로 나누는 이유: API는 최신 경기부터 주기 때문에 계정당 N개로 자르면 최근 달에 쏠립니다 (2025에서 계정당 50개로 잘랐을 때 1,199경기 중 579경기가 12월).
   - 응답 헤더 `X-App-Rate-Limit`을 기준으로 호스트별 슬라이딩 윈도우로 속도를 조절하고, 429가 오면 `Retry-After`만큼 기다립니다.
   - 경기 원본은 `data/raw/riot/matches/*.json.gz`에, id 목록은 `data/raw/riot/matchids/<YEAR>/`에 캐시해서, 중단돼도 재실행하면 이어서 받습니다.
   - 개발 키 한도는 실측으로 앱 100건/2분 + 20건/초(라우팅 호스트별), 메서드 2,000건/10초입니다. 경기 상세는 사실상 3,000건/시간입니다.
   - 이번 수집 실적: 대상 188개 계정, 월별 목록 조회 약 24분, 경기 상세 18,277건 약 6시간 10분, 합계 약 6시간 35분. 429는 0건이었습니다.
5. **account check**: 경기의 `teamPosition`을 선수 포지션과 비교합니다. 10경기 이상인 계정에서 일치율이 40% 미만이면 `suspect`로 표시하고 해당 계정의 `pro_player` 라벨을 지웁니다.

| 산출물 | 단위 | 주요 컬럼 |
|---|---|---|
| `g_kr_apex_ladder.csv` | 래더 선수 | snapshot_kst, ladder_rank, tier, leaguePoints, wins, losses, puuid |
| `g_ttp_accounts.csv` | 선수 × 계정 (trackingthepros) | player, team, role, region, riot_id, rank_now, inactive, native_verified |
| `g_pro_accounts.csv` | 선수 × 계정 | player, team, role, riot_id, source, puuid, riot_id_now |
| `g_pro_account_check.csv` | 선수 × 계정 | player, role, puuid, riot_ids, n_matches, role_share, main_position, win_rate, suspect |
| `g_soloq_participants.csv` | 경기 × 참가자 | matchId, game_start_kst, patch(16.xx), is_remake, puuid, riot_id, pro_player, side, teamPosition, champion_id, win, K/D/A, kda, goldEarned, cs, cspm, damageshare, visionScore |
| `g_soloq_bans.csv` | 경기 × 밴 | matchId, patch, side, pickTurn, champion_id |

- `champion_id`는 `championId`(숫자)를 D의 `champion_key`로 매핑한 Data Dragon id입니다. 그래서 C·D와 바로 조인됩니다. A·B의 챔피언 이름은 `norm()` + `d_champion_name_map`으로 맞추면 전부 매핑됩니다.
- `patch`는 `gameVersion`의 앞 두 자리를 OE 형식(`16.04`)으로 맞춘 값입니다. **문자열로 읽으세요** (`pd.read_csv(..., dtype={"patch": str})`). 그냥 읽으면 `16.10`이 숫자 `16.1`이 됩니다. `c_*.csv`의 `patch`도 같습니다.
- 2026년 1월 1~8일 경기에는 패치 `15.24`(126경기)가 포함됩니다. 2026 `c_patches.csv`에는 없는 패치라 패치 조인 시 빠집니다.
- 다시하기 경기 151개(0.83%)가 들어 있습니다. 통계를 낼 때는 `is_remake == False`로 거르세요. 승자가 0명인 2경기도 모두 여기에 포함됩니다.
- 목록에는 있었지만 Riot이 참가자 없이 돌려준 경기 7개(`queueId` 0)는 결과에서 뺐습니다.

**수집 결과 검증** (2026-11-01): 대상 경기 18,277개 중 유효 18,270개를 모두 반영했습니다 (누락은 위 7개뿐). 전 경기 참가자 10명, 경기 내 참가자 중복 0건, 한 팀 안에서 승패가 엇갈린 경기 0건, 다시하기를 뺀 전 경기 승자 5명, `queueId` 전부 420, 챔피언 id 미매핑 0건. 날짜는 2026-01-01 ~ 10-31, 패치 16.01~16.21 모두 존재하고 패치별 기간이 순서대로 이어집니다. 월별 1,250~2,136경기입니다.

**한계**
- 선수 연결: 출전 선수 67명 중 65명이 경기와 연결됩니다. FenRir (Park Kang-jun), Painter는 계정을 찾지 못했습니다. 다시하기를 뺀 선수별 경기 수는 중앙값 400경기이고, Jiwoo 36, Enosh 41, Pleata 187경기로 낮은 쪽과 ShowMaker 743경기로 높은 쪽의 차이가 큽니다.
- 선수별 경기 수는 `--per-month` 상한과 다릅니다. 다른 대상 계정의 경기에 같이 잡힌 판도 포함되고, 계정이 여러 개면 합산되기 때문입니다.
- **목록 잘림**: `by-puuid/ids`가 오래된 경기를 일부 계정에서 비우거나 잘라서 줍니다. 경기 상세는 남아 있으므로 삭제가 아니라 목록 조회만 막힌 것입니다. 2026 기준 경기 50건 이상인 89개 계정 중 28개가 잘렸고 (Faker 466/819, 가장 오래된 경기 2026-04-27), 잘리는 달은 계정마다 2~8월로 다릅니다. 원인은 확인하지 못했습니다. 그래서 시즌 초 표본이 적습니다 (1월 1,250경기, 7월 2,136경기). Faker도 1~3월은 25경기뿐입니다. 시즌 초 선수 숙련도는 낮게 나올 수 있습니다. 2025는 훨씬 심합니다.
- 계정 164개 중 63개는 2026 목록이 0건입니다. 안 쓰는 계정과 목록이 막힌 계정을 구분할 수 없습니다.
- Riot ID는 재사용될 수 있어 `pro_player`가 본인임을 보장하지 않습니다. 포지션 검사로 4개 계정, 330경기의 라벨을 지웠습니다 (`T1 Cuzz#KR1`, `궁극의 고스트#KR1`, `태 윤#KR1`, Clozer의 `클로짝#0727`). 포지션 검사는 근사치입니다. 다른 라인 연습이 많은 본인 계정도 걸릴 수 있습니다.
- 10경기 미만인 계정 9개(23경기)는 검사를 건너뛰어 라벨이 남아 있습니다. 그중 6개(11경기)는 포지션이 맞지 않습니다 (예: Ruler `귀찮게하지마#KR3`, Keria `역천괴#ker3`). 분석에 주는 영향은 작습니다.
- `DWG KIA#KR1`은 deokdam과 ShowMaker 양쪽에 등록돼 있고 trackingthepros 쪽을 따라 ShowMaker로 매핑됩니다.
- 표본은 LCK 선수 계정 위주이며 KR 고티어 전체를 대표하지 않습니다. 18,270경기 중 2,630경기만 래더 상위 계정 전용으로 잡힌 경기입니다. 래더 상위 30명 중 6명은 LCK 선수 계정입니다.
- 골드·CS 차이 @10/15가 필요하면 경기마다 `timeline` 엔드포인트를 한 번씩 더 호출해야 합니다(호출 수 2배). 현재는 넣지 않았습니다.
- 개발 키는 **24시간마다 만료**됩니다. 401/403이 나오면 키를 재발급해 `.env`를 갱신하세요. 오래 돌릴 거면 Personal API Key를 신청합니다.

### 6-2. 리서치 대상 솔랭 전체 — `g_soloq_research.py` (2026-11-04 추가)

`g_riot_soloq.py`는 계정·월당 30경기 표본이라 챔피언별 판수가 실제보다 적게 잡힙니다. 이 스크립트는 사용자가 정리한 계정 목록 `data/raw/riot/soloq_range.csv`(82명·264계정, 2024~2026 목록 범위 포함)에서 **리서치 대상 58명**(`research/config/players.yaml`)의 계정만 골라, **리서치 기간(2026-01-14 ~ 08-25, `tournament_patches.csv`의 리서치 창)** 솔랭을 **표본 없이 전부** 받습니다.

```
python g_soloq_research.py --steps plan   # 계정 대조만 (API 호출 없음)
python g_soloq_research.py                # accounts → ids → matches (중단돼도 캐시로 이어감)
```

- 키는 `.env`의 `RIOT_API_KEY` 하나로 전체 실행 (선수별 키 아님). 개발 키는 24시간마다 재발급
- 계정: `status == ok`이고 `listed > 0`인 105개 (58명 전원). `DWG KIA#KR1`은 ShowMaker·deokdam 양쪽에 있어 선수 라벨을 붙이지 않음
- Riot ID → PUUID 캐시(`data/raw/riot/accounts.json`)와 경기 상세 캐시(`data/raw/riot/matches/`)는 `g_riot_soloq.py`와 공용 → 이미 받은 경기는 다시 안 받음. 경기 id 목록은 `matchids_research/`에 따로 (월별 + 100개씩 페이지 넘김, 상한 없음)
- 비시즌·MSI 기간 경기도 받되 `research_patch`(26.xx 창 이름)를 비워 둠 → 분석 때 넣을지 결정
- 포지션 일치율 40% 미만(10경기 이상) 계정은 `suspect_account = True`, 챔피언 집계에서 뺌
- 출력 (`data/processed/2026/`): `g_research_accounts.csv`, `g_research_account_check.csv`, `g_research_soloq.csv`(선수 본인 경기만), `g_research_soloq_champs.csv`(선수 × 챔피언: 판수·승률·KDA·비중·CSPM·딜 비중)
- 한계: 목록 잘림(위 한계 참고)은 같은 API라 그대로일 수 있음 — 계정 목록의 `oldest`가 2026년 중간인 계정(Faker 04-27, FenRir 07-18 등)은 그 전 경기가 안 나올 수 있음

### 6-3. 리서치 대상 솔랭 상세 — `g_soloq_detail.py` (2026-11-04 추가)

6-2가 받은 경기 캐시에서 뽑을 수 있는 항목을 전부 뽑고, 타임라인(10·15분)을 더합니다. 자세한 항목은 파일 첫머리 설명.

```
python g_soloq_detail.py --steps build              # 캐시만 읽음 (키 불필요)
python g_soloq_detail.py --steps timeline,build     # 본인 경기마다 timeline 1번 (시간당 약 3,000)
```

- 경기 1행 326칸 안팎: K/D/A·멀티킬·솔로킬·킬 관여·분당 딜·딜/골드 비중·CS·시야·오브젝트, 최종 아이템·주문·룬(한국어 이름, Data Dragon 16.16.1), 맞라인 상대 챔피언·LCK 선수 여부·경기 끝 골드/CS/딜/경험치 차이, 양 팀 5명 챔피언·밴, Riot `challenges` 항목(`ch_*`, 아레나·칼바람용 제외)
- 타임라인: 10·15분 골드·경험치·CS·레벨과 맞라인 차이, 10·15분까지 K/D/A, 스킬 선마 순서, 시작 아이템, 완성 아이템 순서·시각(3개). 원본은 크므로 필요한 것만 `data/raw/riot/timeline_lite/`에 저장
- 집계: `g_research_soloq_champs.csv`(선수×챔피언), `_players.csv`(선수), `_patch.csv`(선수×리서치 패치 창), `_matchups.csv`(선수×챔피언×상대 챔피언, 본 포지션 판만), `_vs_pros.csv`(맞라인 LCK 선수별)
- `on_role` = 선수 본 포지션으로 한 판. 챔피언 집계에는 다른 라인 판도 들어가므로 `on_role_share`를 같이 볼 것 (예: 정글러의 이즈리얼)
- 주의: 6-2 `g_soloq_research.py`가 끝날 때 `g_research_soloq_champs.csv`를 단순 형식으로 씀 → 그 뒤에 이 스크립트 `build`를 다시 돌릴 것

기타 솔랭 소스 (참고):

| 소스 | 내용 | 비고 |
|---|---|---|
| trackingthepros.com 등 | 프로 선수 ↔ 솔랭 계정 | Leaguepedia `SoloqueueIds`가 비었거나 오래됐을 때 보완용 |
| OP.GG, U.GG, Lolalytics 등 | 패치별 챔피언 승률·픽률 집계 | 공개 API가 없고 크롤링은 대부분 약관 위반이라 참고용 |

---

#### 6-3-1. 오래 걸리는 타임라인 받기 — 끊겨도 이어받기 (2026-11-04)

- `powershell -ExecutionPolicy Bypass -File src\pipeline
un_soloq_detail.ps1` → Claude 앱·터미널과 상관없이 숨은 창에서 `g_soloq_detail.py --steps timeline,build` 를 돌림. 이미 돌고 있으면 새로 띄우지 않음
- 경기마다 `data/raw/riot/timeline_lite/{경기}.json.gz` 를 따로 저장하고(임시 파일에 다 쓴 뒤 이름 바꿈), 다시 실행하면 **받은 경기는 건너뜀**. PC가 꺼지거나 절전에 들어가면 멈추므로, 다시 켠 뒤 같은 명령을 다시 실행
- 진행: `data/logs/g_soloq_detail.log` 마지막 줄. 개발 키는 24시간마다 만료 → 401/403이 나오면 키를 새로 받아 `.env` 에 넣고 다시 실행
- **타임라인 형식 v2**: 10·15·20·25분 프레임(10명 전원) + 모든 킬 이벤트 `[초, 킬러, 희생자, 어시스트, x, y, {피해 준 참가자: [물리, 마법, 고정]}, 피해 건수]`. 챔피언 속성표의 캐치·폭딜·성장 곡선 보강용. 옛 형식(v1) 파일은 자동으로 다시 받음

### 6-4. 챔피언 속성표 — `h_champion_attributes.py` (2026-11-04 추가, 1차본)

밴픽 유동층(7장)의 딜 밸런스·앞라인·이니시 판단에 쓰는 **패치 × 챔피언 × 포지션** 표입니다. 태그(AD/AP, 탱커)로 정하지 않고 **솔랭 경기 기록 값**으로 정하며, 챔피언 자체 수치는 보조입니다.

```bash
python h_champion_attributes.py          # 경기 원본 전체를 다시 읽음 (약 4분)
python h_champion_attributes.py --reuse  # 참가자 표를 재사용, 속성표·평가만
```

- 자료: `data/raw/riot/matches/*.json.gz` (G1·G2 캐시, 10명 전원). **01-14 ~ 08-25 경기 전부** (비시즌·MSI 기간 솔랭 포함, 08-26 이후 제외), 5분 미만·조기 항복 제외. **패치 이름표 = 경기의 실제 버전 `gameVersion`** (16.15 → 26.15). 날짜 창 이름은 `date_window` 칸에 참고로 남김
  - 2026-11-05 변경: 솔랭은 대회보다 한 패치 앞서 가서, 날짜 창으로 붙이면 LCK 패치와 어긋났음 (날짜 창 26.14 솔랭 744경기 중 742경기가 실제 16.15, 진짜 16.14 솔랭은 MSI 기간이라 빠져 있었음). 자르는 기준은 여전히 날짜(08-25), 패치는 실제 버전
- 자체 수치: **Data Dragon 한국어 `championFull`** (리서치 12개 패치, `data/raw/ddragon/{버전}/championFull_ko_KR.json`). 툴팁의 `<status>` 태그로 단단한 CC·약한 CC, 문구로 광역 여부·돌진기를 셈. Meraki 는 25.15 이후 갱신이 없고 로크·자헨이 없어 **역할 이름표와 attributeRatings 만** (평가·보조용). 나무위키는 robots 상 `/w/` 허용이지만 스킬 설명이 자유 서술이라 쓰지 않음
- 값 처리: 분당 값·팀 내 몫 → 판수가 30보다 적으면 (패치별 → 전체 패치, 전체 패치 → 포지션 평균) 쪽으로 당김 → 같은 (패치, 포지션) 안 백분위. `reliable` = 판수 30 이상

| 축 | 칸 | 계산 |
|---|---|---|
| ① 딜 구성 | `phys/magic/true_share`, `damage_label` | 챔피언에게 준 물리·마법·고정 피해 비중. 60% 이상이면 AD/AP, 아니면 Hybrid |
| ② 앞라인 | `ax_frontline` | 팀 받은 피해 몫 + 팀 막은 피해 몫 + 데스당 받은 피해 (백분위 평균) |
| ③ 이니시·CC | `ax_engage` | 적을 묶은 횟수/분 + CC 시간/분 + 묶고 같이 잡은 킬/분 |
| ④ 캐치 | `ax_catch` | 단일 대상 단단한 CC 스킬 수(툴팁) + 묶은 횟수/분. `ch_pickKillWithAlly` 는 CATCHER 를 못 가려내 제외 |
| ⑤ 보호 | `ax_protect` | 아군 회복·보호막/분 + 유효 회복·보호막/분 |
| ⑥ 성장 곡선 | ~~`ax_scaling`~~ → ⑪ `ax_growth` | **2026-11-05 폐기**: 늦게·일찍 끝난 판 승률 차는 인접 패치 상관 −0.10~0.38 로 불안정. `scaling` 값만 참고로 남기고 축은 ⑪ 타임라인 성장으로 교체 |
| (딜 비중) | `ax_carry` | 팀 딜 중 몫 |
| ⑦ 암살·다이브 | `ax_dive` | 수적 열세 킬 + 적 포탑 근처 킬 + 멀티킬 + 솔로킬 (/분) + 돌진기 수(툴팁) |
| ⑧ 사이드·스플릿 | `ax_split` | 후반 혼자 민 포탑 + 건물 피해 + 포탑 방패 (/분) + 킬 중 솔로킬 비율 |
| ⑨ 라인전 | `ax_lane` | 라인전 단계 골드·경험치 우위 + 최대 CS 우위. **탑·미드·서폿만** (원딜·정글은 대회 골드 차이와 안 맞아 비움) |
| ⑩ 포킹 | `ax_poke` | 기본 공격 사거리 + 스킬샷 적중/분 |
| ⑪ 시간대별 (타임라인) | `tl_gold_diff{10,15,20,25}`, `tl_dmg_share{…}`, `ax_growth`, `ax_dmg_late` | 맞라인 골드 차이, 팀 챔피언 딜 중 몫(그 분까지 누적). 성장 = 25분 − 10분 골드 차이, 후반 딜 = 25분 딜 몫 − 10분 딜 몫. 25분까지 간 판 30 이상만 |
| ⑫ 순간 딜 (타임라인) | `burst_per_hit`, `ax_burst` | 킬러가 희생자 피해의 70% 이상을 준 킬에서 (킬러 피해 ÷ 희생자가 받은 피해 건수)의 중앙값. 큰 한 방이 많을수록 높음 → 높으면 순간 딜, 낮으면 지속 딜. 킬 30건 이상만 |

- ⑪·⑫는 타임라인(리서치 선수 58명이 있었던 판의 10명 전원)이 쌓여야 값이 생김. 전체 기간 값 하나를 패치 행에도 씀 (`h_champion_timeline.csv` 따로 저장)

| 산출물 | 단위 |
|---|---|
| `h_attr_participants.csv.gz` | 경기 × 참가자 (속성 계산용 칸, puuid 없음) |
| `h_champion_static.csv` | 패치(26.xx·ALL) × 챔피언: 툴팁 CC·광역·돌진기, 기본 스탯, Meraki 이름표 |
| `h_champion_attributes.csv` | 패치(26.xx·ALL) × 포지션 × 챔피언 |
| `h_champion_attributes_eval.md` | 평가 |

1차 평가 (2026-11-04, 솔랭 16,899경기 · 수집 도중 캐시):
- 타당성 (Meraki 이름표 AUC): 앞라인↔TANK 0.90, 이니시↔VANGUARD 0.87 (정글·서폿·탑 0.90), 보호↔ENCHANTER(서폿) 0.99, 캐치↔CATCHER(서폿) 0.68 (약함), 딜 비중↔MARKSMAN 0.73
- 딜 타입: 기존 `d_champions.damage_type`(Meraki adaptiveType)과 96.6% 일치. 어긋난 쪽은 실제 값이 맞는 경우가 대부분 (쓰레쉬·레오나 = AP, 카이사 = AD, 잭스 = Hybrid) → **밴픽에서는 이 표의 실제 비중을 씀**
- 안정성: 반으로 나눈 경기끼리 0.89~0.999, 인접 패치끼리 0.86~1.00. 성장 곡선만 −0.09~0.43
- 대회 일치 (OE, 같은 포지션 안): 받은 피해/분 0.91~0.95, 막은 피해/분 0.92~0.98, 딜 비중 0.62~0.88
- 한계: 툴팁 문구로 광역을 다 못 잡음 (말파이트·아무무 궁극기 등), 툴팁 CC 수 ↔ 관측 이니시 상관 0.64. 성장 곡선은 이기는 팀이 일찍 끝내는 효과가 섞임 → OE 시간대별 골드 차이로 대조 예정
- **수집 완료 후 재생성 (2026-11-04 16:03, 18,219경기)**: 1차본과 같은 행끼리 비교하면 딜 비중·앞라인·이니시·캐치·보호는 차이 중앙값 0.1~0.6점(백분위)이고 15점 넘게 바뀐 행이 거의 없음 (판수 30 이상 기준). 성장 곡선만 전체 기간 행 35개·패치 행 84개가 15점 넘게 바뀜 → 성장 곡선은 아직 쓰지 않는 것이 안전. 평가 수치는 1차와 거의 같음 (앞라인 AUC 0.895, 이니시 0.871, 캐치 0.681, 보호 0.987, 딜 타입 일치 96.3%)
- **2차 축 추가 (2026-11-04 16:22)**: 암살·다이브↔ASSASSIN(미드·정글) AUC 0.91, ↔DIVER(탑·정글) 0.67 → 이 축은 "킬 결정력"에 가까움. 사이드·스플릿↔JUGGERNAUT(탑) 0.74. 포킹↔ARTILLERY 0.73 (사거리가 긴 원딜형 미드가 섞임). 라인전↔OE 15분 골드 차이(같은 포지션) 탑 0.84·미드 0.54·서폿 0.63. ⑪·⑫는 타임라인 107경기뿐이라 아직 값 없음 → 타임라인이 끝나면 `python h_champion_attributes.py --reuse` (경기 원본은 다시 안 읽어도 됨)
- **타임라인 반영 재생성 (2026-11-05 10:23)**: 타임라인 v3 수집 완료(11-05 03:25) 후 `--reuse`. 리서치 창 안 타임라인 16,556경기(전체 18,219경기의 91%), 판수 30 이상 전체 기간 행 345개 중 성장·후반 딜 287개, 순간 딜 325개 채움. 1장 AUC·딜 타입은 변화 없음
  - 안정성 (경기를 반으로 나눔, 평가 2-3): 성장 0.78, 후반 딜 몫 0.96 (포지션 안 0.82), 15분 골드 차이 0.84, 순간 딜 0.94 → 옛 승률 차 방식(인접 패치 −0.10~0.38)보다 훨씬 안정
  - 대회 일치 (성장 ↔ OE 25분 − 10분 골드 차이, 같은 포지션 안): 탑 0.60, 미드 0.41, 정글 0.39, 서폿 0.40, **원딜 0.02** → 원딜 성장 축은 대회와 안 맞음 (원딜은 솔랭·대회 모두 후반 챔피언이 많아 포지션 안 차이가 작고, 대회는 판수가 적음). 원딜 성장 축은 참고만
  - 해석 주의: 성장 = "10분 이후 25분까지 맞라인 상대보다 더 번 골드"라서 **후반 캐리력보다 중반 스노볼(골드 벌이)에 가까움**. 상위: 탑 제드·트페·케일, 정글 그웬·키아나·탈론, 원딜 드레이븐·카이사·제리. 하위는 탱커·유틸(렐·오른·마오카이·세주아니). 후반 캐리는 `ax_dmg_late`(딜 몫 증가)를 같이 볼 것
  - **구간 딜 축 추가 (2026-11-05 10:52)**: 누적 딜 몫은 변화가 무뎌서, 그 구간에 팀이 넣은 딜 중 몫으로 다시 계산
    - `ax_late_seg` = 20–25분 딜 몫 − 0–10분 딜 몫 (25분까지 간 판, 287행)
    - `ax_late_end` = 25분–끝 딜 몫 − 0–10분 딜 몫 (30분 이상 판만, 경기 원본의 최종 딜·골드 사용 → 다시 받지 않음, 209행)
    - `ax_gold_eff` = log(25분–끝 골드당 딜 ÷ 0–10분 골드당 딜) (209행). `goldEarned`는 시작 골드 포함 여부가 프레임 `totalGold`와 다를 수 있어 참고용
    - 안정성(반분, 포지션 안): late_seg 0.86, late_end 0.86, gold_eff 0.68
    - 원딜 `ax_late_end` 상위 스몰더·제리·직스·카이사·시비르·징크스·아펠리오스, 하위 드레이븐·바루스·루시안·애쉬·진·미스 포츈 → 흔한 "후반/라인전 원딜" 구분과 가장 잘 맞음 (아펠리오스가 누적 방식 26 → 74). 미드 상위 아우렐리온 솔·블라디미르·카사딘·제라스·아지르, 하위 이렐리아·르블랑·나피리
    - **원딜·미드·탑의 후반 캐리 축 = `ax_late_end`** (판수가 모자라면 `ax_late_seg`). 서폿은 탱커(알리스타·블리츠·렐)가 높게 나와 "후반 캐리"로 읽으면 안 됨 (초반 딜 몫이 작아서 차이가 커짐). `ax_growth`는 "중반 스노볼"로 따로 씀. `ax_gold_eff`는 포지션 안 안정성이 낮아 참고만
  - **30분 이후 프레임 + 사거리·궁극기 (2026-11-05 12:31, 경기 원본부터 다시 생성)**
    - 30분 이후 프레임: `g_soloq_detail.py --steps late` (또는 `run_soloq_detail.ps1 late`) → `data/raw/riot/timeline_late/` 4,333경기 (30분 이상 판, 25분~끝 1분마다 10명 골드·경험치·CS·딜·받은 피해). 마지막 프레임 = 경기 원본 최종 딜·골드와 일치 확인
    - 킬 피해 스킬별 (2026-11-05 시작, 궁 축 보강용): `run_soloq_detail.ps1 killdmg` → `data/raw/riot/timeline_killdmg/` (속성표 경기 전부 = 2026-11-05 창 밖 포함 후 27,230, 킬마다 피해 준 pid·spellSlot(3 = R)·기본 공격·물리/마법/고정·spellName·type). 약 9시간 (창 밖 경기의 30분 이후 프레임 2,175개를 먼저 받으므로 `run_soloq_detail.ps1 "late,killdmg"`). **PC를 껐다 켜도 같은 명령을 다시 실행하면 받은 경기는 건너뛰고 이어받음** (다 쓴 뒤 이름을 바꾸는 방식이라 반쯤 쓴 파일이 남지 않음). 키 만료(401·403)면 멈추므로 `.env` 키 갱신 후 같은 명령
    - `ax_late_30_35` (30–35분 딜 몫 − 0–10분, 35분 이상 판): 반분 안정성 포지션 안 0.79, 포지션당 24~31행만 채워짐. 원딜 순서는 `ax_late_end` 와 거의 같음 (스몰더·제리·징크스·카이사 상위, 바루스·루시안·진 하위) → **후반 축은 `ax_late_end` 가 주, 30–35분은 확인용**
    - 사거리: `attackrange`, `melee`(250 이하, 90/173명), `range_qwe_max`·`range_qwe_med`(Q·W·E 툴팁 사거리 중 100~3000만), `range_r`, `ax_range`(기본 공격 + 스킬 중간 사거리 백분위). ↔ MARKSMAN·ARTILLERY AUC 0.76, ↔ ARTILLERY(미드·서폿) 0.84. 흐웨이·유미는 스킬 사거리 표기가 없어 비어 있음. 카운터(사거리 차이)·조합(팀 평균)에는 원래 값 `range_*` 를 씀
    - 궁극기: `r_cd1`·`r_cd3`(툴팁), `r_global`(사거리 3000 이상 + 25000 표기 중 애쉬·이즈리얼·징크스·세나·소라카, 헤카림 제외, 28명), `r_casts_pm`(분당 궁 사용, 반분 0.99), `one_spell_mk_pm`(0.76), `knock_kill_pm`(0.78). 궁 재사용 대기 ↔ 분당 사용 −0.35 (방향 맞음)
    - **`ax_ult_fight`(한타형 궁) 는 약함**: ↔ VANGUARD AUC 0.69. 툴팁 광역 CC 판정이 말파이트·아무무 궁을 놓치고, 관측값 2개가 궁 전용이 아님(아무 스킬 / 쓰레쉬·알리스타의 일반 CC 가 섞임). 쉔·갈리오의 합류 가치도 못 잡음 → 시너지에 쓰기 전 보강 필요 (궁이 킬 딜에서 차지하는 몫 = 킬 이벤트 스킬별 피해, 타임라인 다시 받아야 함)
    - **`ax_ult_uptime` 은 변신 챔피언(니달리·엘리스·제이스·우디르·아이번)이 R 을 일반 스킬처럼 써서 위로 몰림** → 그 챔피언들은 빼고 읽을 것
  - 순간 딜 상위: 탑·정글 올라프·나서스·가렌·카직스, 미드 베이가·블라디미르·카사딘, 서폿 판테온·파이크. 하위는 유틸·지속 딜(룰루·밀리오·소라카·세라핀·신지드). 올라프·나서스처럼 "한 대가 큰 근접 챔피언"도 높게 나옴 (킬 하나를 거의 혼자 낸 경우만 셈)

## 7. 밴픽 추천 시스템 설계 (2026-11-04, 설계만 · 코드 없음)

가정: 우리는 **T1 전력분석관**. 다음 상대(`f_next_opponents.csv`)를 상대로 밴·픽을 추천하는 것이 최종 목적입니다. 이 장은 지금까지 만든 자료가 어디에 들어가는지와 남은 설계를 정리한 것이며, 아직 구현한 코드는 없습니다.

### 7-1. 재료 6개와 담당

| # | 재료 | 내용 | 담당 · 상태 | 자료 위치 |
|---|---|---|---|---|
| 1 | **메타** (챔피언 + 아이템) | 패치별로 어떤 챔피언·아이템이 많이 나왔나 (자연어 처리) | 팀원 · 완료 | 팀원 산출물 (형식 확인 필요, 7-6) + C 패치노트·A 픽률 |
| 2 | **픽 선호도** | 선수가 자주·잘 하는 챔피언, 고집하는 픽 | 우리 · 완료 | 프로필 2장, `research/profiles/stats/`, G `g_research_soloq_champs.csv` |
| 3 | **선수 플레이 스타일** | 라인전·합류·교전 성향, 약점 | 우리 · 완료 | `research/profiles/{선수}.md` 1·4장 (58명) |
| 4 | **팀 스타일** | 밴픽 성향, 운영(후반/교전 지향 등), 자주 쓰는 조합 | 우리 · 완료 | `research/profiles/{팀}.md` 1~3장 (10팀), `research/profiles/team_stats/` |
| 5 | **카운터** | 상대가 X를 하면 Y로 받아친다 (예: 상대 크산테 → 베인) | 알고리즘화 · 예정 | A `a_player_games`, B `b_draft_actions`, G 솔랭 |
| 6 | **조합** | 팀 스타일과 맞는 조합 성향(후반·교전 등) + 자주 세트로 나오는 조합(예: 애라핀, 룰루+유나라) | 알고리즘화 · 예정 | B 픽 구성, D `d_champions`(딜 타입·`is_engage`·`is_catcher`), 팀 프로필 3장 |

1~4는 **드래프트 전에 정해지는 정보**, 5~6은 **드래프트가 진행되면서 상대 밴·픽에 따라 값이 바뀌는 정보**라서 두 층으로 나눕니다.

### 7-2. 두 층 구조

```
[고정층]  드래프트 전 · 패치별·팀별 티어리스트
   메타(1) × 픽 선호도(2) × 선수 스타일(3) × 팀 스타일(4)
   → (패치, 팀, 포지션, 챔피언) 마다 점수·티어
             │
             ▼
[유동층]  드래프트 중 · 지금까지 나온 밴·픽을 보고 점수 보정  (학습)
   + 카운터(5): 상대가 이미 고른 챔피언에 대한 상성
   + 조합(6):   우리 팀에 이미 고른 챔피언과의 시너지, 팀 스타일 적합도
   − 제외:      이번 매치에서 이미 쓴 챔피언 (피어리스), 이미 밴·픽된 챔피언
             │
             ▼
[출력]  다음 step(1~20)에 대한 추천
   - 우리 밴: 상대 팀 티어 상위 + 상대 조합을 완성시키는 챔피언
   - 우리 픽: 우리 팀(T1) 티어 + 카운터·조합 보정
   - 상대 예측: 상대 팀 티어 + 상대 기준 카운터·조합 보정 → 다음 상대 픽·밴 확률
```

#### 고정층 — 패치별·팀별 고정 티어리스트
- **단위**: 패치(대회 패치 창, `research/config/tournament_patches.csv`) × 팀 × 포지션 × 챔피언.
- T1용(우리 픽 후보)과 상대 팀용(밴 후보·상대 예측) 둘 다 만듭니다. 상대 팀 티어가 곧 "이 팀이 이 패치에 꺼낼 챔피언" 예상입니다.
- 들어가는 신호
  - 메타: 그 패치 대회·솔랭 픽률·밴률, 팀원의 패치별 등장 챔피언·아이템, C 버프/너프
  - 픽 선호도: 선수별 대회·솔랭 판수·승률·최근성, 프로필 2장의 "고집 픽·잘하는 픽"
  - 선수 스타일: 프로필 1·4장의 성향과 챔피언 성격이 맞는가 (예: 교전형 정글 ↔ 교전 챔피언)
  - 팀 스타일: 팀 프로필의 밴픽 성향(선픽 우선 챔피언, 플렉스 픽, 자주 밴하는 챔피언)
- 리서치 프로필은 글(서술)이라 바로 숫자가 되지 않습니다. 프로필의 주장 표(`research/verified/`)에서 "선수 × 챔피언", "팀 × 조합 성향" 언급을 뽑아 가중치로 바꾸는 단계가 필요합니다 (방법 미정, 7-6).

#### 유동층 — 카운터·조합 (학습)
- **카운터**: (내 챔피언, 상대 챔피언, 포지션 관계) → 상성 점수. 같은 라인 맞대결이 기본이고, 다른 라인 상성(예: 원딜 ↔ 상대 탑 크산테)도 포함.
  - 대회 표본은 502경기라 챔피언 쌍마다 판수가 매우 적음 → 솔랭(G)의 같은 패치 맞대결 승률·라인 지표로 보강하고, 판수가 적으면 0(중립) 쪽으로 줄여서 씀.
- **조합**
  - 세트 조합: 같은 팀에 같이 나온 챔피언 쌍·셋의 빈도와 lift(우연보다 얼마나 자주 같이 나오나), 승률 (예: 애라핀, 룰루+유나라).
  - 스타일 조합: 챔피언 속성(D: 딜 타입, 이니시·캐치, 사거리)과 성장 곡선으로 조합 성향(후반·교전·포킹·픽 등)을 정하고, 팀 프로필의 운영 성향과 얼마나 맞는지 점수.
- **학습 데이터**: B `b_draft_actions.csv` (경기 × step 시퀀스, `is_winner`), A 경기 결과. "상대 픽·조합에 따라 유동적으로 바뀌는 것"을 배우는 게 목적이므로, 입력 = 고정층 점수 + 지금까지의 밴·픽 상태, 정답 = 실제로 그 step에 고른 챔피언(픽 예측) 또는 경기 승패(승률 보정).

### 7-3. 지켜야 할 조건

- **기간 분리**: 고정층·유동층 모두 **2026-08-25까지(리서치 기간)** 자료만으로 만듭니다. **08-26(플레이오프 첫 경기) 이후 경기는 검증에만** 씁니다 (CLAUDE.md 0장). 검증 경기의 패치는 26.16 하나뿐이고, 26.16 리서치 구간(08-19~08-25)은 짧아서 26.16 고정 티어는 직전 패치 값에 크게 기대게 됩니다.
- **피어리스 드래프트**: 같은 매치에서 이미 쓴 챔피언은 양 팀 모두 후보에서 뺍니다 (3장 B). 매치 후반 세트일수록 후보가 줄어드는 것을 반영.
- **사이드와 픽 순서는 별개 변수** (블루 승률 56.3%, 선픽 팀 48.7%, 3장 B). 2026 First Selection 규칙.
- **패치 칸은 문자열** (`16.10` ≠ `16.1`). 리서치 쪽 26.xx ↔ 경기 쪽 16.xx 는 `c_patches.csv`로 맞춤.
- **표기**: 리서치 여론에서 온 값은 "출처들이 이렇게 본다"이지 실제 기량이 아님 (CLAUDE.md 1-3). 추천 근거에 출처 등급을 같이 보여줌.

### 7-4. 평가 (검증 기간, 08-26~)

| 지표 | 계산 |
|---|---|
| 상대 픽·밴 예측 | 플레이오프 각 step에서 상대 실제 선택이 예측 상위 k개 안에 있는 비율 (k = 1·3·5) |
| 고정층만 vs 고정+유동 | 같은 지표로 비교 → 카운터·조합 학습이 실제로 더해 주는지 |
| 추천 픽 | 실제 T1 선택과 겹치는 비율 (참고용. 추천이 맞았는지 승패로 판정하는 건 인과가 아니라서 해석 주의) |

### 7-5. 순서 (안)

1. 팀원 메타 산출물 형식 확인 → 패치 × 챔피언 키(`champion_id`, `patch`)로 조인 가능하게 맞춤
2. 리서치 프로필 → 숫자 가중치 변환 규칙 정하기
3. 고정층: 패치별·팀별 티어리스트 생성 (T1 + 상대 9팀)
4. 유동층: 카운터 표, 조합 표 (세트 조합·스타일 조합)
5. 유동층 학습 (드래프트 시퀀스)
6. 검증 기간 평가 (7-4)

> 2026-11-04: 4·5는 7-7의 GLM 설계(M0~M4)로 구체화됨.

### 7-6. 정해야 할 것

- ~~팀원 메타 결과의 형식~~ → **해결 (2026-11-06)**: 경기일 × 챔피언 메타 점수, 7-7 "팀원 메타 점수" 참고
- 프로필(서술) → 티어 점수로 바꾸는 방법: 주장 표의 `aspect`·`stance` 를 코드로 집계할지, 사람이 프로필을 읽고 태그를 달지
- ~~고정층 신호 4개의 가중치를 손으로 정할지, 검증 이전 구간(예: 26.14·26.15)으로 맞출지~~ → **26.14·26.15로 맞춤** (7-7, 2026-11-04 결정)
- ~~유동층 모델 형태: 단순 점수 합(카운터 표 + 조합 표) vs 시퀀스 학습 모델~~ → **GLM (M1·M2 승패, M4 조건부 로짓)** (7-7)
- 조합 성향 분류(후반·교전·포킹·픽 등) 기준: 팀 프로필에 나온 표현을 그대로 쓸지, 챔피언 속성으로 새로 정할지 → 7-7의 M3(이기는 경로)로 초반형·후반형을 숫자로 정한 뒤 팀 프로필과 대조

### 7-7. GLM 설계 (2026-11-04 결정 · 코드 없음)

"지표를 어떤 기준으로 정했나"의 근거를 **회귀 계수와 신뢰구간**으로 대기 위한 설계. 승패를 보는 모델(조합 가치의 근거)과 픽 선택을 보는 모델(고정 티어리스트의 근거)을 나눔.

#### 확정한 범위·규칙

| 항목 | 결정 |
|---|---|
| 대회 자료 | **LCK + LCK CL(`LCKC`) + LPL + LEC + LCS** (OE, 08-25까지 2,055경기: LCK 447 · LCKC 451 · LPL 619 · LEC 335 · LCS 203). M0 은 솔랭만, **M1·M2·M3 = 솔랭 + 위 5개 리그, M4(픽 선택) 학습 = LCK만** (다른 리그 픽률은 메타 변수로만) |
| 기간 | 지금과 같은 **날짜 기준 08-25까지** (CLAUDE.md 0장). 08-26 이후는 검증만 |
| 패치 | LCK는 리서치 패치 창(= 경기 기록 패치와 같음), **솔랭·다른 리그는 경기 기록의 패치 값** (솔랭은 2026-11-05 변경, 6-4) (같은 날이라도 리그마다 패치가 다를 수 있음). 자르는 기준은 날짜, 패치는 이름표 |
| 기록 완성도 | LPL 은 80%만 `complete` → 시간대 칸(15분 골드 등)이 필요한 M3 는 complete 경기만, 승패만 쓰는 M1·M2 는 전부 |
| M1 행 단위 | **경기 1행, 변수 = 블루 − 레드 차이**, 결과 = 블루 승. 진영 효과는 절편. 절대 수준이 중요한 효과는 상호작용 항을 (블루 기준 − 레드 기준)으로 |
| 속성 누수 방지 | **경기 단위 5묶음 교차 계산**: 솔랭 경기 k묶음에는 나머지 4묶음으로 만든 속성. 대회 경기는 속성 계산에 안 쓰므로 전체 솔랭 속성 |
| 규제 | **M1 엘라스틱넷** (겹치는 변수는 같이 남기고 쓸모없는 변수는 0), **M2 릿지** (챔피언 쌍 수만 개, 판수 적은 쌍은 0 쪽으로). 규제 크기·섞는 비율은 패치 단위 교차검증. 구간은 부트스트랩 |
| M4 팀별 계수 | **3~4개 (숙련·카운터·조합·메타)**, 리그 평균 쪽으로 강하게 당기는 계층형. 나머지 변수는 공통 계수 (팀당 픽 결정 약 450건이라 그 이상은 과적합) |
| 검증 | **26.01~26.11 학습 → 26.14·26.15 로 설정 고름 → 고른 설정으로 08-25까지 전체 재학습 → 플레이오프(08-26~) 한 번만**. 시간 순서로 나눔 (무작위 아님) |
| 성장 곡선 축 | 옛 방식(늦게·일찍 끝난 판 승률 차) 폐기 → 타임라인 방식(25분 − 10분 골드 차이, 후반 딜 몫)으로 교체 |

#### 모델 5개

| 모델 | 질문 | 결과변수 | 분포 · 연결 | 자료 | 산출 |
|---|---|---|---|---|---|
| M0 속성 보정 | 승패·게임 길이를 뺀 챔피언 성격 | 횟수 / 몫 | 포아송·로그(+log 시간 오프셋) / 이항·로짓 | 솔랭 참가자 | 보정 속성표, 요인분석으로 축 묶음 확인 |
| M1 조합 승패 | 어떤 조합 성격이 이기게 하나 | 블루 승 | 이항·로짓, 엘라스틱넷 | 솔랭 → 대회 5리그 → LCK | 조합 계수·구간 = **조합 가중치 근거** |
| M2 시너지·카운터 | 속성으로 설명 안 되는 쌍 효과 | 블루 승 (M1 예측 = 오프셋) | 이항·로짓, 릿지 | 솔랭 + 대회 5리그 | 시너지표·카운터표 |
| M3 이기는 경로 | 조합 → 초반 골드·오브젝트 → 승패 | 15분 골드 차이 / 오브젝트 수 / 승 | 정규·항등 / 포아송·로그 / 이항·로짓 | 솔랭 타임라인 + 대회 complete | 초반형·후반형 정의 |
| M4 픽 선택 | 이 팀은 이걸 고르나 | 실제 선택 | 조건부 로짓 (단계 고정효과 포아송과 같음), 계층형 | LCK 드래프트 | **고정 티어리스트 · 팀 스타일 계수** |

- 리그 계층 (M1·M2·M3): 솔랭 계수 → 대회 공통 편차 → 리그별 편차, 아래로 갈수록 강하게 0 쪽으로 당김. 대회 행은 리그 안 팀 실력(Elo 등) 통제
- 솔랭 카운터인데 대회에서 안 나오는 챔피언: 솔랭 계수 · 대회 근거 수 · 속성 기반 대회 보정 · 등장 확률 · 리서치 언급을 따로 표기 ("솔랭 카운터 · 대회 미검증")
- 미정: M4 에 넣을 리서치 여론 변수 요약 방법 (7-6 두 번째 항목, 시그니처 픽 쪽은 아래 "M4 숙련도 변수"에서 정함), 팀원 메타 형식 (7-6 첫 항목), 대회 팀 실력 지표

#### M4 ↔ 승패 연결 근거 (2026-11-05 설계 · 코드 없음)

"고를 확률이 높다 ≠ 이길 확률이 높다" 이므로 둘을 한 숫자로 섞지 않고, 둘 사이의 관계를 데이터로 확인함.

| 방법 | 내용 | 근거가 되는 값 |
|---|---|---|
| ① 출력 분리 | **고를 확률(M4)** = 상대 예측·밴 우선순위, **이길 가치(M1·M2·M3)** = 우리 추천. 고정 티어표는 이길 가치로 정렬하고 고를 확률은 "상대가 가져갈 위험"으로 옆에 표시 | — |
| ② 모델 밖 선호의 승리 효과 | **26.14·26.15 경기에서** (2026-11-05 수정: 처음엔 "검증 경기에서"였으나 플레이오프로 설정을 정하면 한 번만 검증 원칙 위반) 승패 ~ M1·M2 예측 승률 + M4 "모델 밖 선호"(실제 고른 확률 − 이길 가치로 설명되는 부분) | 계수 > 0 → 팀 고집 픽에 승리 정보(숙련·연습) 있음 → 추천에 일부 반영. 0 근처·음수 → 습관 → 추천에 안 씀 |
| ③ 선택 편향 보정 | M4 고를 확률 = 성향 점수. M1·M2 를 IPW·이중 강건 추정으로 보정해 "골라서 이긴 것"과 "이길 상황이라 고른 것"을 나눔 | 보정 전후 계수 차이 |
| ④ 드래프트 순서 | 같은 챔피언이 늦게(상대 픽을 보고) 골랐을 때 이길 가치가 더 높은가 | 픽 순서 × 챔피언 효과 계수 → 막픽 카운터 추천 근거 |

#### M4 숙련도(시그니처 픽) 변수 (2026-11-05 설계 · 코드 없음)

방향: 출처(대회·솔랭·여론)를 고르지 않고 **모두 변수로 넣고, 가중치는 M4 계수로 정함**. "왜 이 가중치냐" = "실제 LCK 픽을 가장 잘 맞힌 계수(+구간)".

- 솔랭 승률을 주 가중치로 쓰지 않는 이유: 50판 승률 표준오차 약 ±7%p (60%와 50%를 가리려면 수백 판), 자신 있는 판에 고르는 선택 편향. → **판수·픽 비중(드러난 선호)을 주 변수**, 성적은 강하게 당긴 값만 보조
- 모든 변수는 **그 경기 날짜 이전 자료만** (누수 방지)

| 묶음 | 변수 | 계산 |
|---|---|---|
| 대회 | `pro_n` | LCK·LCK CL 에서 그 챔피언 판수, log(1+n) |
| | `pro_recent` | 시간 감쇠 판수 (예: 반감기 60일) |
| 솔랭 | `sq_share` | 그 선수 솔랭 판 중 그 챔피언 비중 (최근 30~90일, 시간 감쇠) |
| | `sq_lift` | **메타 보정 선호** = 같은 패치·포지션 다른 선수 평균 비중 대비 배수 (메타라서 다 쓰는 픽 vs 그 선수만 쓰는 픽) |
| | `sq_perf` | 15분 골드 차이·KDA 를 그 챔피언 평균 대비로, 판수로 당긴 값 (승률보다 잡음 적음) |
| | `sq_wr_eb` | 승률 경험적 베이즈 (선수 평균·챔피언 평균 쪽으로 당김). 참고 |
| 여론 | `nlp_mentions` | 리서치 주장에서 선수 + 챔피언 동시 언급 수, 긍정·부정 순 점수 (챔피언 이름 있는 주장 1,908건) |
| 선택 | `mastery` | 라이엇 champion-mastery (오래된 주력 신호). 현재값뿐이라 과거 시점은 부정확 |

가중치·근거 정하는 절차

1. **M4 조건부 로짓에 그대로 넣음**: 드래프트 단계마다 "고를 수 있던 챔피언 중 실제 선택"을 설명. 계수 β = 가중치, 부트스트랩 구간. 판수·비중이 겹치므로 릿지
2. **묶음 빼 보기**: 26.14·26.15 에서 로그 손실·상위 k 적중률을 대회만 / 대회+솔랭 / 대회+솔랭+여론 으로 비교. 좋아지는 만큼이 그 출처를 넣는 근거, 안 좋아지면 뺌
3. **승리 효과 시험**: M1 에 숙련도 점수(1의 선형 결합)를 넣음. 계수 > 0 → 시그니처를 **추천 점수에도 가산**, 0 → **상대 예측·밴에만** (고르지만 이기게 하진 않는 습관 픽). 위 ② 와 같은 논리
4. **시그니처 이름표**: (a) `sq_lift` 또는 대회 비중이 포지션 기준보다 유의하게 큼 — 베이즈 사후확률 P(비중 > 기준 × 2) > 0.9, (b) 리서치 12구간 중 3구간 이상 지속 (한 패치 메타 픽 제외). 둘 다 → 시그니처, 하나 → 선호, 나머지 → 일반. 배수·구간 수는 26.14·26.15 에서 정함

팀별 고정 티어표 반영: 챔피언 점수 = 이길 가치(M1·M2) + 상대 위험(M4 상대 선택 확률) + 우리 선수 숙련도 가산(3에서 승리 효과가 확인될 때만). 칸마다 근거 숫자 표시 (예: "시그니처 · 대회 12판 · 솔랭 비중 3.1배 · 여론 언급 8")

#### 상대 선수 약점 픽 — "메타지만 줘도 되는 픽" (2026-11-05 설계 · 코드 없음)

예: 상대 미드가 아지르를 못한다면 메타 픽이라도 열어 줄 수 있음 (예시일 뿐, 실제 기록은 확인 전). 판단은 **상대 위험도**로 함:

> 상대 위험(c) = 상대가 c 를 고를 확률(M4) × 상대가 c 를 잡았을 때 이길 가치(메타 가치 + **그 선수의 c 숙련 편차 δ**)

δ 가 충분히 음수면 메타 픽이라도 위험이 낮아져 밴 우선순위가 내려감 ("열어 줘도 되는 픽").

**1. 선수 × 챔피언 숙련 편차 δ 추정**

| 항목 | 방법 |
|---|---|
| 결과변수 | 승패 대신 15분 골드 차이·딜 몫·데스·KDA (판당 정보가 많음). 대회 5~10판 승률은 오차 ±15~20%p 라 판단 불가. M3 경로 계수(골드 → 승률)로 승률 단위 환산 |
| 기준 | 같은 패치·포지션의 **그 챔피언 평균**과 **그 선수 평소 수준**을 모두 뺌 (챔피언이 약한 패치·선수 컨디션을 편차로 오인하지 않게) |
| 모델 | 혼합효과: `지표 ~ 챔피언×패치 + 선수 + (선수×챔피언 무작위효과) + 드래프트 상황`. δ = 무작위효과 추정치(BLUP), 판수가 적으면 자동으로 0 쪽으로 당겨짐 |
| 자료 | 대회(LCK·CL) + 솔랭 계층: 솔랭 δ 를 사전 정보로, 대회 δ 는 거기서 벗어난 만큼. 두 δ 의 상관도 같이 추정 |
| 드래프트 통제 | M1·M2 예측 승률을 오프셋으로 ("불리한 조합에서 억지로 뽑아서 짐"과 구분) |
| 시간 | 최근 판 가중. 패치 변화는 챔피언×패치 항이 흡수 |

**2. 이름표**
- **약점**: 사후확률 P(δ < −τ) > 0.8 (τ 예: 15분 골드 −300). 판수·구간·출처(대회/솔랭) 같이 표시
- **근거 부족**: 구간이 0 포함 → 약점이라 부르지 않음
- **안 하는 것 ≠ 못 하는 것**: 거의 안 고르는 챔피언은 δ 가 아니라 M4 고를 확률이 낮은 것 (위험도 공식에서 이미 낮음)
- 리서치 여론("○○ 아지르 별로")은 사전 정보를 약하게 조정하거나 대조용. 판정 근거 아님

**3. 근거 확인**
1. **예측력** (시간 순서): 26.01~26.11 로 δ 추정 → 26.14·26.15 LCK 경기 M1 에 "양 팀 선수 픽별 δ 합의 차이" 투입. 계수 > 0 (1 에 가까울수록 보정 잘 됨) → 과거 숙련 편차가 미래 승패를 예측한다는 근거. 0 이면 잡음 → 안 씀
2. **프로 팀도 이렇게 하나**: 밴도 단계별 조건부 로짓으로 학습, 변수에 "상대 선수의 그 챔피언 δ". 계수 > 0 → 상대가 잘하는 챔피언은 밴, 못하는 챔피언은 열어 준다는 실제 행동 확인 → M4 팀별 계수에 "상대 맞춤 밴 성향" 후보
3. **열어 준 경우의 결과**: 약점 픽을 열어 줬고 상대가 실제로 잡은 경기의 예측 대비 실제 승률 (표본 적어 참고)

**4. 주의**
- 선택 편향: 자신 있을 때 고르므로 기록된 판의 δ 는 부풀고 약점은 덜 드러남 → M4 고를 확률로 IPW (위 "M4 ↔ 승패 연결 근거" ③)
- 표본: LCK 선수×챔피언은 대부분 한 자리 판수 → δ 대부분 0 근처, **확실한 약점은 소수**만 나오는 게 정상
- 숨겨 둔 픽은 기록에 없음 → 모델 한계로 표시

#### 전체 흐름·검증 순서·M5 실시간 추천 (2026-11-05 정리 · 코드 없음)

**흐름 (재료 → 결과)**
- M0 → 챔피언 속성. M1·M2 → 조합 승패·시너지·카운터 값 (이길 가치). M3 → 초반형·후반형 정의, 골드 → 승률 환산 (M4 에 직접 들어가지 않음)
- M4 → "이 팀은 무엇을 고르나" (고를 확률). M1·M2 값, 메타, 숙련도가 M4 의 **입력**
- **팀별 고정 티어표는 M4 의 입력이 아니라 결과물**: 이길 가치(M1·M2)로 정렬, 상대가 가져갈 위험(M4)은 옆 칸 (위 "M4 ↔ 승패 연결 근거" ①)
- 우리 숙련도(시그니처)·상대 약점(δ)은 M1 에서 승리 효과가 확인될 때만 추천 점수에 가산, 아니면 상대 예측·밴에만

**팀원 메타 티어리스트 (재료 1) 자리**

| 위치 | 쓰는 방식 | 나오는 것 |
|---|---|---|
| M4 (주) | 같은 패치 티어 점수를 메타 변수로 (대회·다른 리그·솔랭 픽밴률과 같이) | 팀별 메타 계수 = 팀이 메타를 얼마나 따르나 |
| M1 (시험) | 조합 티어 점수 합의 블루 − 레드 차이 | 계수 > 0 → 이길 가치에도 반영, 0 근처 → 고를 확률에만 |
| M5 화면 | 근거 칸에 표시 | 참고 |

넣기 전 확인 (팀원에게): ① 패치 × 챔피언 × **포지션** 단위인가 ② 무엇으로 만들었나 (대회 픽률이면 픽밴률과 겹침 → 릿지, 패치노트·커뮤니티·솔랭이면 독립 신호) ③ **시점**: 그 패치 경기 전체로 만든 값이면 누수 → 경기 날짜 이전 자료만 쓴 값 또는 직전 패치 티어. 26.16 티어도 08-25까지 자료로 ④ 등급이면 순서형 숫자로

**검증 순서 (7-7 "검증" 행 풀어 쓰기)**

| 단계 | 기간 | 하는 일 |
|---|---|---|
| 학습 | 26.01~26.11 | 계수 추정 |
| 설정 고르기 | 26.14·26.15 | 규제 크기, 출처 묶음(대회/솔랭/여론), 시그니처·약점 기준 선택. **계수를 맞추는 게 아님** |
| 재학습 | 08-25까지 전부 (26.16 앞부분 08-19~25 포함) | 고른 설정으로 |
| 최종 검증 | **08-26~ 플레이오프만, 한 번** | 결과 보고 고치면 더 이상 검증 아님 |

검증 지표 해석
- **주 지표 = 상대 예측**: 상대 실제 선택이 예측 상위 1·3·5 안에 있는 비율 (M4 성능)
- T1 추천 ↔ T1 실제 픽 일치율은 **참고만** (높아도 "하던 걸 따라 함"일 수 있음)
- 추천의 질: M1·M2 예측 승률의 보정(로그 손실·보정 곡선), 고정층만 vs 고정+유동 비교 (7-4)

**M5 실시간 추천 — 사용 장면과 미정 항목**

장면: T1 분석관이 상대(예: 한화) 밴·픽을 입력 → T1 다음 차례 밴·픽 추천 + 상대 다음 선택 예측. 점수 = 이길 가치(M1·M2) + 상대 위험(M4 × 상대 가치, δ 포함) + 숙련 가산(확인 시). 칸마다 근거 숫자 표시.

| # | 미정 항목 | 선택지 |
|---|---|---|
| 1 | 상대 전략 추론 | 지금은 M4 가 드래프트 상태를 조건으로 다음 선택 확률만 냄. 명시적으로 하려면 상대 픽으로 M3 조합 유형(초반형·후반형 등) 확률을 갱신하는 단계 추가 |
| 2 | 앞 수 보기 | 지금 좋은 픽만 고르면 상대 막픽 카운터에 노출. M4 상대 응수 확률로 1~2수 탐색 vs 위 ④(늦게 고른 가치)로 대신 |
| 3 | 두 장 동시 픽 | B2·B3, R4·R5 처럼 연속 차례는 챔피언 쌍(시너지 포함) 추천 |
| 4 | 입력 항목 | 상대 밴·픽 + 진영, First Selection, 세트 번호, 피어리스로 이미 쓴 챔피언, 상대 선수 명단 |

#### 설계 점검 결과 — 허점과 보완 (2026-11-05 · 코드 없음)

M0~M5 전체를 다시 본 결과. A = 그대로 두면 결과가 틀림, B = 설계 보완, C = 작은 것. 반영 시점은 오른쪽 칸.

| # | 문제 | 보완 | 반영 시점 |
|---|---|---|---|
| **A1** | "M4 ↔ 승패 연결 근거" ② 를 검증 경기로 추정 → 플레이오프로 설정을 정하게 됨 | **26.14·26.15 로 옮김 (반영 완료)** | 완료 |
| **A2** | 플레이오프는 5판 3선승: 4·5세트가 검증 55경기 중 16 (29%), 학습 448경기 중 21 (5%) → 피어리스로 후보가 많이 줄어든 상황을 M4 가 거의 못 봄 | M4 변수에 세트 번호·잠긴 챔피언 수(양 팀), 검증 지표를 세트 번호별로 따로 | M4 |
| **A3** | 피어리스를 "후보에서 빼기"로만 다룸 → 강한 픽을 아껴 두거나 상대 주력 픽을 먼저 써 버리는 판단이 없음 | M4: 남은 세트 수 × 챔피언 가치 상호작용. M5: 시리즈 단위 판단 (우리·상대 남은 후보 폭) | M4·M5 |
| **A4** | M5 "점수 합"의 단위가 다름 (승률 vs 고를 확률). M1·M2 는 5명 완성 조합으로 학습하는데 드래프트 중엔 미완성 | 모두 **기대 승률 변화**로 환산 (우리 픽 = 우리 승률 증가, 밴 = 상대 선택 확률 × 그때 우리 승률 감소). 남은 픽은 M4 확률로 채워 평균 (또는 미완성 조합 모델). M1 예측 승률 보정 곡선 확인 | M1·M5 |
| B1 | 누수 3곳: ① 08-25 전체 솔랭 속성표로 26.14·26.15 설정 고르기 ② 프로필 서술(전체 기간)을 M4 여론 변수로 ③ `mastery` 현재값 | ① 설정 고를 땐 그 패치까지 솔랭만으로 만든 속성표 ② `research/verified/` 의 **경기 날짜 전 주장만** ③ 검증에서 뺌 | M0·M4 |
| B2 | 솔랭 표본 = 리서치 선수 58명이 있던 판 → 프로가 있는 쪽의 조합 계수가 부풀 수 있음 | M1·M2 솔랭 행에 진영별 프로 수(또는 선수 효과) 통제 | M1 |
| B3 | 솔랭 거르기가 5분 미만·조기 항복만 | `wasAfk`·`suspect_account` 판 제외, `on_role` 아닌 판은 속성 계산에서 제외 또는 표시 | M0 |
| B4 | M4 후보 범위 | 밴·픽·피어리스 외에 **이미 채운 포지션** 제외, 플렉스는 포지션 확률로 섞음. 조건부 로짓의 대체 관계(IIA) 한계 → 같은 역할 묶음 보정 또는 한계로 표시 | M4 |
| B5 | 팀별 계수가 시즌 내내 같은 팀이라고 가정 | 주전 교체·코치 변경 시점으로 나누거나 최근 경기 가중 (선수 단위 숙련도는 영향 없음) | M4 |
| B6 | 신챔피언·리워크도 전체 기간 평균 쪽으로 당겨짐 → 옛 성격을 따라감 | `c_patch_champion_changes.csv` 로 리워크 표시, 그 패치부터 따로 | M0 |
| B7 | 검증 표본 적음 (플레이오프 55경기, T1 18경기), 설정 고르기 2개 패치뿐 (26.14 는 솔랭 메타 급변) | 부트스트랩은 **경기 단위**, 설정 후보는 미리 몇 개로 줄임, 기준선 미리 고정 (직전 패치 그대로 · 팀원 메타 C 순위 · M4 기본판) | 검증 |
| C1 | `a_*` 파일을 그냥 읽으면 패치 `16.10` → `16.1` | M 단계 코드는 모두 `dtype=str` | 전부 |
| C2 | Leaguepedia 드래프트 503경기 vs OE 502경기 | 조인 키·빠진 1경기 확인 | M4 |
| C3 | 궁극기 축: 광역 판정이 살아남은 적을 못 셈, 유형 라벨은 사람 확인, 변신 챔피언 | 수집 후 ①② 계산 · ③ 라벨 초안 확인 · 변신 챔피언 제외 | M0 |
| C4 | 실전 입력 오류 | `d_champion_name_map.csv` 로 이름 맞추기, 이미 쓴 챔피언 입력 검사 | M5 |
| C5 | 설계 보고서(Claude Doc)의 옛 수치 | 2026-11-05 반영 | 완료 |

#### M0 1차 실행 결과 — `src/pipeline/m0_attributes.py` (2026-11-05)

```bash
python src/pipeline/m0_attributes.py              # 전체 + 교차 5벌 + 시점별 2벌
python src/pipeline/m0_attributes.py --only full  # 전체만 (약 40초, 첫 실행은 잠수 플래그 때문에 +6분)
```

- 방법: 포지션 × 지표(28개)마다 판 단위 회귀 `지표 ~ 챔피언 + 패치 + 승패 + 게임 길이 10분위` (분당 값 = 포아송·게임 시간 가중, 몫·0/1 = 분수 로짓, 연속 = 정규). 승패 0.5·평균 길이 기준으로 판 값을 옮긴 뒤 기존 속성표 계산(당기기·백분위)을 그대로 통과
- 거르기(점검 B3): `wasAfk`(16.2x 이후만 값 있음) + 잠수 추정(분당 골드 < 170 · 분당 챔피언 피해 < 60) → 17경기 제외. 매칭 배정 포지션 필드는 대부분 기간에 없어 못 씀
- 산출 `data/processed/2026/m0/`: `m0_attributes.csv`(전체), `m0_attributes_fold{0-4}.csv`(경기 crc32 % 5 = k 를 뺀 표, M1 솔랭 k 묶음용), `m0_attributes_asof_26.14.csv`·`_26.15.csv`(그 LCK 패치 창 첫날 전 솔랭만, 설정 고르기용 — 점검 B1), `m0_nuisance.csv`(계수), `m0_eval.md`
- 결과
  - 승패 효과는 판 단위로 큼 (멀티킬 ×2.4, 포탑 방패 ×2.5, 건물 피해 ×2.2, 데스 ×0.63, 아군 회복·보호막 ×1.34). 게임 길이는 회복·보호막(긴 판 ×1.9)·막은 피해(×1.4)에 큼
  - 그런데 **챔피언 단위 축 순위는 거의 그대로** (보정 전후 순위 상관 0.97~1.00, 15점 넘게 바뀐 행은 스플릿 12/387 외 0~1) → 챔피언 승률이 45~58% 안이라 판 단위 효과가 평균에서 상쇄됨. **1차 속성표는 승패에 크게 오염되지 않았음**
  - 챔피언 승률과 축의 상관(포지션 안)은 보정 전후 비슷 (이니시·캐치·보호 +0.12 → +0.10~0.12) → 남은 상관은 "이겨서 생긴 값"이 아니라 그 성격의 챔피언이 실제로 조금 더 이기는 것 (M1 이 판단)
  - 스플릿 축만 +0.04 → −0.16 으로 움직임 (세나·티모 바텀 같은 비주류 행). 스플릿 지표(건물 피해·포탑 방패)가 승패 영향이 가장 커서 보정 효과도 큼
  - 타당성(Meraki AUC)·딜 타입 일치는 1차와 같음 (앞라인 0.89, 이니시 0.87, 보호 0.99, 다이브↔ASSASSIN 0.89)
  - 요인분석(varimax 7): 보호(회복·보호막) / 받은 피해(앞라인) / CC로 잡기(이니시) / 막은 피해 / 건물(스플릿) / 킬 결정력(다이브) / 라인 우위 — **지금 축 구성과 거의 일치**. 막은 피해는 앞라인과 따로 묶임 (앞라인 축에 넣을지 M1 에서 판단)
  - 큰 변경 표시(점검 B6): 26.03 멜, 26.09 우디르·쉬바나·제리, 26.15 벨베스 → 1차는 표시만, M1 전에 당기기 분리 여부 판단
- 한계: 타임라인 축(성장·후반 딜·순간 딜)은 아직 보정·교차 계산 안 함 (전체 경기 값을 모든 표에 붙임). 궁극기 ①② 는 킬 피해 수집 후 추가

#### M0 2차 보완 (2026-11-06)

1차 한계 4개를 보완. 평가 `m0/m0_eval.md` (2차), 회귀 계수 `m0_nuisance.csv`, 챔피언별 승패 효과 `m0_champ_win.csv`, 큰 변경 판정 `m0_rework.csv`

| # | 1차 한계 | 2차 처리 | 결과 |
|---|---|---|---|
| ① | 타임라인 축 미보정, 교차 계산 벌에도 전체 경기 값 | 판 단위 타임라인 값(분별 골드 차이·딜 몫, 성장, 구간 딜, 골드당 딜, 순간 딜)도 같은 회귀(정규)로 보정. 교차 5벌·시점별 2벌도 **그 벌의 경기만으로** 다시 계산 (`m0_timeline.pkl` 캐시) | 승리 효과 큼: 15분 골드 차이 +1,089, 25분 +1,558, 성장 +1,383. 축 순위는 거의 그대로(0.986~0.999). 반분 안정성 성장 0.86·후반 딜 0.88 (1차 0.78·0.86 보다 나음) |
| ② | 잠수 판 거르기 약함 | 끝 기록(분당 골드·피해)은 **잠수 선수도 정상과 비슷**해서 버림 (wasAfk 16명 확인). 타임라인 골드 정지(5분 증가 < 800 또는 10분 골드 < 1,900)로 교체 | 실제 자료에 골드 정지가 거의 없음 (최저 0.05% 분위도 5분에 약 900). wasAfk 16 + 골드 정지 37 = **53경기 제외**. wasAfk 선수는 골드가 정상으로 늘다가 경기 막판에 나간 경우라 골드 정지로는 안 잡힘 → 잠수는 자료에서 0.2% 수준이고 속성에 주는 영향은 무시할 정도 |
| ② | 주 포지션 아닌 판 | 선수 단위 판정 필드(매칭 배정 포지션)가 대부분 기간에 없음 → 대신 `pos_share`(그 챔피언 판 중 이 포지션 비율)·`offrole`(< 5%) 칸 | 판수 30 이상 비주류 행 55개 (니달리 탑·서폿, 요네 바텀 등) |
| ③ | 리워크 표시만 | 패치노트 후보 → 변경 전후 지표 차이가 같은 날짜로 나눈 다른 챔피언들보다 95% 분위 이상이면 나눔. 변경 전 경기는 `era = pre{패치}`, 전체 기간 행 patch = `ALL<{패치}`, 기본 `ALL` 은 변경 후 | **쉬바나(26.09, 96%)·벨베스(26.15, 100%) 나눔**. 우디르 89%·제리 73% 는 안 나눔, 멜(26.03)은 변경 전 18판뿐이라 판정 불가 |
| ④ | 승패 효과가 포지션 공통 | 공통 회귀 잔차로 챔피언별 (이긴 판 − 진 판) 차이 → 판수 조화 평균으로 당김(K = 100) → 공통 효과에 더함. 0 이 많은 횟수는 (실제 합 ÷ 예측 합) 비율로 계산 | 예: 멀티킬 승리 효과가 징크스·스몰더·트리스타나는 더 크고 스웨인·벨코즈는 작음. 딜 몫은 징크스·카사딘이 이기면 더 오름 |

- 챔피언 승률과 축의 상관 (포지션 안, 보정 전 → 1차 → 2차): 캐치 +0.12 → +0.10 → +0.04, 딜 비중 −0.10 → −0.10 → −0.06, 후반 딜 −0.12 → −0.11 → −0.06, 성장 −0.06 → −0.05 → −0.14, 스플릿 +0.04 → −0.16 → −0.16. 이니시·보호(+0.12)는 그대로 → 그 성격 자체가 조금 더 이기는 쪽 (M1 이 판단)
- 축 순위는 2차에서도 보정 전과 0.97~1.00 → **"솔랭 속성표가 승패에 크게 오염되지 않았다"는 1차 결론 유지**
- 파일: 전체 표 `m0_attributes.csv` 는 2026-11-06 00:49 에 2차로 다시 만듦 (`_v2` 파일은 없앰). 교차 5벌·시점별 2벌도 2차

#### 팀원 메타 점수 — 받은 자료와 쓰는 법 (2026-11-06, 재료 1 · 7-6 첫 항목 해결)

- 원본 `data/raw/teammate_meta/` (handoff zip, `README.md`·`handoff_check.md` 포함, 수정하지 않음) → `src/pipeline/i_teammate_meta.py` → 우리 이름으로 바꾼 가공본 `data/processed/2026/i_meta_score_daily.csv`(경기일 111 × 챔피언 172 = 19,092행), `i_champ_meta_daily.csv`(날짜 × 출처 × 챔피언 × 포지션, 122,804행)
- **이름 바꾼 것** (팀원 문서의 M3·M5·M5c·M5d 는 우리 M3·M5 와 다른 것이라 쓰지 않음)

| 팀원 | 우리 | 뜻 |
|---|---|---|
| A ("M5") | `meta_pre` | 경기 전 예측 등장률(픽+밴). 직전 패치 등장률 + 다른 리그 변화 + 솔랭 변화 + 추세 + 패치 노트, 계수는 앞선 패치로만 학습. 26.01·26.02 는 없음 |
| B | `meta_lck` (`_pick`, `_ban`) | 그 패치 전날까지 LCK 등장률, 피어리스로 잠긴 세트는 분모에서 뺌 |
| C ("M5c") | `meta_score` | (k·A + n·B)/(k + n), n = 그 패치 LCK 세트 수. 팀원 운영용 티어리스트 순위 |
| k | `meta_k` | A 를 LCK 몇 세트만큼 믿나 (앞선 패치로만 고름, 대부분 20) |
| main_position, n_positions_25 | `pro_main_position`, `pro_n_positions_25` | 프로 픽 주 포지션, 25% 이상 간 포지션 수 |
| split train/test | `period` research/validation | 08-25 까지 / 08-26~ |
| source pro_LCK … soloq_KR | LCK · LCKC · LPL · LEC · LCS · LCP · CBLOL · SOLOQ_KR | OE league 표기 |
| position 빈칸 | `ALL` | 그날 챔피언 합계 (밴은 이 행에만) |
| — | `oe_patch` | 16.xx (경기 기록 쪽과 붙일 때) |

- **붙이는 규칙**: 경기일 X 의 드래프트에는 `i_meta_score_daily` 의 `valid_for == X` 행 (X 전날까지 자료로 만든 값). 원자료는 `date < X` 만 더해 씀. 검증 기간(08-26~) 행은 08-25 까지 정보로 만든 값이라 우리 "08-25 까지 학습, 플레이오프는 한 번만" 규칙과 맞음. 같은 날 앞 경기·같은 시리즈 앞 세트는 들어 있지 않음 → 피어리스 후보 빼기는 M4 에서
- **쓰는 곳**

| 쓰는 곳 | 방식 | 근거 |
|---|---|---|
| **M4 메타 변수 (주)** | 드래프트 단계마다 `meta_score` (+ `meta_lck_pick`·`meta_lck_ban`), 팀별 메타 계수 | 픽·밴 단계에 같은 `meta_score` 를 넣고 단계별 계수로 나눔 (A·C 는 픽·밴을 못 나눔) |
| M4 플렉스·포지션 확률 | `pro_share_*`, `flex_bot`, `bot_now/prev`, `soloq_share_*` | 비원딜 바텀 처리 포함 |
| 패치 초반 | `meta_pre` 로 시작해 경기가 쌓이면 `meta_score` 가 `meta_lck` 쪽으로 | 팀원 재생 시험: 섞기가 실제만보다 8/8 패치에서 나음 (p = 0.004, 플레이오프 빼도 같음) |
| **M4 기준선** | `meta_score` 순위만으로 상대 픽 맞히기 → M4 가 이걸 넘어야 팀별 계수·숙련·카운터가 의미 있음 | 7-4 평가에 추가 |
| M1 시험 (선택) | 조합 `meta_score` 합 차이 → 승리 효과 있으면 이길 가치에도 | 등장률 = "원하는 정도"라 이길 가치와 다름 |

- **중복 금지**: `meta_pre` 안에 다른 리그·솔랭 변화가 들어 있으므로 M4 에 우리 쪽 다른 리그·솔랭 픽밴률을 또 넣지 않음. 출처별 계수를 따로 볼 때만 `meta_lck` + `i_champ_meta_daily` 원자료
- **충분한가 (점검 결과)**: 요청했던 4개 중 솔랭 실제 버전 이름표 ✅, 경기일마다 전체 챔피언 ✅, k 앞선 패치로만 ✅, 픽·밴 분리는 `meta_lck` 만 ⚠️. LCK 경기일 111일 우리 드래프트와 일치, 우리 d_champions 에 없는 id 0
- **차이 1경기**: 08-01 젠지 vs 디플러스 기아 1세트 — OE 경기 기록이 없어(Riot 경기 ID 빈칸) 팀원 자료(502경기)에 없음, 우리 드래프트(503경기)에는 있음 → M4 학습엔 넣고, 그날 메타 값은 같은 날 다른 경기와 같은 `valid_for` 행
- **한계**: 26.01·26.02 18일은 `meta_pre`·`meta_score` 없음 (`meta_lck` 로 대신), 팀원 솔랭 표본은 패치당 500판 (A 안에서만 쓰임 — 우리 숙련도 변수 `sq_*` 는 우리 솔랭 27,230판으로), 커뮤니티 언급 칸 비어 있음 (여론은 우리 `nlp_mentions`)

#### M0 완성 — 궁극기 축·궁 유형 라벨 (2026-11-06)

- 킬 피해 스킬별(`timeline_killdmg`, 27,177경기) → 참가자 행마다 궁 피해·궁 관여 킬·한타(15초 묶음)당 궁에 맞고 죽은 적 수 (`m0_ult.pkl` 캐시) → 같은 회귀로 승패·길이 보정 → `ax_ult_dmg`(궁 딜 비중, 평균 10.5%), `ax_ult_hit`(궁 킬 관여), `ax_ult_aoe`(궁 다중 처치). 반분 0.94~0.99
- **데이터 궁 축은 광역 CC 궁을 못 잡음**: 툴팁 광역 CC 궁을 오히려 반대로 가림(0.38~0.46). 기록이 "죽은 희생자가 직전 받은 피해"뿐이라 묶기만 하는 궁·피해 없는 궁(알리스타·쉔)이 안 남음 → 이 축들은 "궁으로 킬을 내는 정도"
- **한타형 궁 = 사람 라벨** `research/config/ult_types.csv` (173명, 2026-11-06 사용자 확인; M0 재실행에도 덮어쓰지 않음, 초안은 `m0/m0_ult_types_draft.csv`) → 속성표 칸 `ult_type`(딜·광역 CC·단일 CC·자기 강화·아군 보호·이동·전역·상대 궁), `ult_label_aoe`(1/0), `ult_teamfight`(광역 CC 또는 범위 Y). 사일러스 = 상대 궁(빈칸, M1 에서 상대 궁으로)
- 라벨 검증: 광역 CC 라벨 ↔ VANGUARD AUC **0.911** (툴팁 옛 축 0.686 → `ax_ult_fight_tooltip` 으로 이름 바꿔 참고만), 한타형 궁 0.706. 딜 라벨 행의 궁 딜 비중이 가장 높고(66) 광역 CC 라벨 행의 이니시가 가장 높음(65) → 라벨과 데이터가 맞음. 툴팁 규칙과 일치 78%
- 축별 신뢰도 표 `m0/m0_reliability.csv` (반분·인접 패치·이름표 AUC·대회 일치). 낮음 = 캐치, 툴팁 한타형 궁. 스플릿 반분 0.76
- 보고서: Claude Doc "M0 챔피언 속성표 보고서" (https://claude.ai/artifact/765yy6qzGFQJBhcvVRm2nL)

#### M1 1차 실행 — `src/pipeline/m1_comp.py` (2026-11-06)

```bash
python src/pipeline/m1_comp.py --boot 100   # 약 4분
```

- 행 = 경기 (블루 − 레드 조합 성격 19개), 결과 = 블루 승. 솔랭 27,175 + 대회 2,055경기(LCK 447 · LCKC 451 · LPL 619 · LEC 335 · LCS 203), 08-25 까지. 플레이오프 안 봄
- 조합 성격 = 팀 5명 평균: M0 축 12개(앞라인·이니시·보호·킬 결정력·딜 비중·포킹·스플릿·후반 캐리·중반 스노볼·순간 딜·궁 딜·라인전) + 기본 공격·스킬 사거리, 근접 수, 딜 쏠림(|물리 − 마법| 딜 가중), 한타형 궁 수, 광역 CC 궁 수, 아군 보호 궁 수. 사일러스 = 상대 팀 한타형 궁 비율
- 계층: 공통 + 대회 편차(×c_pro) + 리그 편차(×c_league) 칸을 붙인 엘라스틱넷 로지스틱 (편차는 배율만큼 더 세게 0 쪽으로). 통제: 솔랭 = 진영별 리서치 프로 수 차이(점검 B2), 대회 = 경기 전 리그 안 Elo 차이(K = 24)
- 속성: 솔랭 행 = M0 교차 5벌, 대회 행 = M0 전체 표, 설정 고르기 = 그 시점 표(asof) 전부
- **설정 고르기** (26.14 · 26.15 대회 136 · 225경기, 그리드 32개): C = 0.03, l1 = 0.2, c_pro = 0.3, c_league = 0.5

| 패치 | 진영만 | Elo 만 | M1 |
|---|---|---|---|
| 26.14 로그 손실 (AUC) | 0.703 (0.46) | 0.667 (0.65) | **0.651 (0.67)** |
| 26.15 로그 손실 (AUC) | 0.682 (0.55) | **0.644 (0.64)** | 0.646 (0.64) |

  → **조합 성격은 팀 실력(Elo) 위에 더해 주는 몫이 작음** (26.14 조금 나음, 26.15 같음). LCK 만은 24 · 49경기라 판단 불가. 보정 곡선은 양 끝(0.30 → 0.26, 0.76 → 0.82)이 맞고 가운데(0.45~0.61)는 실제 0.51~0.58 로 평평
- **최종 계수** (`m1/m1_coefs.csv`, 1 SD 차이당 로그 오즈, 부트스트랩 100회 95%): 0.05 ≈ 승률 1.2%p
  - 구간이 0 을 안 포함 (솔랭): 딜 쏠림 −0.064, 보호 +0.052, 한타형 궁 수 −0.047 (광역 CC 궁 수 +0.033~0.041 과 같이 들어가 "CC 없는 광역 궁"의 몫), 이니시 +0.037, 순간 딜 −0.029, 궁 딜 −0.028 (대회 −0.054)
  - 0 근처: 앞라인, 포킹, 사거리, 라인전, 후반 캐리, 중반 스노볼, 근접 수, 아군 보호 궁 수
  - LCK 편차는 판수가 적어 대부분 대회 공통으로 당겨짐
  - 통제: 솔랭 프로 1명 더 → +0.19 (B2 통제가 필요했음), 대회 Elo 차이 /400 → +1.99
- 산출: `m1/m1_eval.md`, `m1_coefs.csv`, `m1_selection.csv`, `m1_games.csv`(경기별 예측 `m1_pred`, 5묶음 교차 예측 `m1_pred_oof` = M2 오프셋), `m1_model.pkl`
- 한계·다음: 주효과만 (상호작용 없음 — 예: 이니시 × 상대 포킹), 선택 편향 보정(③ IPW) 아직, 대회 실력 지표는 Elo 하나

#### M1 2차 — 상호작용 + 대회 숙련 통제 (2026-11-06)

```bash
python src/pipeline/m1_comp.py --boot 100                    # 약 30분 (그리드 256개)
python src/pipeline/m1_comp.py --boot 100 --reuse-selection  # 저장된 m1_selection.csv 로 그리드 건너뜀
```

- 추가 ① **상호작용 196칸** (공통 칸만, 배율 `c_int`): 팀 값(표준화) 곱. 상대 사이 `b_x·r_y − r_x·b_y` 91칸 (진영을 바꾸면 부호만 바뀜), 같은 팀 안 `b_x·b_y − r_x·r_y` 105칸 (제곱 포함 = 많을수록 효과가 줄어드는지)
- 추가 ② **대회 숙련 통제** (선택 편향 일부, 점검 ③의 임시 대체): 선수(OE playerid)가 그 챔피언으로 그 경기 전까지 한 대회 경기 수, 팀 평균 log(1+n) 차이
- 설정: C 0.03 · l1 0.2 · c_pro 0.3 · c_league 0.5 · **c_int 1.0** (설정 128개 중 최소)

| 패치 | Elo 만 | M1 1차 형태 | **M1 2차** |
|---|---|---|---|
| 26.14 로그 손실 | 0.667 | 0.651 | **0.650** |
| 26.15 로그 손실 | 0.644 | 0.646 | **0.642** |

- 주효과는 1차와 거의 같음 (보호 +0.054, 딜 쏠림 −0.064, 한타형 궁 수 −0.051, 이니시 +0.046, 킬 결정력 +0.045 가 새로 굵게, 궁 딜 대회 −0.052)
- 상호작용: 182칸이 0 이 아님, 구간이 0 을 안 포함 16칸 (196칸 × 5% ≈ 10칸은 우연일 수 있음). 큰 것: 같은 팀 궁 딜 × 라인전 −0.040, 우리 킬 결정력 × 상대 궁 딜 +0.033, 같은 팀 캐리 × 스노볼 +0.033, 킬 결정력 제곱 −0.025 (많을수록 효과 줄어듦)
- 숙련 통제: 계수 +0.066 [−0.056, +0.199] (0 포함), 넣어도 조합 계수 변화 최대 0.002 → **"익숙한 챔피언을 골라서 이긴 것"이 조합 효과로 잘못 잡힌 몫은 작음**. 정식 IPW 는 M4 뒤
- 산출 추가: `m1/m1_interactions.csv`. 1차 결과 `m1/_v1/`

#### M2 1차 — `src/pipeline/m2_synergy.py` (2026-11-06)

```bash
python src/pipeline/m2_synergy.py --boot 30   # 약 3분
```

- 행 = 경기, 결과 = 블루 승, **오프셋 = M1 교차 예측 로짓**. 칸(진영 반대칭, 키 = 챔피언@포지션): 챔피언 단독 675 + 챔피언×패치 3,553 + 시너지(같은 팀 쌍, 15판 이상) 8,117 + 카운터(상대 쌍, 15판 이상) 10,246, 대회 편차 4,418. 릿지 (scipy L-BFGS, 칸 종류별 규제)
- 설정 고르기 (M1 과 같은 규칙: 26.14·26.15 마다 cut 이전으로 M1·M2 학습, 그 패치 대회 + 솔랭 경기로 평가)
  - 1차 그리드(규제 3~100): **M1 만보다 나빠짐** (26.15 대회 0.642 → 0.651), 가장 센 규제 끝에서 골라짐 → 쌍 칸이 우연을 배움
  - 넓힌 그리드(규제 100~3000): 챔피언 100 · 시너지 3000 · 카운터 3000 · 대회 편차 배율 10 (여전히 끝 쪽, 3000 근처에서 평평)

| 패치 | M1 만 (대회 / 솔랭) | M1 + M2 (대회 / 솔랭) |
|---|---|---|
| 26.14 | 0.652 / 0.694 | **0.650 / 0.691** |
| 26.15 | 0.642 / 0.694 | **0.636 / 0.693** |

- **결론: 나아진 몫은 거의 챔피언 단독 효과에서 옴**. 쌍 효과는 규제 3000 에서 ±0.2%p 이하로 줄어 사실상 0 → 이 자료 크기(솔랭 2.7만 + 대회 2천)로는 **챔피언 쌍마다 시너지·카운터를 따로 가려낼 힘이 없음**. 시너지·카운터 표의 굵은 칸은 규제로 구간이 좁아진 탓이라 "의미 있음"으로 읽지 않음
- 챔피언 단독 효과 (속성으로 설명 안 되는 몫, 50% 근처 %p): + 뽀삐 서폿 4.1, 렝가 정글 3.7, 니달리 정글 3.5, 렐 3.2 / − 바루스 −3.9, 밀리오 −2.6, 사이온 탑 −2.5, 아지르 −2.3. 솔랭 비중이 커서 솔랭 강세가 많이 반영됨 (대회 편차는 배율 10 으로 강하게 당김)
- 산출: `m2/m2_eval.md`, `m2_champion.csv`, `m2_champion_patch.csv`, `m2_synergy.csv`, `m2_counter.csv`, `m2_selection.csv`, `m2_games.csv`(`m2_pred_oof`), `m2_model.pkl`. 1차 그리드 결과 `m2/_v1/`
- 다음 (안): 쌍을 **구조가 있는 것만** 두어 칸 수를 줄임 — 바텀 듀오(원딜 + 서폿), 정글 + 미드·탑, 맞라인 5쌍. 또는 쌍 효과를 속성 차원으로 줄임(챔피언 × 상대 속성축). M5 의 카운터·시너지 근거는 M1 상호작용(성격 × 성격)이 대신할 수 있음

#### M2 2차 — 구조 쌍 + 15분 라인 골드 (2026-11-06)

```bash
python src/pipeline/m2_synergy.py --boot 30   # 약 7분
```

- 1차(모든 쌍, 승패만)는 쌍 효과가 0 으로 사라져 다시 설계. 1차 결과 `m2/_v1/`(좁은 그리드), `m2/_v1_wide/`(넓힌 그리드)
- **쌍을 구조 있는 것만**: 시너지 = 원딜+서폿, 정글+미드, 정글+탑. 카운터 = 탑·정글·미드 맞대결 + 바텀 2대2 교차 4쌍 (1차 1만 8천 칸 → 승패 모델 시너지 2,332 · 카운터 2,643)
- **골드 모델 (맞라인 상성을 15분 골드로)**: 라인(탑·정글·미드·바텀=원딜+서폿) × 경기 1행, 결과 = 블루 − 레드 15분 라인 골드(천 단위), 정규 릿지. 칸 = 라인 챔피언 단독 + 맞대결 쌍 + 바텀 듀오, 통제 = 프로 수·Elo·숙련 + 출처×라인 절편. 솔랭 타임라인 15분 + 대회 OE `goldat15`(complete 만) → 105,276 라인 행
- **승패 모델**: 오프셋 = M1 교차 예측, 칸 = 챔피언 단독·패치 편차 + 구조 시너지 + 구조 카운터 + **G = 골드 모델의 쌍 몫 합**(라인 상성으로 예상되는 15분 골드 우세, 교차 예측)
- 설정 고르기 (26.14·26.15)
  - 골드: 쌍 칸이 **평가 라인의 설명력을 올림** — R² 대회 0.066 → 0.074, 솔랭 0.056 → 0.063 (챔피언 단독 규제 100, 쌍 규제 300) → **맞라인 상성은 15분 골드에서 보임**
  - 승패 (두 패치 평균 로그 손실): M1 만 대회 0.647 · 솔랭 0.694 → M1 + 챔피언 단독 **0.6406 · 0.6917** → 고른 설정(챔피언 100 · 시너지 없음 · 카운터 300 · G 있음) 0.6408 · 0.6914. 구조 시너지는 승패에 안 보여 규제 ∞ 로 골라짐
- 최종 결과
  - 15분 팀 골드 1,000 우세 = 로그 오즈 +0.456 (50% 근처 약 +11%p). G 계수 +0.265 → 골드 모델이 예상한 라인 상성 우세는 실제 골드 우세의 약 60% 만큼 승리로 이어짐
  - 맞라인 상성 크기 (15분 골드, |값| 중앙값 / 상위 1%): 탑 28 / 145, 정글 15 / 83, 미드 19 / 105, 바텀 31 / 150. 승률 환산 상위 1% ≈ 1~2%p. 예: 나르 → 크산테 +207, 암베사 → 레넥톤 −244, 갈리오 → 사일러스 +187, 노틸러스 → 렐 −242
  - 바텀 듀오 (15분 바텀 골드): 자야+라칸 +227, 루시안+나미 +171 / 이즈리얼+니코 −143
  - 라인 챔피언 단독 (15분 라인 골드): 키아나 정글 +807, 제이스 탑 +626 / 쉔 탑 −673, 유미 −618. **골드를 덜 먹는 성향(쉔 궁 합류, 유미)도 음수로 나오므로 "라인전 약함"과 같지 않음**
- **결론**: 챔피언 쌍의 상성은 **승패로는 거의 안 보이고 15분 골드로는 보임**. 라인 상성 하나가 승률에 주는 몫은 큰 것도 1~2%p 라 승패 예측 개선은 미미. 예측은 M1 + 챔피언 단독이 대부분을 함. 시너지(같은 팀 쌍)는 바텀 듀오 골드에서만 보이고 승패에는 안 보임
- M5 에서 쓰는 법 (안): 상대 픽에 대한 카운터 = 골드 모델 맞대결 값 × 0.456 (승률 환산) + 승패 카운터 칸. 근거 칸에 "15분 골드 ±N, 판수" 표시
- 산출: `m2/m2_eval.md`, `m2_lane_counter.csv`(맞라인 골드 상성, `win_pp` = 승률 환산), `m2_bot_duo.csv`, `m2_lane_champion.csv`, `m2_champion.csv`, `m2_champion_patch.csv`, `m2_synergy.csv`, `m2_counter.csv`, `m2_gold_selection.csv`, `m2_selection.csv`, `m2_games.csv`(`m2_pair_gold`, `m2_pred_oof`), `m2_model.pkl`
- 다음 (선택): 판수를 늘리면 골드 모델 상성이 더 많이 잡힘 → 챌린저 300명 일반 유저 솔랭 수집(새 경기 5~6만, 개발 키로 약 40시간) 검토

#### M2 3차 — 정글 듀오 골드 + 교전 단위 시너지 (2026-11-06)

```bash
python src/pipeline/m2_fights.py              # 교전 표 (캐시 m2/m2_fights.pkl, 약 30초)
python src/pipeline/m2_synergy.py --boot 30   # 약 7분
```

- 질문: 리 신 + 오리아나처럼 **같은 팀 조합 시너지**를 승패 말고 다른 결과로 볼 수 있나 (2차까지 승패로는 안 보임)
- 추가 ① **정글 + 미드, 정글 + 탑 듀오 15분 골드**: 골드 모델의 정글 행과 미드(탑) 행에 같은 쌍 칸 (계수 × 2 = 두 라인 합 효과), 규제 따로 고름
- 추가 ② **교전 모델** (`m2_fights.py`): 솔랭 킬 기록(`timeline_killdmg`)을 15초 묶음으로 → 킬 2개 이상 · 킬 수가 갈린 교전 255,009건 (27,166경기, 경기당 약 9.4). 결과 = 블루가 그 교전 킬을 더 냄. 칸 = 챔피언 단독 + 같은 팀 모든 쌍 (경기 조합), 통제 = 교전 시점 팀 골드 차이(10·15·20·25분 선형 보간), 시간대, 프로 수. 쌍 몫 합 F 를 승패 모델 칸으로
- 설정 고르기 (26.14·26.15)
  - 골드: **정글 듀오가 평가 골드 설명력을 올림** — 같은 설정에서 정글 듀오 없음 → 있음(규제 1000): R² 대회 0.064 → **0.074**, 솔랭 0.055 → **0.063**
  - 교전: 같은 팀 쌍을 넣어도 평가 교전 로그 손실이 그대로 (0.59649 = 쌍 없음) → **교전 결과에서는 조합 시너지가 안 보임** (골드 상황 통제 후, 솔랭)
  - 승패: 고른 설정 = M1 + 챔피언 단독 (시너지 ∞ · 카운터 ∞ · G 없음 · F 계수 0). 2차와 달리 G(정글 듀오 포함)는 대회 평가 경기에서 오히려 나빠짐 (0.6406 → 0.6425, 솔랭은 0.6917 → 0.6910)
- 최종 예: 정글 + 미드 리 신 + 오리아나 **+142** (670판), 리 신 + 라이즈 +131. 교전 모델 리 신 쌍은 대부분 0 근처
- 교차 예측 누수 수정: 최종 5묶음 교차 예측에서 전체 자료 해를 시작점으로 쓰던 것을 0 에서 출발로 (누수 때 솔랭 0.682 → 수정 후 0.689)
- **M2 종합 결론**
  - 쌍 효과는 **15분 골드에서만 보임**: 맞라인 상성, 바텀 듀오, 정글 + 미드·탑 듀오. 크기는 강한 것도 ±150~250 골드 (승률 환산 1~3%p)
  - **승패·교전 결과로는 쌍 효과가 안 보임** → 이길 가치(승률)는 M1 + 챔피언 단독 효과로 계산하고, 쌍 골드 값은 M5 근거 칸("15분 골드 ±N, 판수")으로 보여 줌. 승률로 환산한 값은 참고 표시만
  - 판수를 늘리면(챌린저 일반 유저 수집) 골드 쌍 표는 촘촘해지지만 승패 예측 개선은 작을 것
- 산출 추가: `m2_jungle_duo.csv`, `m2_fight_synergy.csv`, `m2_fight_champion.csv`, `m2_fight_selection.csv`, `m2_games.csv`(`m2_fight_score`). 2차 결과 `m2/_v2/`

#### M2 조합 찾기 — 인식(프로 밴픽) × 효과(솔랭) (`src/pipeline/m2_combos.py`, 2026-11-06)

```bash
python src/pipeline/m2_combos.py                # 약 1분 (솔랭 이벤트 캐시 m2/m2_combo_events.pkl 이 있을 때)
python src/pipeline/m2_combos.py --report-only  # 저장된 표로 포지션 쌍별 보고서만 다시
```

- 목적: 리 신 + 아리, 카밀 + 갈리오, 애쉬 + 세라핀 같은 조합을 **자연어 처리 없이 밴픽 자료로** 찾고, 실제 효과가 있는지 확인. 같은 팀 포지션 쌍 10가지 전부 (탑·정글, 정글·미드, 원딜·서폿, 탑·미드 …)
- **[1] 인식 (OE 5리그 팀-경기 4,110)**: 동시 픽 lift (패치별 픽률 곱 대비, 축소), 같은 차례 비율 (두 장 동시 픽, 같은 포지션 쌍 대비), 밴 반응 lift (상대가 1페이즈에 A → 2페이즈에 B 밴)
  - 상위: 칼리스타 + 레나타 13.0배, 자야 + 라칸 9.3, 루시안 + 밀리오 8.1, 애쉬 + 세라핀 7.8 (밴 반응 3.2), 아펠리오스 + 쓰레쉬 7.8 (밴 반응 9.6), 마오카이 + 요네 4.2, 럼블 + 요네 3.5, 탈리야 + 바이 2.3
  - **시간 검증**: 26.13 까지 '인식' 쌍 88개가 26.14·26.15 대회에서도 우연의 1.96배로 같이 뽑힘 (나머지 1.06배), lift 순위 상관 0.27 → **M4 상대 예측 입력으로 쓸 수 있음**
- **[2] 효과 (솔랭 교전 255,478 · 킬 143만)**
  - ① 공동 딜 lift (같은 희생자에 같이 딜, 같은 포지션 쌍 대비): 경기 반분 순위 상관 **0.65** → 재현됨. "같이 노는 정도" (가치와는 다름)
  - ② 경기 안 교전 비교 (둘 다 낀 교전 vs 하나만, 경기×쌍 고정효과, 포지션 쌍 평균 대비): 반분 순위 상관 **0.02** → **재현 안 됨, 분류에 쓰지 않음** (참고 칸만). 낌을 딜·죽음으로만 판정하는 한계 + 솔랭 노이즈
- **[4] 분류** (효과 = M2 3차 15분 듀오 골드 구간 > 0, 평가 패치 검증된 값): 검증된 콤보(골드) 13 · 인식 · 같이 놂 11 · 인식만 117 · 인식 · 골드 손해 4 · 인식(솔랭 근거 부족) 50 · 숨은 콤보(골드) 132
  - 검증된 콤보: 자야 + 라칸 (+227), 루시안 + 나미 (+171), 루시안 + 밀리오 (+86), 바드 + 케이틀린 (+98), 리 신 + 라이즈 (+131)
  - 요청 예: 리 신 + 아리 = 프로가 오히려 덜 묶음 (lift 0.6), 듀오 골드 +71 · 카밀(서폿) + 갈리오 = 대회 11회 lift 1.8 · 밴 반응 2.7 (인식만) · 애쉬 + 세라핀 = 대회에서 가장 강하게 묶음(7.8배) · 듀오 골드 +101 이나 구간이 0 포함 → 인식만 · 리 신 + 오리아나 = 숨은 콤보(골드 +142, 대회 lift 0.8)
- 쓰는 법: [1] 인식 지표 → **M4** (상대가 A 를 뽑으면 B 확률↑, 두 장 동시 픽 예측). 듀오 골드·공동 딜 → **M5 근거 칸**. 승률 가치에는 넣지 않음 (M2 3차 결론)
- 산출: `m2/combos/m2_combo_eval.md` (분류별 + 포지션 쌍별 표), `m2_combo_table.csv`, `m2_combo_draft.csv`, `m2_combo_joint.csv`, `m2_combo_fight.csv`
- 다음 (안): 교전 효과를 다시 보려면 "낌"을 위치 기반(타임라인 1분 좌표, 교전 지점 반경)으로 정하거나 대회 이벤트 자료가 필요

#### 대회 이벤트 자료 확인 (2026-11-06) — 쓰지 않기로

- 목적: 조합 교전 효과를 대회 경기로 보기 위한 경기 중 이벤트 (킬·어시스트 시점, 위치)
- LoL Esports 라이브 통계 피드 (`feed.lolesports.com`, 경기 목록 `esports-api.lolesports.com`): 공식 문서가 없는 비공개 API. robots.txt 요청에도 **403 "Missing Authentication Token"** → CLAUDE.md 1-1 대로 두 도메인 **전체 중단**, `research/raw/blocked.csv` 기록
- 라이엇 약관 (Terms of Service, 2024-12-01): 7.1-12 "공개되지 않은 영역 접근 시도" 금지, 7.1-11 허가 없는 봇·스크립트 금지. 개발자 정책 (2025-05-29): "Products should use supported services from Riot Games for data ingestion". 공식 개발자 API 에는 대회 자료가 없음
- 공식 경로는 라이엇 공식 데이터 파트너 (GRID) 유료 라이선스뿐 (구단·방송사 대상) → 개인 프로젝트로는 어려움
- 결론: 대회 이벤트 자료는 쓰지 않음. 조합 효과는 솔랭 이벤트 + 대회 요약 지표(OE 15분 골드·첫 용 등)로만 보고, 교전 참여를 다시 보려면 솔랭 타임라인 위치 기반 판정으로

#### 조합 교전 효과 — 위치 기반 2차 (2026-11-06)

- 바꾼 것 (`m2_combos.py` 기본, 1차는 `--dmg`): timeline_lite 킬 기록의 **킬 위치(x, y)와 어시스트**를 씀
  - 교전 = 15초 안 **그리고 교전 중심에서 2,500 안** (같은 시각 다른 라인 싸움은 따로) → 교전 190,916 (1차 255,478)
  - 낌 = 킬러 · 어시스트(라이엇 기준: 딜·CC·보호막 등) · 딜 준 사람 · 희생자 (1차는 딜·죽음만)
  - 1분 단위 선수 위치는 저장해 두지 않았고, 라이엇 타임라인도 1분 간격이라 몇 초짜리 교전 판정에는 너무 성김 → 쓰지 않음
- 결과: 경기 안 교전 비교 반분 순위 상관 0.023 → **0.098** (조금 나아졌으나 여전히 재현 안 됨). 공동 딜 0.653 → 0.678
- 크기 점검 (반분, 쌍별): 팀-경기 30 이상 쌍의 차이 값 표준편차 5.2%p vs 표준오차 중앙값 4.5%p → **쌍마다 진짜 차이는 약 2~3%p 이하**, 쌍 하나의 표준오차(2~4.5%p)에 묻힘. 400판 이상 쌍(4개)만 반분 상관 0.4
- 결론: 방법보다 **판수 부족**. 쌍 하나를 1%p 정밀도로 보려면 지금의 약 4~10배 판수 필요 → 교전 효과는 분류에 계속 쓰지 않음. 챌린저 일반 유저 수집(새 경기 5~6만)으로 2~3배까지는 늘릴 수 있음
- 1차 결과 `m2/combos/_v1_dmg/`, 이벤트 캐시 `m2/m2_combo_events_loc.pkl`
