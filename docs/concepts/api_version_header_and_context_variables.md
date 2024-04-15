# API Version header and context variables

Cadwyn automatically converts your data to a correct version and has "version checks" when dealing with side effects as described in [the section above](./version_changes.md#version-changes-with-side-effects). It can only do so using a special [context variable](https://docs.python.org/3/library/contextvars.html) that stores the current API version.

You can also pass a different compatible contextvar to your `cadwyn.VersionBundle` constructor.
