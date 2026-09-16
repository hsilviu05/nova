# 013 — A permissioned tool system, and confirmation as the boundary

**Status:** Accepted · **Date:** 2026-09-16

## Context

NOVA became a terminal for a development environment. That means a language
model gets to decide, from a sentence somebody typed on a phone, which program
runs on a Mac that holds source code, credentials and running services.

Three properties had to hold at once, and they pull against each other:

1. **It has to be useful.** "Check SnapWorth" should check SnapWorth, not
   produce a paragraph about how one might check SnapWorth.
2. **It has to be safe when the model is wrong.** Models hallucinate
   arguments, misread intent, and occasionally decide the helpful thing is to
   clean up.
3. **It has to be safe when the model is *manipulated*.** Tool output is
   written by other people. A container name, a branch name, a GitHub issue
   title and a log line all end up inside the prompt, and any of them can
   contain instructions.

The third is the one that rules out the obvious design. Giving the model a
shell and telling it to be careful fails to (3) immediately, because "be
careful" is an instruction and so is everything it reads afterwards.

## Decision

**Every capability is a registered tool with a schema and a permission, and
anything destructive requires a confirmation token the model cannot obtain.**

### The registry is built at startup and frozen

What NOVA can do is decided from configuration before any request arrives.
There is no code path that constructs a tool on demand, and no request can add
one. A capability that could appear at runtime is a capability nothing is
auditing.

Registration enforces two invariants that a tool cannot enforce about itself:

- A tool needing confirmation must carry a `confirmation_prompt`. An
  unlabelled "Are you sure?" is not informed consent.
- `Permission.WRITE` is only valid in the knowledge group.

### Three permissions, and the second one is narrow on purpose

| Level | Meaning | Runs |
|---|---|---|
| `read` | No side effects | Always, including mid-reply |
| `write` | Changes something NOVA owns, reversibly, visibly | Without asking |
| `destructive` | Everything else | Only with an explicit confirmation |

Two levels would have been simpler. The reason there are three is memory:
NOVA already writes memories on its own, from ordinary conversation, in a
background task nobody approves. Prompting for the explicit "remember that I
prefer Postgres" while the implicit version goes unremarked is theatre, not
consent.

But "reversible and visible" is exactly the kind of definition that erodes.
The tempting fix for a future tool that keeps hitting the confirmation prompt
is to relabel it `WRITE`, so the registry **refuses to register** one outside
the knowledge group. The rule is enforced at startup rather than in review.

### Confirmation is a token, not a flag

```
model asks ──▶ refused ──▶ server describes the action in words
                              and mints a token bound to
                              (account, tool, arguments)
                                        │
                   SSE "confirm" event ─┴─▶ the app ──▶ a sheet
                                                          │
                                        person taps yes ──┘
                                                          │
                              POST /tools/invoke + token ─┘
```

The properties that matter, each with a test:

- **The model never receives a token.** It goes to the client in the SSE
  event; the tool result fed back to the model says only that the action is
  waiting for approval.
- **The service refuses a destructive call from a model-initiated context**
  outright, not only the chat loop. A second caller cannot reintroduce the
  hole.
- **The token authorises the arguments**, not just the tool. Approving
  "remove nova-test" authorises nothing else, and a mismatched presentation
  discards the token rather than returning it.
- **Single use**, via `GETDEL`.
- **Scoped to the account**, so it cannot be replayed against another user.
- **Fails closed.** If Redis is unreachable, NOVA refuses. This is the exact
  opposite of the rate limiter, which fails open — losing a cache should
  degrade abuse protection, never be the reason a container gets deleted.

### The chat path admits read-only tools

This is the decision that actually bounds the damage, and everything above is
secondary to it. A prompt injection that completely succeeds gets NOVA to run
a **different read-only tool**.

### No shell, anywhere

Tools run through `create_subprocess_exec` with an argv array. There is no
interpreter, so no globbing, substitution, pipes or `;`. Children get a built
environment containing none of NOVA's configuration, a deadline enforced on
the process group, and bounded output.

`execute_shell_command` exists, is off, and even when on runs only exact
matches from an allowlist. It is `DESTRUCTIVE` regardless of what is being
run, because NOVA cannot know that an allowlisted program is harmless with the
arguments it was just handed.

## Alternatives considered

**Give the model a shell and rely on the system prompt.** Fastest to build and
the most capable. Rejected because every defence would be a sentence, and
sentences lose to other sentences the model reads later. The failure is also
unbounded: there is no worst case short of "the machine".

**Confirm everything, including reads.** Safe and unusable. A terminal that
asks permission to run `git status` is a terminal nobody opens, and a person
who has tapped "yes" forty times today is not reading the forty-first prompt.
Confirmation fatigue is a real attack surface, not a usability nicety.

**A confirmation flag in the tool call.** `{"confirmed": true}` — the model
sets it when the person agreed. Rejected in about ten seconds: the model
writes the flag. Any argument that persuades it to run the tool persuades it
to set the flag.

**Persist pending actions in Postgres.** More durable, and durability is the
wrong property. A confirmation is answered within seconds or not at all, and
one that survives a restart is stale. Redis with a short TTL means expiry is
free and the failure mode is "ask again".

**An allowlist of shell commands instead of typed tools.** Simpler to extend —
but an allowlist has no schema, so arguments are unvalidated; no structured
result, so the app can only render text; and no description the model can use
to choose well. Typed tools cost more per capability and give all three.

**Let the model see permissions** so it can avoid proposing what will be
refused. Rejected: it turns the permission system into something the model can
reason about and plan around. It sees a name, a description and a schema. The
app sees permissions, because the person holding the phone is entitled to know
which button changes something.

## Consequences

**Good.** Every capability is auditable, testable, and individually
switchable. The audit log answers "what was attempted" rather than only "what
happened". Adding a capability is one file and one registry line, and the new
tool inherits validation, timeouts, redaction and auditing by existing. The
default deployment can do nothing but read.

**Bad.** Each capability is a Pydantic model, a spec and an `execute` — more
ceremony than a shell command. The model can only do what somebody thought to
expose, so there will be questions NOVA cannot answer that a shell could.
Destructive actions need a round trip through a human, which makes NOVA
useless for anything unattended — a cost accepted deliberately, since
unattended is not what this is for.

**Unresolved.** Confirmation proves somebody tapped a button, not that they
read the prompt. The prompt names the specific action and shows the arguments,
which is the most a design can do. Beyond that it is the same trust a `sudo`
password is.
