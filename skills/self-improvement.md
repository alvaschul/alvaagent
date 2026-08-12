---
name: self-improvement
description: >
  Procedure the agent follows to capture feedback, track improvements, self-test
  after editing its own source, and reflect on its performance — enabling
  autonomous self-correction.
tags: [self-improvement, feedback, quality, reflection]
related_skills: []
---

# Self-Improvement Loop

## Trigger
- The user gives explicit feedback via /feedback (good/bad/neutral).
- The agent notices a pattern of mistakes or user dissatisfaction.
- The agent has just edited its own source code (alvaagent_tui.py).
- The agent finishes a substantial task and has idle time.

## Steps

1. **Capture feedback immediately.** When the user rates a response or expresses
   satisfaction/frustration, call `feedback(rating="good"|"bad"|"neutral",
   notes=...)` with the user's exact words. Do NOT wait — record it while the
   context is fresh.

2. **Check for improvement patterns.** After recording feedback (especially
   "bad"), call `reflect()` to review recent feedback and pending improvements.
   Look for repeated complaints about the same area (brevity, accuracy,
   tone, format, etc.).

3. **Record improvement actions.** When a pattern emerges, call
   `improvement_set(area="...", action="...")` with a concrete, actionable fix.
   Area names should be short labels (e.g. "response brevity", "python errors").
   Action descriptions should be specific (e.g. "keep replies under 3 sentences").

4. **After editing your own source, ALWAYS self-test.** Any edit to
   alvaagent_tui.py, start.sh, or test_tui.py must be followed by:
   - `run_command("python3 -m py_compile alvaagent_tui.py")` — syntax check
   - `run_command("python3 test_tui.py")` — functional test suite
   - If both pass, tell the user the change is done and will take effect on restart.
   - If either fails, revert the edit and explain what broke.

5. **Run /self-test after significant changes.** Beyond compile+test_tui.py,
   use the /self-test command (or call tool_self_test) to run the full harness
   test suite: calculator, sandbox, todo, memory, skills, command classification,
   file tools, and feedback/improvement/reflect tools.

6. **Mark improvements done after verifying.** Once you've fixed an area and
   confirmed the fix works (via self-test or user confirmation), call
   `improvement_done(area="...")` to close the loop.

7. **Reflect periodically.** When idle or between tasks, call `reflect()` to
   review the feedback log and pending improvements. If there are unresolved
   improvements, prioritize fixing them before starting new work.

8. **Save reusable procedures as skills.** When you discover a non-obvious
   procedure during self-improvement (e.g. a pattern for handling a certain type
   of user request), save it with `skill_save(name=..., content=...)` so it can
   be applied on future tasks.

## Anti-patterns
- Do NOT ignore repeated "bad" feedback on the same thing — treat it as a real bug.
- Do NOT claim an improvement is done without actually verifying the fix.
- Do NOT edit your own source without running py_compile + test_tui.py afterward.
- Do NOT let feedback accumulate unread — call reflect() regularly.
