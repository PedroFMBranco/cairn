---
name: cairn-review
description: Walk the operator through a Cairn project's unanswered lead questions one at a time in grill format, write the answer files, and tell the operator the hub restarts an idle lead afterwards. In the terminal it then ends its tmux session; in the dashboard chat (`/cairn-review chat`) it ends with the summary. Run in the project repo directory.
---

# cairn-review

Run in the project repo (the current directory). You collect answers. You do not do the lead's work.

**Chat mode.** Invoked as `/cairn-review chat`, you are talking to the operator through the Cairn dashboard: skip step 4 (there is no tmux session). Keep messages short markdown; the dashboard shows the option letters as buttons and reads two markers, so write them exactly: start every question message with the question id in bold (`**q07**`), and end the final summary message with the line `Review finished.` (also when there was nothing to review).

## Rules

- Write only `work/inbox/qNN.answer.md`. Never edit question files, tickets, the map or `work/status`.
- Describe tickets in plain words, never by id or file name.
- One question per message. The operator answers fast; keep each message short.
- Never print, log or write environment variables. `CLAUDE_CODE_OAUTH_TOKEN` may hold a login token.
- Never start, stop or restart the lead. The hub does that.

## 1. List

1. Read `work/inbox/q*.md` (skip `*.answer.md`). A question is unanswered when `qNN.answer.md` does not exist.
2. Order: `gate: yes` first, then oldest `asked_at`.
3. Tell the operator the count and how many are gates. None: say so and stop.

## 2. Walk

For each question, send one message:

```
**q07** <context line, plus the ticket's title in words if it has one>

A. ...
B. ... (recommended: ...)
C. ...

Gate: yes/no. If you skip it, the lead already applies B.
Reply: a letter, y = take the recommendation, later = skip, or your own text.
```

Interpret the reply:

- A letter: `choice` is that letter.
- `y`: `choice` is the recommended letter (the `default`).
- `later`: skip, write nothing.
- Free text: `choice: other`, the text goes in the body. If the text starts with a letter that matches an option, use that letter and put the rest in the body.
- "Why" or a question: read the code, results or ticket the question concerns, answer briefly, then repeat the options.

Write the answer file right after each answer, before the next question. Use the current UTC time:

```
---
id: q07
choice: B
answered_at: 2026-10-17T15:10:00Z
---
<free text from the conversation, may be empty>
```

## 3. Summarize

Say how many were answered and skipped, and what each answer changes for the lead, in plain words.

If you wrote an answer file, tell the operator: if the lead is idle, the hub restarts it within about 30 seconds of this session ending (in chat mode: once no questions are left, or the operator ends the review), and it reads the answers first. A lead that is still working picks them up before its next ticket.

## 4. End the session (Terminal mode only)

Project name: the key in `~/.cairn/projects.json` whose `path` is this directory (else the directory name). Tell the operator the review session closes now, then end it as your last command:

```
tmux -L cairn kill-session -t =<project>-review
```

The next Review starts a fresh session with a current question list.
