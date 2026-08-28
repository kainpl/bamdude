"""Cloud Link — the opt-in agent that connects this farm to the cloud portal.

The wire contract (envelope v1) is defined once, as zod schemas in the portal
repo, and mirrored here in :mod:`schemas`. The portal's exported fixtures are
snapshotted under ``backend/tests/fixtures/cloud_link/`` and pin the mirror to
the contract; edit the mirror to match a fixture, never the other way round.
"""
