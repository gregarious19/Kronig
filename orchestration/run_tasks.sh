#!/usr/bin/env bash
# Execute an experiment broken into micro-tasks, stopping at the first blocker or approval gate.
#
# Usage: orchestration/run_tasks.sh <run_dir>
#
# Task types (tasks.json "type"):
#   agent    a fresh Sonnet session does judgement work; "model" may be one id or a ladder, e.g.
#            ["claude-haiku-5-5","claude-sonnet-5-5"]: when a rung fails the check (and did not file its own
#            blocker), the next rung retries automatically with the failed check output, without the planner (author a spec, repair a failed solve, interpret results)
#   command  a deterministic Kronig command (validate, compile, run, report, conformance); no model call
#            optional "on_fail": "human" turns a failure into NEEDS_HUMAN (e.g. questions only the user can answer)
#   gate     a human approval point; the run pauses until approvals/<id>.approved binds to the exact content shown
#
# Outputs flow between tasks by reference: in a task file, `cmd`, `check`, or `show`,
#   {{out:<id>}}      is replaced with the trimmed stdout of command task <id>
#   {{outpath:<id>}}  is replaced with the path of that stdout file
#   {{out:self}}      (in a command task's `diagnose` only) this task's own stdout
# A command task's optional `diagnose` runs on failure; its full output is added to the blocker.
#
# <run_dir> holds tasks.json, tasks/*.md, inputs/, and the run state this script keeps:
#   done/<id>             task finished; skipped on re-run (gates are re-verified every time)
#   outputs/<id>.out      full stdout of a command task
#   approvals/<id>.request.md   what the human is asked to approve, with its sha256
#   approvals/<id>.approved     written only after the human approves; first line is the approved sha256
#   blockers/<id>.md      open blocker; the run refuses to start while one exists
#   blockers/resolved/    blockers the planner has handled
#   attempts/<id>         executions per task, capped by MAX_ATTEMPTS
#   notes/<id>.md         agent handoff notes
#   logs/                 full transcripts (<id>.<n>.jsonl), stderr, check logs, usage.tsv
#
# Exit codes (last stdout line is the status):
#   0 ALL_DONE · 1 ERROR (setup) · 2 BLOCKED (planner can resolve) · 3 NEEDS_HUMAN
#   4 REVIEW (notes affect later tasks) · 5 APPROVAL (a gate needs the human's approval)
#
# Environment: EXECUTOR_MODEL (default claude-sonnet-5-5), MAX_ATTEMPTS (default 3),
#              EXECUTOR_BARE=1 (adds --bare; needs ANTHROPIC_API_KEY)
set -uo pipefail

die() { echo "ERROR $*"; exit 1; }

[[ $# -eq 1 ]] || die "usage: $0 <run_dir>"
command -v jq >/dev/null || die "jq is required"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$(cd "$1" 2>/dev/null && pwd)" || die "run dir not found: $1"
MANIFEST="$RUN_DIR/tasks.json"
[[ -f "$MANIFEST" ]] || die "missing $MANIFEST"
jq -e '.tasks | type == "array" and length > 0' "$MANIFEST" >/dev/null || die "tasks.json has no tasks"

KRONIG_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
repo_root_cfg="$(jq -r '.repo_root // "."' "$MANIFEST")"
[[ "$repo_root_cfg" == /* ]] || repo_root_cfg="$KRONIG_ROOT/$repo_root_cfg"
REPO_ROOT="$(cd "$repo_root_cfg" && pwd)" || die "bad repo_root: $repo_root_cfg"
MODEL="${EXECUTOR_MODEL:-claude-sonnet-5-5}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
EXECUTOR_PROMPT="$SCRIPT_DIR/prompts/executor.md"
REPO_RULES="$REPO_ROOT/AGENTS.md"

mkdir -p "$RUN_DIR"/{done,outputs,approvals,blockers/resolved,attempts,logs,notes}
USAGE_LOG="$RUN_DIR/logs/usage.tsv"
[[ -f "$USAGE_LOG" ]] || printf 'time\ttask\tattempt\tsubtype\tcost_usd\tinput_tokens\tcache_read\tcache_create\toutput_tokens\tturns\tsession_id\n' >"$USAGE_LOG"

LOCK="$RUN_DIR/.lock"
mkdir "$LOCK" 2>/dev/null || die "another run_tasks.sh holds $LOCK (remove it if stale)"
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

open_blocker="$(find "$RUN_DIR/blockers" -maxdepth 1 -name '*.md' | sort | head -n1)"
if [[ -n "$open_blocker" ]]; then
  if grep -qE '^needs_human:[[:space:]]*true' "$open_blocker"; then echo "NEEDS_HUMAN $open_blocker"; exit 3; fi
  echo "BLOCKED $open_blocker"; exit 2
fi

# ---------------------------------------------------------------- helpers
write_blocker() { # id kind needs_human problem evidence_file result_text transcript
  local path="$RUN_DIR/blockers/$1.md"
  {
    printf -- '---\ntask: %s\nkind: %s\nneeds_human: %s\nwritten_by: run_tasks.sh\n---\n' "$1" "$2" "$3"
    printf '## Problem\n%s\n\n## Evidence (complete output)\n```\n' "$4"
    [[ -s "$5" ]] && cat "$5"
    printf '```\n'
    [[ -n "$6" ]] && printf '\n## Executor final message (complete)\n%s\n' "$6"
    printf '\n## Full records\n- output log: %s\n- session transcript: %s\n- task outputs so far: %s/outputs/\n- working tree: run `git status` and `git diff` in the repo\n' \
      "$5" "${7:-none}" "$RUN_DIR"
  } >"$path"
  echo "$path"
}

task_field() { jq -r --arg id "$1" --arg f "$2" --arg d "$3" \
  '(.tasks[] | select(.id == $id) | .[$f]) // .defaults[$f] // $d | if type == "array" then join(",") else tostring end' "$MANIFEST"; }

# Replace {{out:<id>}} and {{outpath:<id>}} references. Fails if a referenced output does not exist yet.
render() {
  local text="$1" ref ref_id file
  while IFS= read -r ref; do
    [[ -z "$ref" ]] && continue
    ref_id="${ref#*:}"; ref_id="${ref_id%\}\}}"
    file="$RUN_DIR/outputs/$ref_id.out"
    [[ -f "$file" ]] || { echo "missing output $ref_id referenced as $ref" >&2; return 1; }
    if [[ "$ref" == "{{outpath:"* ]]; then
      text="${text//"$ref"/$file}"
    else
      text="${text//"$ref"/$(sed -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$file")}"
    fi
  done < <(grep -oE '\{\{(out|outpath):[A-Za-z0-9_.-]+\}\}' <<<"$text" | sort -u)
  printf '%s' "$text"
}

run_check() { # check log -> 0 if passes (an empty check passes)
  [[ -z "$1" ]] && return 0
  (cd "$REPO_ROOT" && bash -c "$1") >"$2" 2>&1
}

bump_attempts() { # id -> echoes new count, or exits NEEDS_HUMAN when over the cap
  local a=$(( $(cat "$RUN_DIR/attempts/$1" 2>/dev/null || echo 0) + 1 ))
  if (( a > MAX_ATTEMPTS )); then
    write_blocker "$1" scope true "Task $1 has failed $MAX_ATTEMPTS times; it needs a human decision or a re-plan." /dev/null "" >/dev/null
    return 1
  fi
  echo "$a" >"$RUN_DIR/attempts/$1"; echo "$a"
}

# ---------------------------------------------------------------- task types
do_gate() { # id n total
  local id="$1" show request approved_file digest shown
  request="$RUN_DIR/approvals/$id.request.md"; approved_file="$RUN_DIR/approvals/$id.approved"
  show="$(render "$(task_field "$id" show "")")" || { blk "$id" spec "Gate $id references an output that does not exist." /dev/null; return 2; }
  shown="$RUN_DIR/logs/$id.show.log"
  if [[ -n "$show" ]]; then (cd "$REPO_ROOT" && bash -c "$show") >"$shown" 2>&1 || {
      blk "$id" command "Gate $id could not produce what it must show for approval: \`$show\`." "$shown"; return 2; }
  else : >"$shown"; fi
  local task_file="$RUN_DIR/$(task_field "$id" file "tasks/$id.md")"
  digest="$( { [[ -f "$task_file" ]] && cat "$task_file"; cat "$shown"; } | shasum -a 256 | cut -d' ' -f1)"

  if [[ -f "$approved_file" ]] && [[ "$(head -n1 "$approved_file" | tr -d '[:space:]')" == "$digest" ]]; then
    touch "$RUN_DIR/done/$id"; echo "ok    [$2/$3] $id (approved, content unchanged)"; return 0
  fi
  [[ -f "$approved_file" ]] && mv "$approved_file" "$approved_file.stale.$(date +%s)"  # content changed since approval
  rm -f "$RUN_DIR/done/$id"
  {
    printf -- '---\ngate: %s\nsha256: %s\n---\n' "$id" "$digest"
    [[ -f "$task_file" ]] && cat "$task_file"
    printf '\n## Content to approve (output of `%s`)\n```\n' "${show:-nothing}"; cat "$shown"; printf '```\n'
    printf '\nTo approve, the human (not an agent) records:\n  printf "%%s\\napproved_by: <name>\\napproved_at: %%s\\nnote: <optional>\\n" %s "$(date -u +%%FT%%TZ)" > %s\n' "$digest" "$approved_file"
  } >"$request"
  echo "APPROVAL $request"; return 5
}

blk() { # id kind problem evidence [result] [transcript] -> prints BLOCKED line
  local p; p=$(write_blocker "$1" "$2" false "$3" "$4" "${5:-}" "${6:-}"); echo "BLOCKED $p"
}

do_command() { # id n total
  local id="$1" cmd check attempts out err log rc
  cmd="$(render "$(task_field "$id" cmd "")")" || { blk "$id" spec "Command $id references an output that does not exist." /dev/null; return 2; }
  [[ -n "$cmd" ]] || die "command task $id has no cmd"
  check="$(render "$(task_field "$id" check "")")" || { blk "$id" spec "Check for $id references a missing output." /dev/null; return 2; }
  attempts="$(bump_attempts "$id")" || { echo "NEEDS_HUMAN $RUN_DIR/blockers/$id.md"; return 3; }
  out="$RUN_DIR/outputs/$id.out"; err="$RUN_DIR/logs/$id.$attempts.stderr.log"; log="$RUN_DIR/logs/$id.$attempts.combined.log"
  echo "cmd   [$2/$3] $id (attempt $attempts): $cmd"
  (cd "$REPO_ROOT" && bash -c "$cmd" </dev/null) >"$out" 2>"$err"; rc=$?
  { printf '$ %s\n[exit %s]\n--- stdout ---\n' "$cmd" "$rc"; cat "$out"; printf -- '--- stderr ---\n'; cat "$err"; } >"$log"
  if (( rc == 0 )) && run_check "$check" "$RUN_DIR/logs/$id.check.log"; then
    touch "$RUN_DIR/done/$id"; echo "done  [$2/$3] $id"; return 0
  fi
  local problem="Command exited $rc: \`$cmd\`."
  (( rc == 0 )) && { problem="Command succeeded but its check fails: \`$check\`."; cat "$RUN_DIR/logs/$id.check.log" >>"$log"; }
  # Optional diagnose command (e.g. dump a failed bundle's stderr, validation report, manifest state) goes into the blocker.
  local diagnose; diagnose="$(task_field "$id" diagnose "")"
  if [[ -n "$diagnose" ]]; then
    local self_ref='{{out:self}}' own_ref="{{out:$id}}"
    diagnose="$(render "${diagnose//"$self_ref"/$own_ref}")" || diagnose="echo 'diagnose references a missing output'"
    { printf -- '--- diagnose: %s ---\n' "$diagnose"; (cd "$REPO_ROOT" && bash -c "$diagnose") 2>&1; } >>"$log"
  fi
  if [[ "$(task_field "$id" on_fail "")" == "human" ]]; then
    write_blocker "$id" input true "$problem" "$log" "" >/dev/null; echo "NEEDS_HUMAN $RUN_DIR/blockers/$id.md"; return 3
  fi
  blk "$id" command "$problem" "$log"; return 2
}

do_agent() { # id n total
  local id="$1" check task_file prompt attempts
  CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude)}" || die "claude CLI not found on PATH"
  task_file="$RUN_DIR/$(task_field "$id" file "tasks/$id.md")"
  [[ -f "$task_file" ]] || die "task file missing for $id: $task_file"
  check="$(render "$(task_field "$id" check "")")" || { blk "$id" spec "Check for $id references a missing output." /dev/null; return 2; }
  [[ -n "$check" ]] || die "agent task $id has no check command"
  prompt="$(render "$(cat "$task_file")")" || { blk "$id" spec "Task $id references an output that does not exist." /dev/null; return 2; }
  attempts="$(bump_attempts "$id")" || { echo "NEEDS_HUMAN $RUN_DIR/blockers/$id.md"; return 3; }

  local check_log="$RUN_DIR/logs/$id.check.log"
  if (( attempts > 1 )) && run_check "$check" "$check_log"; then
    touch "$RUN_DIR/done/$id"; echo "done  [$2/$3] $id (check already passing)"; return 0
  fi

  local blocker_path="$RUN_DIR/blockers/$id.md" notes_path="$RUN_DIR/notes/$id.md" sys_prompt tools allowed
  sys_prompt="$(sed -e "s|{{BLOCKER_PATH}}|$blocker_path|g" -e "s|{{NOTES_PATH}}|$notes_path|g" -e "s|{{TASK_ID}}|$id|g" "$EXECUTOR_PROMPT")"
  [[ -f "$REPO_RULES" ]] && sys_prompt+=$'\n\nRepository rules (from AGENTS.md), binding for this task:\n'"$(cat "$REPO_RULES")"
  tools="$(task_field "$id" tools "Read,Edit,Write,Glob,Grep")"
  [[ ",$tools," == *",Write,"* ]] || tools="$tools,Write"
  allowed="$(task_field "$id" allowed "")"

  # "model" may be one id or a ladder; attempt n uses rung n (the last rung repeats).
  local ladder task_model rung
  IFS=, read -ra ladder <<<"$(task_field "$id" model "$MODEL")"
  rung=$(( attempts - 1 < ${#ladder[@]} - 1 ? attempts - 1 : ${#ladder[@]} - 1 ))
  task_model="${ladder[$rung]}"
  # A previous rung's failed check output travels with the escalated attempt.
  local escalation="$RUN_DIR/logs/$id.escalation.md"
  (( attempts > 1 )) && [[ -s "$escalation" ]] && prompt+=$'\n\n'"$(cat "$escalation")"
  local args=(-p --model "$task_model" --output-format stream-json --verbose
        --append-system-prompt "$sys_prompt"
        --tools "$tools" --permission-mode acceptEdits
        --max-turns "$(task_field "$id" max_turns 20)" --max-budget-usd "$(task_field "$id" max_budget_usd 2)"
        --strict-mcp-config --disable-slash-commands --exclude-dynamic-system-prompt-sections
        --add-dir "$RUN_DIR")
  [[ -n "$allowed" ]] && args+=(--allowedTools "$allowed")
  local d
  while IFS= read -r d; do
    [[ -z "$d" ]] && continue; [[ "$d" == /* ]] || d="$REPO_ROOT/$d"; args+=(--add-dir "$d")
  done < <(jq -r --arg id "$id" '((.tasks[] | select(.id == $id) | .add_dirs) // .defaults.add_dirs // [])[]' "$MANIFEST")
  [[ "${EXECUTOR_BARE:-0}" == "1" ]] && args+=(--bare)

  echo "agent [$2/$3] $id (attempt $attempts, $task_model)"
  local session_log="$RUN_DIR/logs/$id.$attempts.jsonl" session_err="$RUN_DIR/logs/$id.$attempts.stderr.log" runner rc
  runner=("$CLAUDE_BIN"); command -v caffeinate >/dev/null && runner=(caffeinate -i "$CLAUDE_BIN")
  # Prompt on stdin: variadic flags (--add-dir, --tools, --allowedTools) would swallow a trailing argument.
  (cd "$REPO_ROOT" && "${runner[@]}" "${args[@]}" <<<"$prompt") >"$session_log" 2>"$session_err"; rc=$?

  local final subtype result
  final="$(jq -c 'select(.type == "result")' "$session_log" 2>/dev/null | tail -n1)"
  if [[ -n "$final" ]]; then
    subtype="$(jq -r '.subtype // "unknown"' <<<"$final")"; result="$(jq -r '.result // ""' <<<"$final")"
    jq -r --arg t "$(date -u +%FT%TZ)" --arg id "$id" --arg a "$attempts" \
      '[$t, $id, $a, .subtype, .total_cost_usd, .usage.input_tokens, .usage.cache_read_input_tokens, .usage.cache_creation_input_tokens, .usage.output_tokens, .num_turns, .session_id] | map(tostring) | @tsv' \
      <<<"$final" >>"$USAGE_LOG"
  else
    subtype="no_result_event"; result="$(cat "$session_err")"
  fi

  if [[ -f "$blocker_path" ]]; then
    if grep -qE '^needs_human:[[:space:]]*true' "$blocker_path"; then echo "NEEDS_HUMAN $blocker_path"; return 3; fi
    echo "BLOCKED $blocker_path"; return 2
  fi
  if run_check "$check" "$check_log"; then
    touch "$RUN_DIR/done/$id"
    if [[ -s "$notes_path" ]]; then echo "done  [$2/$3] $id (notes: $notes_path)"; else echo "done  [$2/$3] $id"; fi
    if (( $2 < $3 )) && grep -qE '^affects_later_tasks:[[:space:]]*true' "$notes_path" 2>/dev/null; then
      echo "REVIEW $notes_path"; return 4
    fi
    return 0
  fi

  local problem kind
  if (( rc != 0 )) || [[ "$subtype" != "success" ]]; then
    problem="Executor session ended abnormally (exit $rc, subtype $subtype) and the check fails: \`$check\`."
    kind=env; [[ "$subtype" == error_max_turns || "$subtype" == error_max_budget_usd ]] && kind=scope
  elif [[ "$(tail -n1 <<<"$result" | tr -d '[:space:]')" == "BLOCKED" ]]; then
    problem="Executor reported BLOCKED but did not write a blocker file; its full final message explains why. Check: \`$check\`."
    kind=unknown; grep -qiE 'denied|permission' <<<"$result" && kind=permission
  else
    problem="Executor reported completion but the check fails: \`$check\`."; kind=test
  fi
  # Escalate to the next model in the ladder when the failure could be the model's (not the environment's).
  if [[ "$kind" != env ]] && (( rung + 1 < ${#ladder[@]} && attempts < MAX_ATTEMPTS )); then
    {
      printf '## Previous attempt
%s (attempt %s) did not pass the check: %s
' "$task_model" "$attempts" "$problem"
      printf 'Its edits may still be on disk: inspect them, then correct or replace them.

Check output:
```
'
      cat "$check_log"; printf '```

Its final message:
%s
' "$result"
    } >"$escalation"
    echo "escalate [$2/$3] $id: $task_model failed the check, retrying with ${ladder[$((rung + 1))]}"
    do_agent "$@"; return $?
  fi
  blk "$id" "$kind" "$problem" "$check_log" "$result" "$session_log"; return 2
}

# ---------------------------------------------------------------- main loop
total=$(jq '.tasks | length' "$MANIFEST")
n=0
while IFS= read -r id; do
  n=$((n + 1))
  type="$(task_field "$id" type agent)"
  # Gates are re-verified even when done, so an edit after approval re-opens them.
  if [[ -f "$RUN_DIR/done/$id" && "$type" != gate ]]; then echo "skip  [$n/$total] $id"; continue; fi

  for dep in $(jq -r --arg id "$id" '.tasks[] | select(.id == $id) | (.depends_on // [])[]' "$MANIFEST"); do
    [[ -f "$RUN_DIR/done/$dep" ]] || { blk "$id" spec "Dependency $dep is not done; tasks.json order is wrong." /dev/null; exit 2; }
  done

  case "$type" in
    gate)    do_gate "$id" "$n" "$total" ;;
    command) do_command "$id" "$n" "$total" ;;
    agent)   do_agent "$id" "$n" "$total" ;;
    *)       die "task $id has unknown type '$type'" ;;
  esac
  status=$?
  (( status == 0 )) || exit "$status"
done < <(jq -r '.tasks[].id' "$MANIFEST")

notes=$(find "$RUN_DIR/notes" -name '*.md' -size +0 | wc -l | tr -d ' ')
echo "ALL_DONE $RUN_DIR ($total tasks, $notes with notes in $RUN_DIR/notes)"
exit 0
