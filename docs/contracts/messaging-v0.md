# Git-backed messaging v0

`peoplebot.messaging` implements a bounded owner-publishes/peer-reads transport. Each environment has one deterministic direct ref under `refs/heads/peoplebot/messages/v0/`. A message commit contains only `message.json`; history is linear, append-only, and capped at 256 entries.

A `peoplebot.message.v0` record carries a stable message ID, kind (`task`, `reply`, or `stop`), sender and recipient, task and correlation IDs, optional `reply_to`, purpose, at most 4096 UTF-8 content bytes, a UTC timestamp, and at most eight exact `StateRef` values. Serialized records are capped at 16 KiB. Content is data, not executable authority.

`OutboundDestination` and `PeerSource` are distinct types. Publication accepts only the owner destination and pushes exactly one direct ref with no tag or wildcard operation. Reading fetches one observed peer commit and rejects unapproved senders, malformed or merge history, duplicate IDs, and remote URL mismatch. Caller-provided expected URLs may not contain credentials; configured Git URL rewrites are rejected because they make the effective endpoint ambiguous.

`append_and_publish_owned_message` is the work-cycle publication boundary. It holds one environment publisher admission across remote-tip inspection, local append, push of the exact newly created commit, and verification against the exact configured write endpoint. A competing local publisher is refused while that sequence is active. The push uses `<exact-commit>:<destination-ref>`, so a later local ref advance cannot substitute different content. A differing read endpoint is not treated as evidence about the write destination.

`publish_message` remains the lower-level exact-commit publisher and reports remote-verified, failed, or uncertain evidence after preflight, push, and independent write-endpoint inspection. A timeout or unverifiable post-state is uncertain and is never silently retried. `reconcile_message_publication` performs observation only. These contracts do not claim distributed transactionality, exactly-once external effects, or authority derived from a message.

Replies are accepted only when `validate_correlated_reply` proves exact request/reply, task, correlation, sender, and recipient identities. `resolve_message_states` resolves referenced commits and paths solely through caller-supplied checkouts.

Run the deterministic two-environment fixture from a source checkout:

```powershell
python docs/examples/message_work_cycle_fake.py
```
