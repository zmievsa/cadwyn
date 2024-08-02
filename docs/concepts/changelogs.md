# Changelogs

Cadwyn can automatically generate API changelogs for your versions. By default they are available through the unversioned endpoint `GET /changelog`. You can also get it from `Cadwyn.generate_changelog` method.

## Hiding version changes and instructions

Sometimes you might want to do private internal version changes or instructions within the version changes that should not be visible to the public. You can do this by using the `cadwyn.hidden` function. For example:

```python
from cadwyn import hidden, VersionChange, endpoint


class VersionChangeWithOneHiddenInstruction(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        hidden(endpoint("/users/{user_id}", ["GET"]).had(path="/users/{uid}")),
    )


@hidden
class CompletelyHiddenVersionChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(User).field("address").existed_as(type=str),
    )
```

## Customizing changelog endpoint

Just pass the `changelog_url` argument to `Cadwyn` and a `GET` to this url will start returning the changelog for all versions based on the contents of your `VersionBundle`.

If you want to hide the changelog endpoint, pass `include_changelog_url_in_schema=False` to `Cadwyn`.

If you want to delete the changelog endpoint, pass `changelog_url=None` to `Cadwyn`.

## Changelog structure and entry types

Please, visit the swagger page for your app and check the structure and values of enums in the `/changelog` endpoint.
