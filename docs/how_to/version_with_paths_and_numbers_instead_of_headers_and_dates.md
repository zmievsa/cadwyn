# Version with paths and numbers instead of headers and dates

Cadwyn uses version headers with ISO dates by default for versioning. However, you can use any strings instead of ISO dates and/or you can use path version prefixes instead of version headers. Here's our quickstart tutorial example but using version numbers and path prefixes:

Feel free to mix and match the API version formats and version locations as you see fit.
But beware that Cadwyn does not support [version waterfalling](../concepts/api_version_parameter.md#api-version-waterfalling) for arbitrary strings as versions.

```python
{! ./docs_src/how_to/version_with_path_and_numbers_instead_of_headers_and_dates/block001.py !}
```
