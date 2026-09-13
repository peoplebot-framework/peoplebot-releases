# Adopt PeopleBot 0.1.0a3 in a sovereign environment

## Verify before installation

1. Obtain `peoplebot-0.1.0a3-py3-none-any.whl`, the matching
   `peoplebot-0.1.0a3.tar.gz`, public provenance, and `SHA256SUMS` from the same
   reviewed prerelease.
2. Verify every `SHA256SUMS` entry before installation and retain the matching
   source archive with the wheel.
3. Record the exact public distribution commit from provenance. The proposed
   Blueprint path is `peoplebot/blueprints/project_review/blueprint.json`; the
   proposed Adapter path is `peoplebot/adapters`. These become usable public
   `StateRef` values only after that exact commit is published.
4. Create a fresh Python 3.11-or-newer environment and install the exact wheel with
   `python -m pip install --no-deps ./peoplebot-0.1.0a3-py3-none-any.whl`.

Git-backed operations require Git 2.45 or newer. The project-review Adapter is
configured for `codex-cli 0.153.4`, model `gpt-5.6-luna`, and read-only sandbox
mode. The v0 Instance-admission implementation is Windows-specific.

## Supply sovereign bindings explicitly

Installation creates no Instance and grants no authority. The consuming
environment separately owns and supplies its environment and Instance identities,
project repository and pinned State, explicit context paths and context-policy
State, runtime and authentication context, evidence store/runtime root, timestamps,
and any memory repository or synchronization destination.

Review execution, explicit memory saving, explicit memory synchronization, and
fresh-process recovery are separate operations. A review result does not save
memory, a local memory commit is not remote synchronization, and a copied Blueprint
does not transfer an Instance, credentials, private context, or standing authority.

Tool-event rejection is detection rather than proof that every tool or external
effect is prevented. A live CLI/model acceptance check and consumer-specific setup
require their own authorization and evidence.
