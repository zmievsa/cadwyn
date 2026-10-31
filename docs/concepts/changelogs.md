# Changelogs

!!! warning "Deprecated"

    Cadwyn's changelog feature is deprecated and will be removed in Cadwyn 9.0. It remains available for backwards compatibility, but new applications should maintain and expose their own changelog instead. Pass `changelog_url=None` to `Cadwyn()` to disable the deprecated endpoint.

Cadwyn can automatically generate API changelogs for your versions. By default, they are available through the deprecated unversioned endpoint `GET /changelog`. You can also access it via the deprecated `Cadwyn.generate_changelog()` method.

## Hiding version changes and instructions

Sometimes you might want to make private internal version changes or instructions within the version changes that should not be visible to the public. You can do this by using the deprecated `cadwyn.hidden()` function. Consider the example below:

```python
{! ./docs_src/concepts/changelogs/block001.py [ln:1-1,9-] !}
```

## Customizing changelog endpoint

The deprecated changelog endpoint name can be customized by specifying a new name via the deprecated `changelog_url` argument to the `Cadwyn()` constructor. Accessing this URL via a `GET` request will return the changelog for all versions based on the content of your `VersionBundle`.

If you want to hide the changelog endpoint, pass the deprecated `include_changelog_url_in_schema=False` argument to `Cadwyn()`.

If you want to disable the changelog endpoint, pass `changelog_url=None` to `Cadwyn()`.

## Changelog structure and entry types

Please visit the Swagger page for your app and check the structure and values of enums in the `/changelog` endpoint.
