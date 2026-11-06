"""전체 수집 파이프라인 실행 (D → C → F → G 계정 → G 솔랭). 한 단계가 실패해도 나머지는 진행.

A(a_oracles_elixir.py)·B(b_picks_bans.py)는 팀원이 수집 — 다시 만들 때만 개별 실행.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = [
    ("D 챔피언 메타 + C 기본 스탯", "d_champion_meta.py"),
    ("C 패치노트", "c_patch_notes.py"),
    ("F 로스터/일정", "f_roster_schedule.py"),
    ("G 선수 계정 (trackingthepros)", "g_accounts_ttp.py"),
    ("G 솔로랭크 (Riot API 키 필요)", "g_riot_soloq.py"),
]


def main():
    results = []
    for name, script in STEPS:
        print(f"\n===== {name} ({script}) =====", flush=True)
        extra = sys.argv[1:2] if script == "f_roster_schedule.py" else []  # --refresh 는 F만
        rc = subprocess.call([sys.executable, str(HERE / script), *extra], cwd=HERE)
        results.append((name, rc))
    print("\n===== 결과 =====")
    for name, rc in results:
        print(f"  {'OK  ' if rc == 0 else 'FAIL'} {name}")
    sys.exit(0 if all(rc == 0 for _, rc in results) else 1)


if __name__ == "__main__":
    main()
