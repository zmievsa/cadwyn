# Change a schema that is not used in any endpoint

In some situations, you may want to use versioning not only for your OpenAPI schemas and endpoints but also within your code, for example to send versioned webhooks to your clients.

Suppose you want to change the type of an `id` field from integer to string:

```python
{! ./docs_src/how_to/change_openapi_schemas/change_schema_without_endpoint/block001.py !}
```

Unless there is an endpoint that has `User` as its response_model, this code will end up causing an error when you run the Cadwyn app. This is because Cadwyn tries to make sure that all of your converters apply to at least one endpoint. Otherwise, it would be too easy for you to make a mistake when writing converters for the wrong schemas.

To avoid it, set `check_usage=False`:

```python hl_lines="21"
{! ./docs_src/how_to/change_openapi_schemas/change_schema_without_endpoint/block002.py !}
```
