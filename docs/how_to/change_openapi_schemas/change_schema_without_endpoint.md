# Change a schema that is not used in any endpoint

In some situations, we may want to use versioning not just for our openapi schemas and endpoints but also within our code such as when we want to send versioned webhooks to our clients.

For example, let's say we want to change the type of an "id" field from integer to string:

```python
{! ./docs_src/how_to/change_openapi_schemas/change_schema_without_endpoint/block001.py !}
```

Unless there is an endpoint that has `User` as its response_model, this code will end up causing an error when we run our Cadwyn app. This is because Cadwyn tries to make sure that all of your converters apply to at least one endpoint. Otherwise, it would be too easy for you to make a mistake when writing converters for the wrong schemas.

To avoid it, set `check_usage=False`:

```python hl_lines="21"
{! ./docs_src/how_to/change_openapi_schemas/change_schema_without_endpoint/block002.py !}
```
