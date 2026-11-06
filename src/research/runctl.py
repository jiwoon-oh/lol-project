"""실행 관리 — 토큰 기록·상한, 진행 상태, 중단 후 이어하기 (코드만, 토큰 0).

구독 사용량이 중간에 떨어져도 처음부터 다시 하지 않게:
- 에이전트 1개 = 결과 파일 1개 (research/claims/{player}/out/agent_NN.json, profiles/{player}_draft.md)
  → 결과 파일이 있고 형식이 맞으면 "완료", 없거나 깨졌으면 "다시 돌릴 것"
- 에이전트가 끝날 때마다 토큰을 usage.csv 에 기록 (run 칸 = budget.yaml run.id)
- 다음 에이전트를 띄우기 전에 budget 으로 상한 확인 → 넘으면 멈춤

사용
  python runctl.py status [--players Doran,Oner,Faker]   # 단계별 완료/남은 일, 다시 돌릴 에이전트 목록
  python runctl.py budget [--next 150000]                 # 이번 실행 누적 토큰 vs 상한. 넘으면 종료 코드 2
  python runctl.py usage --stage S4 --player Doran --agent agent_01 --tokens 51234 --seconds 210 [--note ...]
  python runctl.py backfill [--dry-run]                   # Workflow 로 띄운 에이전트 토큰을 대화 기록에서 찾아 usage.csv 에 채움

- usage.csv 의 account 칸 = budget.yaml run.account (계정을 바꾸면 그 값을 바꿀 것). 상한은 계정 합계로 셈
- status 는 사람이 읽는 요약을 logs/STATUS.md 에도 씀 (계정을 바꿔 이어할 때 이 파일부터 봄)
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from common import LOGS, RESEARCH, ROOT, get_logger, load_config, players, slug, targets

log = get_logger("run")
USAGE = LOGS / "usage.csv"
STATE = LOGS / "run_state.json"
STATUS_MD = LOGS / "STATUS.md"
HEADER = ["time", "run", "stage", "player", "agent", "batches", "total_tokens", "seconds", "note", "account"]


def run_cfg() -> dict:
    return load_config("budget")["run"]


def account() -> str:
    return str(run_cfg().get("account", "a1"))


def read_usage(migrate: bool = True) -> list[dict]:
    """usage.csv 행 목록. account 칸이 없던 예전 파일이면 칸을 붙여 다시 씀 (예전 행 = 처음 계정 a1, migrate=False 면 안 씀)."""
    if not USAGE.exists():
        return []
    with open(USAGE, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if rows and "account" not in rows[0]:
        for r in rows:
            r["account"] = "a1"
        if not migrate:
            return rows
        with open(USAGE, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=HEADER)
            w.writeheader()
            w.writerows(rows)
        log.info("usage.csv 에 account 칸 추가 (예전 행 %d개 = a1)", len(rows))
    return rows


def log_usage(stage: str, pid: str, agent: str, tokens: int, seconds: float, note: str = "", batches: str = "",
              time: str | None = None):
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    read_usage()  # 예전 형식이면 먼저 맞춤
    new = not USAGE.exists()
    with open(USAGE, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(HEADER)
        w.writerow([time or datetime.now().isoformat(timespec="seconds"), run_cfg()["id"], stage, pid, agent, batches,
                    tokens, seconds, note, account()])


def used_tokens(run_id: str, by: str = "stage") -> dict:
    """이번 실행(run_id)의 누적 토큰 (by = stage 또는 account). v1 기록은 세지 않음."""
    out = {}
    for r in read_usage():
        if r.get("run") == run_id:
            k = r.get(by) or "a1"
            out[k] = out.get(k, 0) + int(r["total_tokens"])
    return out


def cmd_budget(next_tokens: int) -> int:
    rc = run_cfg()
    used = used_tokens(rc["id"])
    total = sum(used.values())
    limit = rc["token_cap"] * rc["stop_ratio"]
    print(f"실행 {rc['id']}: 누적 {total:,} 토큰 {used} / 상한 {rc['token_cap']:,} (멈춤 기준 {limit:,.0f})")
    print(f"계정별: {used_tokens(rc['id'], 'account')} (지금 계정 {account()})")
    if total + next_tokens > limit:
        print(f"⛔ 다음 작업(약 {next_tokens:,})을 하면 멈춤 기준을 넘음 → 여기서 멈추고 사용자에게 확인")
        return 2
    print(f"✅ 다음 작업 가능 (남은 여유 약 {limit - total - next_tokens:,.0f})")
    return 0


USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def transcripts_dir() -> Path:
    """이 프로젝트의 Claude Code 대화 기록 폴더 (~/.claude/projects/<경로의 : \\ / 를 - 로>)."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[:\\/]", "-", str(ROOT))


def read_agent(meta: Path) -> dict | None:
    """Workflow 하위 에이전트 1개의 단계·대상·토큰. 토큰 = 대화 중 가장 컸던 맥락 크기(입력+캐시+출력),
    Agent 도구가 알려 주는 total_tokens 와 같은 기준 (메인 대화에서 기록한 값과 맞춤)."""
    info = json.loads(meta.read_text(encoding="utf-8"))
    if "workflows" not in meta.parts and info.get("finished") is not True:
        return None  # 아직 돌고 있는 에이전트 (끝난 뒤 다시 backfill 하면 채워짐)
    desc = info.get("description") or ""
    if m := re.fullmatch(r"(\S+)/(agent_\d+)", desc):
        stage, pid, agent = "S4", m[1], m[2]
    elif m := re.fullmatch(r"S6 (\S+)", desc):
        stage, pid, agent = "S6", m[1], "s6"
    else:
        return None
    peak, first, last, err = 0, None, None, ""
    for line in open(meta.with_name(meta.name.replace(".meta.json", ".jsonl")), encoding="utf-8"):
        j = json.loads(line)
        if ts := j.get("timestamp"):
            first, last = first or ts, ts
        msg = j.get("message") or {}
        if u := msg.get("usage"):
            peak = max(peak, sum(u.get(k, 0) for k in USAGE_KEYS))
        if j.get("isApiErrorMessage"):
            err = "session limit" if "limit" in str(msg.get("content")) else "api error"
    t0, t1 = (datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone() for t in (first, last))
    return {"stage": stage, "player": pid, "agent": agent, "tokens": peak, "seconds": round((t1 - t0).total_seconds(), 1),
            "time": t1.replace(tzinfo=None).isoformat(timespec="seconds"), "id": meta.name.split(".")[0],
            "wf": meta.parent.name, "failed": err if not peak else ""}


def cmd_backfill(dry_run: bool):
    """Workflow 로 띄운 하위 에이전트는 runctl usage 를 부르지 않아 usage.csv 에 빠짐 → 대화 기록에서 채움.
    메인 대화에서 띄운 에이전트도 이름이 같은 형식이면 채움 (usage 로 이미 손으로 기록한 것은 아래에서 건너뜀).
    이미 채운 에이전트(note 의 agent-… id)와 손으로 기록한 같은 단계·대상·에이전트는 건너뜀 → 여러 번 돌려도 같음."""
    rows = read_usage(migrate=not dry_run)
    seen_ids = {m for r in rows for m in re.findall(r"agent-[0-9a-f]+", r.get("note") or "")}
    manual = {(r["stage"], slug(r["player"]), r["agent"]) for r in rows
              if r.get("run") == run_cfg()["id"] and not re.search(r"agent-[0-9a-f]+", r.get("note") or "")}
    base = transcripts_dir()
    # Workflow 에이전트 + 메인 대화에서 이름을 "{대상}/agent_NN" · "S6 {대상}" 으로 띄운 에이전트
    metas = sorted(base.glob("*/subagents/workflows/*/agent-*.meta.json")) + sorted(base.glob("*/subagents/agent-*.meta.json"))
    add = []
    for meta in metas:
        a = read_agent(meta)
        if not a or a["id"] in seen_ids or (a["stage"], slug(a["player"]), a["agent"]) in manual:
            continue
        add.append(a)
    tot = sum(a["tokens"] for a in add)
    failed = [a for a in add if a["failed"]]
    print(f"대화 기록 {base} : Workflow 에이전트 {len(metas)}개 중 새로 채울 것 {len(add)}개, 토큰 {tot:,} "
          f"(실패 {len(failed)}개 = 0 토큰으로 기록)")
    for st in ("S4", "S6"):
        part = [a for a in add if a["stage"] == st]
        print(f"  {st}: {len(part)}개, {sum(a['tokens'] for a in part):,}")
    if dry_run:
        print("(--dry-run: usage.csv 는 그대로)")
        return
    for a in sorted(add, key=lambda a: a["time"]):
        note = f"backfill {a['wf']} {a['id']}" + (f", 실패: {a['failed']}" if a["failed"] else "")
        log_usage(a["stage"], a["player"], a["agent"], a["tokens"], a["seconds"], note, time=a["time"])
    print(f"usage.csv 에 {len(add)}행 추가")


def s4_state(pid: str) -> dict:
    d = RESEARCH / "claims" / slug(pid)
    mf = d / "manifest.json"
    if not mf.exists():
        return {"stage": "S4", "state": "not_prepared"}
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    done, todo = [], []
    for ag in manifest["agents"]:
        out = d / "out" / f"{ag['agent']}.json"
        try:
            res = {r["batch"] for r in json.loads(out.read_text(encoding="utf-8"))["results"]}
            ok = set(ag["batches"]) <= res
        except Exception:
            ok = False
        (done if ok else todo).append(ag["agent"])
    collected = (RESEARCH / "claims" / f"{slug(pid)}.jsonl").exists() and \
        (RESEARCH / "claims" / f"{slug(pid)}.jsonl").stat().st_mtime >= max(
            [(d / "out" / f"{a}.json").stat().st_mtime for a in done] or [0])
    return {"stage": "S4", "agents_done": done, "agents_todo": todo, "collected": collected and not todo}


def s5_state(pid: str) -> bool:
    c, v = RESEARCH / "claims" / f"{slug(pid)}.jsonl", RESEARCH / "verified" / f"{slug(pid)}.jsonl"
    return c.exists() and v.exists() and v.stat().st_mtime >= c.stat().st_mtime


def s6_state(pid: str) -> dict:
    p = RESEARCH / "profiles"
    return {"input": (p / "work" / f"{slug(pid)}_s6_input.md").exists(), "draft": (p / f"{slug(pid)}_draft.md").exists(),
            "checked": (p / f"{slug(pid)}_check.txt").exists()}


def cmd_status(names: list[str]):
    state = {"time": datetime.now().isoformat(timespec="seconds"), "run": run_cfg()["id"], "players": {}}
    for pid in names:
        s4, s6 = s4_state(pid), s6_state(pid)
        if s4.get("state") == "not_prepared":
            nxt = "S1~S3 확인 후 extract_claims.py prepare"
        elif s4["agents_todo"]:
            nxt = f"S4 에이전트 다시 실행: {', '.join(s4['agents_todo'])} (prepare 다시 하지 말 것)"
        elif not s4["collected"]:
            nxt = "extract_claims.py collect"
        elif not s5_state(pid):
            nxt = "verify_quotes.py"
        elif not s6["input"]:
            nxt = "build_profile.py prepare"
        elif not s6["draft"]:
            nxt = "S6 에이전트 실행"
        elif not s6["checked"]:
            nxt = "build_profile.py check"
        else:
            nxt = "완료 → S7 사람 검토"
        state["players"][pid] = {"S4": s4, "S5_done": s5_state(pid), "S6": s6, "next": nxt}
        print(f"{pid}: {nxt}")
    used = used_tokens(run_cfg()["id"])
    state["tokens_used"] = used
    print(f"이번 실행 누적 토큰: {sum(used.values()):,} {used}")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    write_status_md(state)
    print(f"→ {STATUS_MD}")


def write_status_md(state: dict):
    """계정을 바꿔 이어할 때 먼저 볼 요약. 결과 파일에서 매번 새로 만들므로 손으로 고치지 않음."""
    rc = run_cfg()
    total = sum(state["tokens_used"].values())
    limit = rc["token_cap"] * rc["stop_ratio"]
    groups: dict[str, list[str]] = {}
    for pid, s in state["players"].items():
        key = re.sub(r":.*", "", s["next"]) if s["next"].startswith("S4 에이전트") else s["next"]
        groups.setdefault(key, []).append(pid)
    lines = [
        "# 실행 상태 (자동 생성 — 손으로 고치지 말 것)", "",
        f"- 만든 시각: {state['time']} · 실행 `{rc['id']}` · 지금 계정 `{account()}`",
        f"- 누적 토큰: **{total:,}** / 상한 {rc['token_cap']:,} (멈춤 기준 {limit:,.0f}, 남은 여유 {max(limit - total, 0):,.0f})",
        f"- 단계별: {state['tokens_used']} · 계정별: {used_tokens(rc['id'], 'account')}",
        "- 다시 만들기: `python src/research/runctl.py backfill` (Workflow 를 돌렸다면) → `python src/research/runctl.py status`",
        "", "## 다음 할 일별 대상", "", "| 다음 할 일 | 수 | 대상 |", "|---|---|---|"]
    for key, pids in sorted(groups.items(), key=lambda kv: kv[0]):
        lines.append(f"| {key} | {len(pids)} | {', '.join(pids)} |")
    todo = [(pid, s["S4"]["agents_todo"]) for pid, s in state["players"].items() if s["S4"].get("agents_todo")]
    lines += ["", f"## 다시 돌릴 S4 에이전트 ({sum(len(a) for _, a in todo)}개)", "",
              "같은 bundle 파일·같은 출력 경로로 다시 띄움. `extract_claims.py prepare` 는 다시 하지 않음.", ""]
    lines += [f"- {pid}: {', '.join(a)}" for pid, a in todo] or ["- 없음"]
    STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--players", default=None)
    b = sub.add_parser("budget")
    b.add_argument("--next", type=int, default=0, help="다음에 띄울 작업의 예상 토큰")
    u = sub.add_parser("usage")
    for k in ("stage", "player", "agent"):
        u.add_argument(f"--{k}", required=True)
    u.add_argument("--tokens", type=int, required=True)
    u.add_argument("--seconds", type=float, required=True)
    u.add_argument("--batches", default="")
    u.add_argument("--note", default="")
    bf = sub.add_parser("backfill")
    bf.add_argument("--dry-run", action="store_true", help="채울 내용만 보여 주고 usage.csv 는 그대로")
    a = ap.parse_args()
    if a.cmd == "status":
        cmd_status(a.players.split(",") if a.players else list(targets()))
    elif a.cmd == "budget":
        sys.exit(cmd_budget(a.next))
    elif a.cmd == "backfill":
        cmd_backfill(a.dry_run)
    else:
        log_usage(a.stage, a.player, a.agent, a.tokens, a.seconds, a.note, a.batches)


if __name__ == "__main__":
    main()
