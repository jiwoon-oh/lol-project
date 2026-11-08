# 실행 중단·재개 절차 (토큰이 떨어졌을 때)

> 2026-11-03 · 대상: 여론 리서치 파이프라인 v2 (`src/research/`)
> 핵심: **작업 단위 = 결과 파일 1개**. 끝난 건 파일로 남고, 남은 것만 다시 돌림. 처음부터 다시 하지 않음.

## 1. 평소 실행 규칙 (Claude가 지킴)

| 언제 | 할 일 |
|---|---|
| 하위 에이전트를 띄우기 **전** | `python runctl.py budget --next <예상 토큰>` — 종료 코드 2면 **띄우지 않고 멈춰서 사용자에게 보고** |
| 에이전트가 **끝날 때마다** | `python runctl.py usage --stage S4 --player <선수> --agent <agent_NN> --tokens <n> --seconds <s>` |
| 단계 사이 | `python runctl.py status` — 선수별 다음 할 일, `research/logs/run_state.json` 저장 |

- 상한은 `research/config/budget.yaml`의 `run.token_cap` (지금 3,300만, 멈춤 기준 90% = 2,970만. 1,500만 → 2,000만 → 3,300만 사용자 결정). 구독에 남은 양은 코드가 알 수 없어서 **직접 정한 상한**으로 멈춤
- 에이전트를 한꺼번에 많이 띄우면 한도에 닿았을 때 진행 중인 몫이 같이 날아감 → S4는 **도란 먼저**, 그다음 오너·페이커 병렬

## 2. 두 가지 "떨어짐"

| 경우 | 무슨 일이 생기나 | 어떻게 |
|---|---|---|
| **A. 내가 정한 상한에 닿음** (`budget`이 멈춤) | 새 에이전트를 띄우지 않음. 이미 끝난 결과는 그대로 | Claude가 멈추고 누적 토큰·남은 일을 보고 → 사용자가 상한을 올리거나 다음에 이어서 |
| **B. 구독 사용량 한도에 닿음** (앱이 막음) | 진행 중이던 하위 에이전트가 중간에 멈추거나 실패 → 그 에이전트의 결과 파일이 **없거나 깨짐**. 끝난 에이전트 결과는 남음. 메인 대화도 한도가 풀릴 때까지 멈춤 | 한도가 풀린 뒤 3장 순서로 이어하기 |

※ 멈춘 에이전트가 쓴 토큰은 돌려받지 못함 → 에이전트 1개 몫(묶음 8개, 약 96건)이 최대 손실. 손실을 더 줄이려면 `budget.yaml`의 `s4.batches_per_agent`를 낮춤 (에이전트 수가 늘어 총 토큰은 조금 늘어남)

## 3. 이어하기 (한도가 풀린 뒤)

1. 같은 대화 또는 새 대화에서 **"이어서 해줘"** — Claude가 CLAUDE.md를 읽고 아래를 함
2. `python runctl.py status` → 선수별 `next` 확인
3. `next`대로만 진행:

| status 표시 | 할 일 | 주의 |
|---|---|---|
| `S4 에이전트 다시 실행: agent_02, agent_03` | **그 에이전트만** 다시 띄움 (같은 bundle 파일, 같은 출력 경로) | ❌ `extract_claims.py prepare` 다시 하지 말 것 — 끝난 결과까지 지워짐 (코드가 `--force` 없으면 거부) |
| `extract_claims.py collect` | collect 실행 | 몇 번 다시 해도 같은 결과 |
| `verify_quotes.py` | S5 실행 | 토큰 0 |
| `build_profile.py prepare` / `S6 에이전트 실행` | S6 | 초안 파일이 있으면 완료로 봄 |
| `완료 → S7` | 사람 검토 | |

4. 이어서 하는 동안에도 1장 규칙(`budget` → 실행 → `usage`)을 그대로 지킴

## 4. 토큰을 쓰지 않는 단계가 끊긴 경우

| 단계 | 다시 실행하면 |
|---|---|
| S1 `find_articles.py` | 처음부터 다시 (1~2분, 같은 결과) |
| S2 `fetch_articles.py` | 이미 받은 기사는 건너뜀 |
| OP.GG `collect_opgg.py discover` | `state.json`의 마지막 쪽 다음부터 이어서 (`--restart`면 처음부터) |
| OP.GG `collect_opgg.py fetch` | 이미 받은 글은 건너뜀 |
| S3 `sample_community.py` | 같은 입력이면 같은 표본 (난수 고정). ⚠ **S4 prepare 뒤에는 다시 돌리지 말 것** — 표본이 바뀌면 묶음과 어긋남 |

## 4-1. Workflow로 돌렸을 때 · 다른 계정으로 이어할 때

- Workflow 하위 에이전트는 `usage`를 부르지 않음 → 끝나거나 멈추면 `python runctl.py backfill` (`--dry-run`으로 먼저 볼 수 있음). 대화 기록(`~/.claude/projects/<프로젝트>/*/subagents/workflows/`)에서 에이전트별 최대 맥락 크기를 토큰으로 채움. 이미 채운 것은 건너뜀
- 계정을 바꾸기 전: `backfill` → `status` (STATUS.md 생성). 대화 기록은 지금 계정·PC에만 있으므로 순서 중요. 토큰을 안 쓰는 명령이라 한도에 걸린 뒤에도 터미널에서 직접 실행 가능
- 새 계정: `budget.yaml` `run.account` 를 바꿈 (`a2` …). 상한은 계정 합계. 자세한 표는 CLAUDE.md 7-1

## 5. 사용자가 확인할 수 있는 것

- 구독 남은 양·초기화 시각: Claude 앱 설정 → 사용량
- 이번 실행 누적·다음 할 일: `research/logs/STATUS.md` (자동 생성), 원본 `research/logs/usage.csv` (run 칸 = `v2`, account 칸 = 계정)
