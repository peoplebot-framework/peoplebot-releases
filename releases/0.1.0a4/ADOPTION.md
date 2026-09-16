# PeopleBot 0.1.0a4 adoption boundary

Installing the verified wheel supplies experimental framework utilities. It does
not create or run an agent.

Before a provider-backed or scheduled operation, the consuming sovereign
environment must separately:

1. pin the public distribution commit and matching artifacts;
2. define each agent Instance and bind it to its own assigned provider chat/session;
3. configure credentials outside repositories and shared messages;
4. define task authority, allowed paths/effects, finite invocation/time budgets,
   and STOP;
5. configure owner-write and peer-read message endpoints separately;
6. configure one owner-local usage profile and save every run boundary;
7. use isolated attempt branches/worktrees and preserve failed attempts;
8. configure memory and synchronization independently from framework adoption;
9. keep the supplied scheduler template disabled until a manual tick, busy
   rejection, STOP, failure, and real timed launch have been verified.

Environment, Instance, provider session, task, launcher, and Execution are
separate identities. Missing optional task/launcher/provider-turn links remain
unknown and do not erase known Instance usage or block unrelated authorized work.
Measured tokens do not establish account allowance, credits, cost, model, effort,
or finality.

See `docs/operations/instance-operating-model.md` for the complete reusable setup
model and explicit unsupported boundaries.
