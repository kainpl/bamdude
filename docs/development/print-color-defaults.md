# Exact colour defaults

In Settings, **Force color match by default** sets the initial colour policy for new work composed by an order plan and for a fresh Print/Schedule dialog. It is off on an installation that has not configured it. Matching itself, including the independent base-material option, is unchanged.

The dialog reads the ordinary UI preferences endpoint, so an operator does not need permission to read all settings. An existing job, copied routing snapshot, group leader's answer or a checkbox the operator already touched wins over the default. A background settings reload never resets that answer. If the initial read fails, the dialog waits for a retry or an explicit choice to use the displayed values before submitting.

Order-plan Queue and Queue entire plan capture the effective Boolean in each new job, in either queue tier. An idempotent plan submission freezes it at registration; executing or retrying that operation after a settings change uses the frozen value. Historical registered operations without this field retain their original loose policy. No existing jobs are rewritten when the setting changes.

The wire setting is `default_force_color_match` in GET/PUT settings and GET UI preferences. It is an ordinary non-secret Boolean, included in portable settings restore. Explicit null is not an instruction to clear it. No schema migration is required for the existing key/value settings store.

This is a default for these two composition paths, not a global dispatch override. Direct queue/AutoQueue API requests keep their existing omitted-field default and honor explicit `force_color_match: false` or `true`. Virtual printers keep their separately configured `queue_force_color_match` policy. Integrations that require exact colours should send their own explicit choice.
