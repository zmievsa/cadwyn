
# Change the business logic in a new version

First, ask yourself: are you sure there really needs to be a behavioral change? Are you sure it is not possible to keep the same logic for both versions? Or at least make the behavior depend on the received data? Behavioral changes (or **side effects**) are the least maintainable part of almost any versioning approach. They produce the largest footprint on your code so if you are not careful -- your logic will be littered with version checks.

But if you are certain that you need to make a breaking behavioral change, Cadwyn has all the tools to minimize its impact as much as possible.

## Calling endpoint causes unexpected data modifications

You'd use an `if statement` with a [side effect](../../concepts/version_changes.md#version-changes-with-side-effects).

## Calling endpoint doesn't cause expected data modifications

You'd use an `if statement` with a [side effect](../../concepts/version_changes.md#version-changes-with-side-effects).

## Calling endpoint doesn't cause expected additional actions (e.g. Webhooks)

You'd use an `if statement` with a [side effect](../../concepts/version_changes.md#version-changes-with-side-effects).

## Errors

### Change the status code or a message in an HTTP error

You can [migrate anything about the error](../../concepts/version_changes.md#migration-of-http-errors) in a version change.

### Introduce a new error or remove an old error

You'd use an `if statement` with a [side effect](../../concepts/version_changes.md#version-changes-with-side-effects).
