# Version with paths and numbers instead of headers and dates

By default, Cadwyn puts the version in a header and uses ISO dates as versions. You can use any strings instead of ISO dates, path version prefixes instead of version headers, or both.
You can mix and match version formats and version locations as you see fit, but Cadwyn does not support [version waterfalling](../concepts/api_version_parameter.md#api-version-waterfalling) for arbitrary strings as versions.

Here's the quickstart tutorial example with version numbers and path prefixes:

```python
{! ./docs_src/how_to/version_with_path_and_numbers_instead_of_headers_and_dates/block001.py !}
```
