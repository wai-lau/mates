## Lessons

- Before routing to Qwen, run the check on base and confirm it fails by exit code; a check that is green on base cannot prove the fix.
- Make checks gate on exit status: never end a step in a pipe to tail, head or grep. Use pipefail or && between steps, and test the check on a known-bad tree.
- A check must not modify the tree it judges; put proof files in place during setup, not inside the check.
- Keep briefs for small checkable fixes lean: state the behaviour and the check. A short brief did as well as one ten times longer.
- If the exact diff is already known and only a few lines, apply it yourself; routing it costs more lead and wall time than the edit.
- Route to Qwen only when the check fixes the outcome; layout, fit or design work where Qwen would choose the approach stays with Claude.
- After an escalation, find the cause (setup, check, brief or infrastructure) before re-sending, and keep the retry brief minimal.
- Before sending to a pane, confirm it has the runtime the check needs; a missing runtime looks like a worker stall.
- Cap stalled Qwen tasks: after two re-briefs or about 30 minutes without progress, finish from its diff instead of briefing again.
- When a passing verdict still lists findings, skim the diff or the findings before accepting, so that real issues are not waved through.
- Prefer Qwen for small checkable fixes when latency does not matter; use a subagent only when a fast turnaround is needed.
- Record the exact worker model id, lead tokens and the check on every attempt, claude-self included, and count each re-brief as an attempt.
