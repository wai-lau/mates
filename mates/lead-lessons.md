## Lessons

- Before routing to Qwen, run the check on base and confirm it fails; a check that is green on base cannot prove the fix.
- Keep briefs for small checkable fixes lean: state the behaviour and the check. A short brief did as well as one ten times longer.
- After an escalation, find the cause (setup, check or brief) before re-sending, and keep the retry brief minimal.
- When a passing verdict still lists findings, skim the diff or the findings before accepting, so that real issues are not waved through.
- Prefer Qwen for small checkable fixes when latency does not matter; use a subagent only when a fast turnaround is needed.
- Record the exact worker model id on every attempt, so results can be compared across model swaps.
