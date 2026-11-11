# mates daily retro

You review one day of the mates audit log. The lead (Claude) routes each
task: to a local Qwen pane (`qwen`), to itself (`claude-self`) or to a
subagent (`claude-subagent`). The routing rule is:

> "hand everything with a clear auditable outcome to Qwen / complex reasoning
> with divergent outcomes go to Claude"

Each log line below is one finished attempt. `summary`, `reason`, `check` and
the answers in `questions` are the lead's own words. Question text and flag
paths were removed; `flags` keep only their kind (for example
`unnamed-file`: Qwen edited a file the brief did not name).

Look for:

- misroutes: a Qwen task with no check, or that needed judgement; a Claude
  task that had a clear check and could have gone to Qwen;
- weak briefs: questions asked, sent-back or escalated attempts, flags;
- cost: lead tokens spent per Qwen task against doing it directly.

Answer with ONE JSON object and nothing else:

```
{"findings_md": "<short markdown findings for the day>",
 "lessons": ["<lesson>", ...],
 "proposals": ["<proposal>", ...]}
```

- `findings_md`: a few short sections, plain words, numbers where you have
  them.
- `lessons`: the WHOLE new list of lessons for the lead (it replaces the
  current list, given below). Keep, merge or drop the old ones. At most 20,
  each at most 200 characters, each a general habit. Never name a repo, a
  file or a path: a lesson that does is rejected.
- `proposals`: changes to the routing rule, the number of parallel Qwen tasks
  or the model. These go to the owner as questions; they are never applied.
  Empty when you have none.
