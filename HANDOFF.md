# 인수인계 — LCK 2026 선수·팀 프로필 리서치 + T1 스카우팅 대시보드

작성 2026-11-05, 수정 2026-11-06 (선수 페이지 새 레이아웃·커리어·사진). 이 압축본(`handoff.zip`)만으로 **대시보드를 다시 만들고 고칠 수 있게** 묶었습니다. 리서치 파이프라인 코드와 문서도 같이 들어 있지만, 원문(기사 본문·커뮤니티 글·댓글·인용)은 빠져 있습니다 (아래 "빠진 것").

## 1. 먼저 읽을 것

| 순서 | 파일 | 내용 |
|---|---|---|
| 1 | 이 문서 | 무엇이 들어 있고 대시보드를 어떻게 만드는지 |
| 2 | `CLAUDE.md` | 리서치 전체 인수인계 (원칙·기간·출처 등급·파이프라인 S1~S8·진행 상태) |
| 3 | `docs/decisions.md` | 주요 결정과 이유 (맨 아래가 최신) |
| 4 | `docs/PIPELINE.md` | 밴픽 데이터 파이프라인(A~G) + **7장 밴픽 추천 시스템 설계** |
| 5 | `docs/RESEARCH_PIPELINE.md` | 딥리서치 과정 전체: 단계별 입출력, 에이전트를 띄운 방식, 실행 기록, 수치 |

Claude Code를 쓴다면 압축을 푼 폴더에서 열면 `CLAUDE.md`를 자동으로 읽습니다.

## 2. 대시보드

- 결과물: `dashboard/report.html` (파일 하나, 데이터 내장. 표지는 `dashboard/index.html`. 브라우저로 바로 열림). **선수 사진은 Leaguepedia 주소에서 불러오므로 인터넷 연결이 필요** (끊겨 있으면 실루엣이 대신 나옴). claude.ai 아티팩트로는 올리지 않음 (아티팩트는 외부 이미지를 막음)
- 다시 만들기:
  ```bash
  pip install pandas pyyaml
  python dashboard/build.py
  ```
  → `dashboard/data.json`, `dashboard/report.html`, `dashboard/index.html`(표지) 을 새로 씀 (토큰 0, 몇 초)
- 화면 구성
  - 표지(기본 = T1): 팀 이름 크게 + 08-25 기준 주전 5명 카드(사진) + 팀 요약·운영 지표
  - 스크롤하면 LCK 정규시즌 순위 (Rounds 1–4, 08-25까지). 팀을 누르면 그 팀 표지
  - 선수 카드를 누르면 풋볼매니저식 프로필 (한 화면 + 스크롤 한 번 정도)
    - 왼쪽: 사진, 나이·포지션·국적, 경기·승률·KDA(2026 LCK 리서치 기간), 커리어(팀별 기간·경기·승률)
    - 가운데·오른쪽: 방사형 차트(선수 vs LCK 같은 포지션 리서치 대상 평균, 축 범위 = 그 선수들의 최소–최대) · 보고서(장점/단점, 신뢰도, `기록` 표시 = 경기 기록 기반) · 지표 설명 9칸(값·평균·순위·뜻) · 스카우트 요약 · 챔피언 폭 · 밴픽 시사점(추론)
    - 예전의 폼·출처 간 시각 차이 탭, 픽 순서·1페이즈 픽·킬 관여는 뺐음 (프로필 md에는 그대로 있음)
    - 장점/단점 나누기는 간단한 규칙 (`template.html` 의 `NEG`·`MIX` 정규식, 능력치 점수 14 이상/6 이하)
  - 팀마다 색이 다름 (`template.html` 의 `THEMES`)
  - 주소: `#team-T1`, `#team-GenG`, `#player-Faker` 처럼 바로 갈 수 있음

| 파일 | 역할 |
|---|---|
| `dashboard/build.py` | 데이터 뽑기 + HTML 조립. 프로필 md에서 요약·표 문장만 가져오고 `[#n]` 근거 번호는 지움 |
| `dashboard/template.html` | 화면 (CSS·JS). `__DATA__` 자리에 data.json이 들어감 |
| `dashboard/data.json` | 뽑은 데이터 (선수 58명, 팀 10개, 순위) |
| `dashboard/career.py` | 커리어·사진 주소를 Leaguepedia Cargo API로 받아 `dashboard/career/careers.json` 으로 저장 (`src/pipeline/leaguepedia.py` 사용, `.env` 의 LP_USER/LP_PASS 있으면 로그인). 다시 받을 때만 실행 |
| `dashboard/career/careers.json` | 선수별 커리어(팀·기간·경기·승률)와 사진 주소 |
| `dashboard/player_sample*.py/.html`, `career/Doran.csv` | 레이아웃을 정할 때 쓴 도란 샘플 (참고용) |

`build.py` 가 읽는 것

| 자료 | 위치 | 쓰는 곳 |
|---|---|---|
| 선수·팀 명단 | `research/config/players.yaml`, `teams.yaml` | 이름·포지션·본명 |
| 확정 프로필 | `research/profiles/{대상}.md` | 요약, 플레이스타일, 챔피언 평가, 약점, 밴픽 시사점, 폼 메모, 출처 간 차이 |
| 선수 기록표 | `research/profiles/stats/{선수}.json` | 능력치, 챔피언 폭, 패치별 폼 |
| 팀 기록표 | `research/profiles/team_stats/{팀}.json` | 블루/레드, 골드 차, 오브젝트, 픽·밴 |
| 경기 기록 | `data/processed/2026/a_player_games.csv` (Oracle's Elixir) | **08-25 기준 로스터**, 소속 이력 |
| 일정·로스터 | `data/processed/2026/f_match_schedule.csv`, `f_rosters.csv` (Leaguepedia) | 순위, 생년월일·국적 |
| 커리어·사진 | `dashboard/career/careers.json` (Leaguepedia) | 커리어 표, 사진 주소. `dashboard/photos/{선수}.png·jpg·webp` 가 있으면 그 파일을 페이지에 넣음 |
| OE 원본 | `data/inven_2026/games/*.csv` (하나만) | 정글 15분 킬+어시스트. 없거나 2개 이상이면 그 지표만 빠짐 |
| 리서치 패치 창 | `src/research/patch_windows.py` (`research/config/tournament_patches.csv`) | 능력치 계산 경기 범위 |
| 평가 집계 | `research/eval/per_target.csv` | 표지의 주장 수 |
| 챔피언 한글 이름 | `dashboard/career/champ_ko.json` (Data Dragon ko_KR) | 프로필 문장·챔피언 폭의 영어 이름 → 한글 |
| 팀 로고 주소 | `dashboard/career/team_logos.json` (Leaguepedia) | 표지·순위 로고 (주소만, 열 때 인터넷 필요) |
| 글꼴 | `dashboard/fonts/NoonnuBasicGothicRegular.ttf` | 표지·리포트 본문 글꼴 |

커리어 표 기준: Leaguepedia 공식 대회 전체(정규·플레이오프·국제전·KeSPA컵·CL), 2026-08-25까지. 국가대표·올스타 제외. 이름만 바뀐 같은 구단(SKT T1→T1, DAMWON→DWG KIA→Dplus Kia 등)은 이어지면 한 줄. 기간은 계약일이 아니라 첫·마지막 경기 달

### 아직 안 된 것 / 이어서 할 일
- **사진은 외부 주소**: Leaguepedia 사진 서버(`static.wikia.nocookie.net`)는 코드로 받으면 403 → `blocked.csv` 에 도메인 전체 중단으로 기록함 (다시 요청하지 않음). 사진을 파일로 넣고 싶으면 사람이 직접 저장해 `dashboard/photos/{선수 ID}.jpg` 로 두고 `build.py` 를 다시 돌리면 됨 (가로 300px 정도 권장)
- 선수 58명·팀 10개 페이지를 브라우저(1440px·375px 폭)로 열어 넘침 없음을 확인함 (2026-11-05)
- 팀 페이지는 표지 사진 말고는 예전 그대로 (선수 페이지 같은 개편은 아직)
- "리서치 대상 아님" 카드: 08-25 주전인데 10경기 미만이라 프로필이 없는 선수가 있으면 나옴 (지금은 없음. HLE Bluffing·DRX Minous는 출전이 적어 주전으로 잡히지 않음)
- 밴픽 추천 화면 (2026-11-08): `python dashboard/draft_server.py --port 8770` → http://localhost:8770 (모델을 돌려야 해서 파일 하나가 아니라 로컬 서버, 시작 약 20초). 상대 차례 자동(M4), T1 차례 추천 카드·두 장 동시 픽·피어리스 다음 세트. 설명은 `docs/PIPELINE.md` 7-7 "M5 8단계"

## 3. 지켜야 할 것 (CLAUDE.md 1장 요약)
- 프로필 문장은 "**이 출처들이 이 선수를 이렇게 본다**"이지 실제 기량이 아님. 화면에도 이 문구 유지
- 출처 등급(사실·본인·기자·커뮤니티 등)을 섞지 않음. 신뢰도 강함/보통/약함 유지
- 원문(기사·글·댓글 인용)은 개인 분석용 → **공개 저장소·공개 페이지에 올리지 않음**
- 기간: 리서치 = 2026-08-25까지. 08-26 이후(플레이오프)는 밴픽 모델 검증용이라 프로필·대시보드에 넣지 않음
- 시즌 중 이적 5명(Aiming, Jiwoo, Diable, Taeyoon, Sharvel)은 08-25 소속 기준. 기록표 `stats/*.json` 의 `team` 칸은 이적 전 팀이 남아 있음 (수치는 두 팀 합산이라 맞음)

## 4. 들어 있는 것

```
handoff/
├─ HANDOFF.md, CLAUDE.md, requirements.txt, .env.example
├─ docs/                     결정·재개 절차·계획·밴픽 파이프라인
├─ dashboard/                build.py, template.html, cover_template.html, data.json, index.html(표지), report.html, fonts/, career.py, career/, 도란 샘플
├─ research/config/          명단·별칭·검색어·기간·지시문
├─ research/profiles/        확정 프로필 68개 (.md) + stats/ + team_stats/
├─ research/eval/            품질 지표 (집계만)
├─ research/logs/STATUS.md   진행 상태 (자동 생성본)
├─ src/research/             리서치 파이프라인 코드 (S1~S8)
├─ src/pipeline/             밴픽 데이터 파이프라인 코드 (A~G)
└─ data/processed/2026/      a_player_games.csv, f_match_schedule.csv, f_rosters.csv
```

## 5. 빠진 것 (일부러)
- **원문·인용이 든 것**: `research/raw/`(기사 원문), `samples/`(커뮤니티 표본), `claims/`·`verified/`(인용 포함 주장), `reviews/`(검토표, 인용 포함), `profiles/*_claims_used.csv`(근거 번호별 인용·URL), `profiles/work/`(S6 입력), `*_draft.md`·`*_check.txt`
  → 프로필의 `[#n]` 근거를 직접 보려면 원래 폴더의 `*_claims_used.csv` 가 필요. 필요하면 따로 요청
- `data/` 원본(인벤·유튜브·OE 원본 CSV·솔랭), 그 밖의 `data/processed/` 파일 → 리서치 파이프라인을 처음부터 다시 돌릴 때만 필요
- `.env` (API 키) — 각자 `.env.example` 보고 만들 것
- 토큰 기록 `research/logs/usage.csv` 등
