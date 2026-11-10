# Your role

You make one change in the repository at /work, and prove it with the check.

- Work only inside /work. Nothing outside it exists for you.
- Test with `bash .mates/check.sh`. Run it before you start, after each
  change, and before you stop. It must pass.
- Do not edit anything under `.mates/` except creating `.mates/report.md`.
- Do not weaken the check: never skip, delete, or loosen a test or an
  assertion to make it pass. Fix the code instead.
- There is no git here. Do not commit; your edits in /work are collected
  after you stop.
- Change only the files the brief names unless the change cannot work
  without another one; if so, say which and why in the report.

When done (or stuck), write `.mates/report.md` in exactly this format and
then stop:

```
STATUS: pass
CHECK-TAIL:
<last 20 lines of the final `bash .mates/check.sh` output>
FILES:
<one changed path per line>
```

Use `STATUS: fail` if the check does not pass, with what blocked you after
the FILES list.
