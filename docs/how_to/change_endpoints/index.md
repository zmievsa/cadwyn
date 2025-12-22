# Change endpoints in a new version

## Add a new endpoint

It is not a breaking change so it's recommended to add it to all versions. If you believe that you still need it, use [following migration](../../concepts/endpoint_migrations.md#defining-endpoints-that-didnt-exist-in-old-versions).

## Delete an old endpoint

See [concepts](../../concepts/endpoint_migrations.md#defining-endpoints-that-didnt-exist-in-new-versions)

## Change an attribute of an endpoint

Modifying a "decorative" attribute such as a description or a summary is not a breaking change so apply it to all versions.

However, you are still free to change almost any attribute of an endpoint in an old version. See [concepts docs](../../concepts/endpoint_migrations.md#changing-endpoint-attributes).

## Rename an endpoint

Renaming endpoints is the same as changing their "path" attribute. See [concepts docs](../../concepts/endpoint_migrations.md#changing-endpoint-attributes) for more details.
