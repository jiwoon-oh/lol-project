# 인수인계 — T1 밴픽 전력분석 시스템 (2026-11-11 통합본)

이 문서 하나로 다른 계정·다른 PC·다른 사람이 이어 갈 수 있게 정리했습니다. 예전 인수인계 문서 네 개(`HANDOFF.md` 스카우팅 리포트판, `docs/M5_HANDOFF.md`, `HANDOFF_NEXT.md`, `HANDOFF_2026-11-09.md`)를 합친 것입니다.

| 알고 싶은 것 | 문서 |
|---|---|
| 프로젝트 소개·결과·방법론 | [`README.md`](README.md) |
| 규칙 (Claude Code가 자동으로 읽음) | [`CLAUDE.md`](CLAUDE.md) |
| 모델별 실험과 모든 수치 | [`docs/PIPELINE.md`](docs/PIPELINE.md) 7-7장 (M0~M5 1~12단계) |
| 날짜별 결정과 이유 | [`docs/decisions.md`](docs/decisions.md) |
| 딥리서치 전 과정 | [`docs/RESEARCH_PIPELINE.md`](docs/RESEARCH_PIPELINE.md) |
| 리서치가 토큰 한도에 걸렸을 때 이어 하기 | [`docs/runbook.md`](docs/runbook.md) |

---

## 1. 지금 상태

| 영역 | 상태 |
|---|---|
| 딥리서치 | **완료**. 선수 58 + 팀 10 프로필 확정, 주장 10,497개, 인용 통과율 94.0%, 사람 표본 주장 98.4% · 귀속 94.9%. 주장 ↔ 경기 기록 대조까지 (2026-11-08) |
| 밴픽 모델 M0~M4 | **완료**. M4는 `data/processed/2026/m4/final/`에 고정, 플레이오프 55경기 1회 검증 (픽 top5 63.5% · 밴 64.7%). **다시 바꾸지 않음** |
| M5 추천 | **완료**. 확률 보정, 시간대 경로 이길 가치, 몬테카를로, 상태 입력 특징, 재생 검증, 1수 앞보기, 밴 기준, 피어리스 시리즈, 팀 실력 차, 두 장 동시 픽, 추천 검증(학습 밖 301경기), 쏠림 동점권 |
| 밴픽 대시보드 | **완료** (로컬 서버, 실제 경기 대조 탭 포함) |
| 스카우팅 리포트 | 선수 페이지 완료, **팀 페이지 개편은 아직** |
| 결과 시각화 | claude.ai 비공개 아티팩트 "T1 밴픽 모델 성적표" (2026-11-10, 저장소 밖, 5장) |
| 모델 개발 | **여기서 멈춤** (2026-11-11 판단: 더 다듬어도 성능이 크게 오르지 않을 것으로 봄. 조합 쪽 시험은 4장대로 사실상 다 봤음) |

---

## 2. 새 환경에서 처음 할 일

1. **폴더를 통째로 옮기기.** git에는 코드·문서·설정만 있습니다. `data/`(경기 자료·모델 결과)와 `research/`(원문·주장·프로필)는 `.gitignore`로 빠져 있어 `git clone`만으로는 아무것도 못 돌립니다. **원문이 들어 있으니 공개 저장소·공유 링크에 올리지 않습니다.**
2. **파이썬.** 모델은 `lightgbm`이 필요합니다. 이 PC에서는 MS Store 파이썬 3.12에만 깔려 있고 anaconda 쪽에는 없습니다.
   ```bash
   python -c "import lightgbm, sys; print(sys.executable)"
   ```
3. **`.claude/launch.json`** (git 제외라 새로 만들어야 할 수 있음) — 앱 브라우저에서 화면을 띄우는 설정.
   ```json
   {"version": "0.0.1", "configurations": [
     {"name": "dashboard", "runtimeExecutable": "python", "runtimeArgs": ["-m", "http.server", "8765", "--bind", "127.0.0.1", "--directory", "dashboard"], "port": 8765},
     {"name": "draft", "runtimeExecutable": "<lightgbm 이 있는 python.exe 경로>", "runtimeArgs": ["dashboard/draft_server.py", "--port", "8770"], "port": 8770}
   ]}
   ```
4. **`.env`** — 키는 이 파일에만 둡니다(`.gitignore`에 있음). 형식은 `.env.example` (`RIOT_API_KEY`, `LP_USER`·`LP_PASS`, `ANTHROPIC_API_KEY`, `NAVER_CLIENT_ID`·`NAVER_CLIENT_SECRET`, `YT_API_KEY`). **모델·대시보드 실행에는 필요 없고** 새로 수집할 때만 씁니다. Riot 개발 키는 24시간마다 재발급.
5. **리서치 에이전트를 다시 띄울 때만**: 바꾸기 전 옛 계정에서 `python src/research/runctl.py backfill` → 새 계정에서 `research/config/budget.yaml`의 `run.account`를 `a2`로. 누적 토큰 2,966만 / 상한 3,300만이라 상한도 올려야 함 (사용자 결정).
6. **아래 3장의 "메모리로만 있던 규칙"을 새 Claude에게 알려 주기** (메모리는 계정·PC별이라 넘어가지 않음).

새 Claude에게 줄 첫 지시 (예시)
```
HANDOFF.md, CLAUDE.md, README.md 를 읽고 프로젝트 상태를 파악해줘.
커밋·푸시는 내가 말할 때까지 하지 말고, 커밋할 땐 네가 바꾼 파일만 골라서 목록을 먼저 보여줘.
플레이오프(08-26~) 자료로는 아무것도 조정하지 마. 다음 할 일은 HANDOFF.md 7장에서 고를게.
```

---

## 3. 꼭 지킬 규칙

### 메모리로만 있던 규칙
- **커밋·푸시는 사용자가 말할 때만** (2026-11-07 "앞으로는 내가 말할때까지 푸쉬도 커밋도 하지마"). 커밋할 땐 `git add -A` 금지, 내가 바꾼 파일만 골라 목록을 먼저 보여 줌.
- **S6 프로필 형식** = 사용자 예시 `profile_Oner.md` 틀(플레이스타일 · 챔피언·픽 · 폼 · 약점 · 밴픽 시사점, 표에 근거 `[#n]`·기록 대조·신뢰도) + **"커뮤니티 시각" 칸 하나 추가**. 커뮤니티 등급은 그 칸 안에서 구분.
- **새 시험은 돌리기 전에 판단 기준을 `docs/decisions.md`에 먼저 적고** 그 기준대로 판단 (2026-11-09).

### 자료·모델 규칙
- **자료는 2026-08-25까지.** 08-26 이후(플레이인·플레이오프)는 M4 최종 검증에 한 번 썼고, 결과를 보고 아무것도 조정하지 않음. 다시 보거나 튜닝에 쓰지 않음.
- 설정은 리허설(LCK 26.14·26.15·RTM)로 고르고, 새로 확인할 때는 리허설에 안 쓴 패치(26.07~26.10, 26.16)를 씀.
- `m4_validate.py --run --once`는 이미 돌렸음. `m4/final/` 고정 모델을 바꾸지 않음.
- 리서치: robots.txt 준수, 차단 신호면 그 도메인 중단(`research/raw/blocked.csv`), 우회 도구 금지, 원문 공개 금지, `extract_claims.py prepare` 다시 하지 않음 (CLAUDE.md 1장).

### 공개하면 안 되는 것
- `.env` (API 키), `data/`, `research/`의 `raw/`·`samples/`·`claims/`·`verified/`·`reviews/`·`profiles/*_claims_used.csv`·`profiles/work/` — 기사 본문·커뮤니티 글·댓글·인용이 들어 있음.
- 스카우팅 리포트 결과물(`dashboard/data.json`, `report.html`, `index.html`)에도 프로필 인용이 들어 있음 → 공개하려면 인용을 뺀 판을 따로 만들어야 함. (그래서 `.gitignore`에 있음)
- 동료 작업 zip(`Downloads/lol-project.zip`)에는 `.env`와 `research/` 원문이 들어 있음 → 공유 조심.

---

## 4. 무엇을 알게 됐나 (모델 결론)

| 시험 | 승패 예측에 도움? |
|---|---|
| 상대 픽 확률 (M4 + 굴리기) | **맞음** — 플레이오프 top5 63.5%, 리허설 밖 재생에서 "70% 이상"이 실제 67% |
| 픽 추천 ↔ 승패 (학습 밖 301경기) | 같은 방향 — 추천에서 벗어난 만큼 더 짐 (기울기 −0.70 [−1.35, −0.11]). 관찰 자료라 인과는 아님 |
| 이길 가치 = 시간대 골드 경로 | 가장 좋음 (평가 패치 5개 로그 손실 0.6285) |
| M1 조합 성격 / 조합 비가산 몫 | 없음 (가중치 ≈ 0 / 오히려 나빠짐) |
| M2 시너지·듀오, 맞라인 상성, 속성 기반 상성 | 15분 골드에서만 보이고 승패는 나빠짐 → 근거 칩으로만 |
| 선수 × 챔피언 숙련 δ | 승패는 그대로, 패치별 배율 k 모두 양수 → × 0.32만 반영 |
| 밴 가치 (상대 조합 가치 감소) | **근거 약함** (기울기 −1.40 [−3.71, +0.61]) → 대시보드에 "검증 미통과" 표시 |

설계에 반영된 관찰
1. **프로 픽은 "이기는 픽"보다 메타·숙련·상대 반응으로 설명됨** — 그래서 "고를 확률"(M4)과 "이길 가치"(추천)를 분리.
2. **같은 시리즈 앞 세트 정보가 큼** — 2세트 이상 밴의 39.5%가 같은 팀의 앞 세트 밴. 입력에 앞 세트 밴·픽·승패를 꼭 받음.
3. **강팀(Gen.G·한화)은 예측이 어려움** (픽 top5 54~58%) — 화면에 확률을 보여 주고 상위 5 밖 선택도 흔하다는 걸 표시.
4. **2밴 페이즈는 반응형** (상대 빈 포지션, 우리 픽 응수 막기, 조합 완성 막기).
5. **26.16은 LCK 경기가 적은 패치** — 같은 패치의 LEC·LCS(4~7일 먼저 시작)가 메타 신호.
6. **추천 쏠림**: 이길 가치가 챔피언 한 장 값의 합이라 애쉬·럼블·신드라 같은 챔피언에 몰림. 동점권 규칙으로 상위 5 비중 39.0 → 35.8%만 줄었음.

---

## 5. 무엇이 어디 있나

### 코드
| 영역 | 파일 | 역할 |
|---|---|---|
| 자료 수집 | `src/pipeline/a_*`~`i_*` | OE·Leaguepedia·패치노트·Data Dragon·솔랭·팀원 메타 (PIPELINE 1~6장) |
| M0~M3 | `m0_attributes.py`, `m1_comp.py`, `m2_synergy.py`·`m2_combos.py`, `m3_paths.py` | 챔피언 속성 → 조합 승패 → 시너지·카운터 → 이기는 경로 |
| M4 | `m4_*.py` (시험 v1~v7), `m4_freeze.py`, `m4_validate.py` | 상대 예측. 고정 모델 `data/processed/2026/m4/final/` |
| M5 공용 | `m5_state.py` | 드래프트 상태 → 남은 단계 M4 행(유령 기록) + 굴리기 엔진 |
| M5 | `m5_calibrate.py`, `m5_value.py`·`m5_value_w.py`, `m5_skill.py` | 확률 온도 τ, 이길 가치 표, 숙련 편차 δ |
| M5 | `m5_sim.py` | 시험판·추천 계산 (1수 앞보기, 밴 기준, 피어리스, 동점권 `PICK_TIE_Z`) |
| M5 검증 | `m5_replay.py`, `m5_rec_check.py`, `m5_ban_check.py`, `m5_rec_diverse.py`, `m5_ctx.py`, `m5_counter.py` | 재생 검증, 추천·밴 검증, 쏠림, 조합 맥락·상성 시험 |
| 밴픽 대시보드 | `src/pipeline/m5_draft.py` (엔진), `dashboard/draft_server.py` (서버), `dashboard/draft.html` (화면) | 6장 |
| 스카우팅 리포트 | `dashboard/build.py`, `template.html`, `cover_template.html`, `career.py` | 6장 |
| 리서치 | `src/research/*`, `record_check.py` | S1~S8, 주장 ↔ 기록 대조 |

### 결과 (git 제외)
| 무엇 | 위치 |
|---|---|
| 고정 모델·최종 검증 | `data/processed/2026/m4/final/`, `m4/validation/` |
| M5 표·보고서 | `data/processed/2026/m5/` (`calibration.json`, `m5_value_champion.csv`, `m5_skill.csv`, `m5_replay*.md`, `m5_ctx.md`) |
| 추천·밴 검증 | `data/processed/2026/m5/rec_check/` (`m5_rec_check.md`, `m5_ban_check.md`, `m5_rec_diverse.md`, 대시보드용 `replay.json`, 첫 실행 `v1/`) |
| 리서치 프로필·평가 | `research/profiles/{대상}.md`, `research/eval/summary.md`, `record_check.md` |
| 결과 시각화 | https://claude.ai/artifact/WLyz8nBhyEwiywTH8tkvk6 — 집계 숫자만 담은 비공개 아티팩트(원문 없음). 지금은 소유자만 열 수 있음, 공유는 페이지 Share 메뉴에서. 모델을 다시 돌려도 자동으로 안 바뀜 → 고칠 땐 아티팩트를 읽어 와 그 위에 수정 |

---

## 6. 화면 두 가지

### 6-1. 밴픽 대시보드
```bash
python dashboard/draft_server.py --port 8770   # lightgbm 있는 파이썬, 시작 약 20초 → http://localhost:8770
```
- 시작 화면: 상대 팀, T1 선픽/후픽, 블루/레드 선택. 또는 **실제 경기 대조 탭** (학습 밖 301경기, T1 64경기 — 단계마다 실제 선택 vs 그 시점 추천, 경기 전 예상, 놓친 몫, 실제 승패. 픽만, 밴은 대조 안 함).
- 위: 중계 화면식 밴픽 판. 아래: 상대 차례 = M4 예측 상위 8 (자동 진행 또는 직접 입력), T1 차례 = 추천 카드(예상 승률, 조합·뺏기·응수, 상대 다음 수, 근거 칩, 동점권 표시) + 챔피언 선택판.
- 연달아 두 번 고르는 차례(8·9, 10·11, 18·19)에는 두 장 묶음 추천, 세트 끝나면 피어리스 다음 세트(진 팀 선픽·진영 바꿈 가정), 되돌리기.
- 예상 승률 = 진영 0.093 + 2.32 × (T1 Elo − 상대 Elo)/400 + 챔피언 이길 가치 합 + 숙련 δ × 0.32. 밴 순위는 "검증 미통과" 안내가 붙음.
- 계산: 밴 ~7초, 픽 ~4초. 한 사람용 로컬 서버. 챔피언 그림은 브라우저가 Data Dragon에서 불러옴(인터넷 필요).
- 가상 경기일 2026-08-26, 라인업 = 08-25 이전 마지막 LCK 경기, 패치 26.16.

### 6-2. 스카우팅 리포트 (선수·팀 페이지)
```bash
python dashboard/build.py    # → dashboard/data.json, report.html, index.html(표지), 몇 초
```
- 표지(기본 T1): 팀 이름 + 08-25 기준 주전 5명 카드 + 팀 요약·운영 지표 → LCK 정규시즌 순위 → 선수 카드를 누르면 풋볼매니저식 프로필 (사진·커리어, 방사형 차트, 장점/단점·신뢰도, 지표 9칸, 챔피언 폭, 밴픽 시사점). 주소 `#team-T1`, `#player-Faker`.
- 장점/단점 나누기는 `template.html`의 `NEG`·`MIX` 정규식과 능력치 점수(14 이상/6 이하). 팀 색은 `THEMES`.
- `build.py`가 읽는 것: `research/config/players.yaml`·`teams.yaml`, `research/profiles/{대상}.md`·`stats/*.json`·`team_stats/*.json`, `data/processed/2026/a_player_games.csv`·`f_match_schedule.csv`·`f_rosters.csv`, `dashboard/career/careers.json`(커리어·사진 주소, `career.py`로 Leaguepedia에서 받음)·`champ_ko.json`·`team_logos.json`, `research/eval/per_target.csv`.
- 사진·로고는 Leaguepedia 주소를 브라우저가 불러옴(코드로 받으면 403 → 도메인 중단). 사진을 파일로 넣으려면 사람이 직접 저장해 `dashboard/photos/{선수}.jpg`.
- 시즌 중 이적 5명(Aiming, Jiwoo, Diable, Taeyoon, Sharvel)은 08-25 소속 기준(기록표 `team` 칸은 이적 전 팀이 남아 있지만 수치는 두 팀 합산).
- `dashboard/player_sample*.py/.html`, `career/Doran.csv`는 레이아웃을 정할 때 쓴 도란 샘플(참고용).

---

## 7. 남은 일 (사용자 결정 대기, 시작한 것 없음)

모델 성능 개선은 멈추기로 했으므로 아래는 주로 정리·공개·검증 쪽입니다.

| # | 할 일 | 메모 |
|---|---|---|
| 1 | 스카우팅 리포트 팀 페이지 개편 | 선수 페이지처럼 (11-05부터 남아 있음) |
| 2 | 포트폴리오용 공개판 | 인용 뺀 프로필 샘플, 사례 페이지, 대시보드 녹화. 결과 시각화 아티팩트는 원문이 없어 공유 후보 |
| 3 | 동료 작업과 합칠지 | 5-1 참고 |
| 4 | (검증, 선택) 2026 LPL·LEC·LCS 플레이오프로 학습 밖 확인 | 한 번도 안 쓴 자료. M4는 LCK만 학습했으므로 쓸 수 있음 |
| 5 | (검증, 선택) 리서치 재현율·프로필 요약 충실도 | 원문 표본에 사람이 먼저 표시 → S4와 비교 |
| 6 | (선택) 높은 확률 과신 | 50~70%라 한 것의 실제 비율 44~51% |
| 7 | (선택) 대시보드 다듬기 | 포지션 직접 지정, 계산 대기 줄이기 |

보류: 팀 × 조합 성격 상호작용(표본이 작아 실패 가능성 큼), 측면 라벨링 100건, v9 비교 실험, 챌린저 일반 유저 솔랭 수집.

### 7-1. 동료 작업과의 차이 (2026-11-09, 읽기만 함)
동료 작업(`Downloads/lol-project.zip`)과 우리는 **M4 고정 커밋 `8447c39`에서 갈라져 M5를 각자 만들었습니다.** M4 고정 모델 파일 4개는 같습니다.

| | 우리 | 동료 |
|---|---|---|
| 이길 가치 | 시간대 골드 경로(M1 없음) + 숙련 δ × 0.32 | M1 + M2 챔피언 단독 + Elo |
| 남은 드래프트 | 끝까지 몬테카를로 + 1수 앞보기 | 1수 앞보기 + 티어 기댓값 |
| 검증 | 학습 밖 정규시즌·RTM 301경기 | 플레이오프 55경기 재사용 (우리 규칙과 어긋남) |

- 동료만 한 것: M4 보강 시험 5종(모두 미채택), 발표 화면 `m5_play_server.py`, 클라우드 배포(Render), 명령줄 `m5_live.py`, 자동 점검 `m5_check.py`(진영 대칭·확률 합·1수 앞보기 일관성·날짜 범위 — 레드 진영 Elo 부호 버그를 이걸로 잡았음).
- 가져올 만한 것: 자동 점검 스크립트, 추천을 움직이는 요인 진단(1순위 몫 평균, 1수 앞보기가 1순위를 바꾼 비율), 시도 기록부.
- 합칠 때 부딪히는 곳: `src/pipeline/m5_calibrate.py`(이름 같고 내용 완전히 다름), `docs/decisions.md`·`PIPELINE.md`(양쪽이 각자 덧붙임), 대시보드 포트 8770, 동료 `.gitignore`의 `deploy/`.

---

## 8. 실행 명령

```bash
python dashboard/draft_server.py --port 8770               # 밴픽 대시보드
python dashboard/build.py                                  # 스카우팅 리포트
python src/pipeline/m5_sim.py --opp Gen.G --opp-first --sets 2   # 가상 드래프트 보고서 (1~3분)
python src/pipeline/m5_state.py --parity                   # 상태 입력 특징 = 학습 표 점검 (약 1분)
python src/pipeline/m5_replay.py --sets 26.08,26.09,26.10  # 재생 검증 (약 20분, --report 로 보고서만)
python src/pipeline/m5_rec_check.py                        # 추천 검증 (약 15분, raw_*.pkl 있으면 그 세트 건너뜀, --report)
python src/pipeline/m5_ban_check.py                        # 밴 검증 (약 25분)
python src/pipeline/m5_rec_diverse.py                      # 쏠림 비교 (몇 초)
python src/pipeline/m5_ctx.py                              # 조합 맥락 시험 (약 1분)
python src/pipeline/m5_value.py --reuse                    # 이길 가치 보고서만 (--reuse 없으면 수십 분)
python src/pipeline/m5_value_w.py                          # 최종 이길 가치 표 (약 1.5분, 예전 E-C 표는 _ec 로 보관)
python src/research/record_check.py                        # 리서치 주장 ↔ 경기 기록 대조
python src/research/runctl.py status                       # 리서치 상태 (토큰 안 씀)
```

---

## 9. 알아 둘 함정

- **패치 칸은 문자열로 읽기.** 숫자로 읽으면 `26.10` → `26.1`. LCK 드래프트 표(`m4_v6_rows.pkl`의 `Patch`)에는 실제로 `"26.1"`로 남아 있음.
- **한글 파일명**은 NFD로 저장돼 있을 수 있음 → 비교할 땐 `unicodedata.normalize("NFC", ...)`.
- **OE CSV는 하나만** `data/inven_2026/games/`에 (두 개면 경기 중복).
- `m4_v1.build_rows`는 2026-11-08에 바뀜: 유령 기록(`ghost` 칸) 지원, 픽 차례를 단계 번호로. 실제 경기는 같은 값이라 M4 학습 결과는 그대로.
- `research/logs/s5_summary.csv`는 마지막 대상만 담음 → 대상별 수치는 `research/eval/per_target.csv`.
- 이전 계정의 Claude 대화 기록·메모리는 그 계정·PC의 `~/.claude/projects/`에만 있음.
