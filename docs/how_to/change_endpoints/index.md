# Change endpoints in a new version

## Add a new endpoint

It is not a breaking change so it's recommended to simply add it to all versions. If you believe that you still need it, you can use the [following migration](../../concepts/endpoint_migrations.md#defining-endpoints-that-didnt-exist-in-old-versions).

## Delete an old endpoint

See [concepts](../../concepts/endpoint_migrations.md#defining-endpoints-that-didnt-exist-in-new-versions)

## Change an attribute of an endpoint

Changing a "decoratory" attribute such as description or summary is generally not a breaking change and should just be applied to all versions.

However, you are still free to change almost any attribute of an endpoint in the old version. See [concepts docs](../../concepts/endpoint_migrations.md#changing-endpoint-attributes).

## Rename an endpoint

Renaming endpoints is the same as changing their "path" attribute. See [concepts docs](../../concepts/endpoint_migrations.md#changing-endpoint-attributes) for more details.
