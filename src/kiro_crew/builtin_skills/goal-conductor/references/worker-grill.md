# Worker grill

A worker grill settles one costly choice fast. Two or three sub-agents talk it through in short turns, a moderator worker runs them, and the owner only steers the direction and answers at most three taste questions. Facts and points everyone agrees on are settled by the workers, never asked.

It runs only while the `worker_grill` feature preview is on, which is when your turn carries the "Worker grill preview is on" paragraph. Then it replaces the skill's unasked debate offer; the formal debate in `worker-debate.md` stays for when the owner names one.

## The conductor's part

1. **Offer.** When a choice has two or more plausible paths and a wrong pick is costly, add "Let workers grill it first" to that reply's `[OPTIONS:]`. Costly means it crosses a security, data, approval or trust boundary, changes a contract others depend on, or follows a failed attempt or a reviewer's design block. Size alone is not a reason. Do not offer it for a small clear fix, CI or rebase work, wording, an emergency, a choice the owner already made, or when fewer than half the goal's work items are left.
2. **Card.** When the owner picks it, reply with one `[GRILL: {...}]` line, on its own line and before any `[OPTIONS:]` line. At most one per reply.

   ```
   [GRILL: {"q":"Who writes the welcome's facts?","must":"No wrong PR state again","skip":"The Board layout","who":[{"name":"A","stance":"For plan A"},{"name":"B","stance":"For plan B"},{"name":"C","stance":"Critic: can it go wrong again?"}],"minutes":10}]
   ```

   `q`, `must` and `skip` are at most 200 characters each. `who` has 2–3 entries; `name` is at most 20 characters and `stance` at most 80. `minutes` is an integer from 1 to 60. The line must be valid JSON; anything else shows as plain text. End the same reply with an `[OPTIONS:]` line of two choices in the owner's language, one to start the grill and one to skip it: where the dashboard draws the card, the card replaces those buttons; where it does not, the buttons still let the owner answer.
3. **Start.** The owner's start choice, or the card's message starting `开始讨论`, starts the grill. When the message carries `问题：`, `必须：` and `不讨论：` lines, those are the owner's edits and win; otherwise use the card as you wrote it. Dispatch ONE `kirocrew-worker` as moderator, one work item, with the question, must and skip, the participants and stances, the minutes, the owner's own words quoted verbatim, and this file's path. The skip choice, or `不用讨论了`, drops the grill.
4. **Steer.** When the owner adds a direction while it runs, send it to the moderator with `session_send`, word for word.
5. **Questions.** When the moderator reports `question`, record its question with `work_ask_record` so the owner gets buttons, and send the answer back. One question at a time, at most three in all.
6. **Close.** The moderator's `done` carries the consensus. Build on it like any other item result.

## The moderator's part

You are the moderator. You do not take a side.

1. **Thread.** Write `thread.md`: the question, must, skip, each participant's name and stance, `direction v1` (the owner's words), and the time cap. Save it with `artifact_save` and keep its link. Every round appends to it.
2. **Rounds.** Each round, run all participants at once with `spawn_sub_agents`, on the same model. Give each its name, its stance, the whole `thread.md` text and this rule: answer the latest points in at most 5 sentences; a fact carries a `path:line` or the command output that shows it; say "no new point" when you have none. Append each reply under its name, then update the artifact.
3. **Deep check.** When one fact is disputed and would change the conclusion, start one `spawn_run` to check it, and keep the talk going on the other points. Paste its finding into `thread.md` when it returns. Never more than one check at a time.
4. **Direction.** A direction the conductor relays is read at the start of the next round. Add it as `direction v2` (and so on) and tell the participants.
5. **Report each round.** `work_report status=progress`, with a `discussion` field (added to `work_report` by #37340): `phase` (`talking`, `checking`, `asking` or `done`), `round`, `minutes`, `direction`, `agreed` (at most 8), `disputed` (at most 5, each a point and the sides), `checking` (at most 3), `recent` (at most 6, each who and text) and `thread` (the artifact link). Send the whole object every time; the newest replaces the last. Put the agreed and disputed points in `summary` too.
6. **Stop** when two rounds in a row bring no new point, when the time cap is reached (the card's minutes, 15 by default, at most 6 rounds), or when the owner says stop.
7. **Settle.** A point every participant agreed on is decided. A fact is decided by its evidence, never by asking. What is left is taste: a choice the evidence cannot make. Ask at most three of those, one at a time, each as `work_report status=question reason=needs_human` with your recommendation and why.
8. **Done.** Report `done` with the consensus in `summary`, the owner's answers, and the thread link in `artifacts`.

**When sub-agents are refused.** If `spawn_sub_agents` or `spawn_run` is refused in your session, play each participant yourself in turn, from its stance, within the same rules. Say so in `thread.md` and in your `done` summary: the talk is faster but less independent.
