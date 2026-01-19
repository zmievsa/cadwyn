# API version parameter

Cadwyn adds another routing layer to FastAPI by default: by a version parameter. This means that before FastAPI selects the correct route for the request, Cadwyn decides which version of the route will be used based on the version parameter. Feel free to look at the example app with URL path version prefixes and arbitrary strings as versions [here](../how_to/version_with_paths_and_numbers_instead_of_headers_and_dates.md).

## API version location

A version parameter can be passed in two different ways:

- as a custom header
- as a URL path parameter

Cadwyn uses the following defaults:

```python
from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_location="custom_header",
    api_version_parameter_name="X-API-VERSION",
    versions=VersionBundle(Version("2022-01-02")),
)
```

but you can change the header name if you want to:

```python
from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_location="custom_header",
    api_version_parameter_name="MY-GREAT-HEADER",
    versions=VersionBundle(Version("2022-01-02")),
)
```

or you can use a path parameter:

```python
from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_location="path",
    api_version_parameter_name="api_version",
    versions=VersionBundle(Version("2022-01-02")),
)
```

## API version format

A version parameter can be formatted in two different ways:

- as an ISO date
- as an arbitrary string

Cadwyn uses the following default:

```python
from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_format="date",
    versions=VersionBundle(Version("2022-01-02")),
)
```

In the example above only dates are accepted as valid versions.

You can also use an arbitrary string:

```python
from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_format="string",
    versions=VersionBundle(
        Version("v2"),
        Version("anything_can_be_a_version"),
        Version("v1"),
    ),
)
```

In the example above any arbitrary string is accepted as a valid version. Cadwyn does not sort them, Cadwyn assumes that their actual order matches the order of the versions in the `VersionBundle`.

### API version waterfalling

For historical reasons, date-based routing also supports waterfalling the requests to the closest earlier version of the API if the request date parameter does not match any of the versions exactly.

If the app has two versions: 2022-01-02 and 2022-01-05, and the request date parameter is 2022-01-03, then the request will be routed to the 2022-01-02 version, as it is the closest version, but lower than the request date parameter.

An exact match is always preferred to a partial match and a request is never matched to the higher-versioned route.

The routing is implemented like this because Cadwyn was born in a microservice architecture and it is extremely convenient to have waterfalling there. Assume that you have two Cadwyn services: Payables and Receivables, each defining its own API versions. Payables service might have ten versions while Receivables service might have only two versions because it did not need as many breaking changes. If a client requests a version that does not exist in Receivables, Cadwyn will waterfall to some earlier version, making Receivables behavior consistent even if API keeps getting new versions.

## API version parameter title and description

You can pass a title and/or a description to the `Cadwyn()` constructor. It is equivalent to passing `title` and `description` to `fastapi.Path()` or `fastapi.Header()` constructors.

```python
app = Cadwyn(
    ...,
    api_version_title="My Great API version parameter",
    api_version_description="Description of my great API version parameter",
)
```

## API version context variables

Cadwyn automatically converts your data to the correct version and has "version checks" when dealing with side effects as described in [the section above](./version_changes.md#version-changes-with-side-effects). It can only do so using a special [context variable](https://docs.python.org/3/library/contextvars.html) that stores the current API version.

You can also pass a different compatible `contextvar` to your `cadwyn.VersionBundle()` constructor.
