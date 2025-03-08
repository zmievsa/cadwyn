# Where to put the version and how to format it

Cadwyn adds another routing layer to FastAPI by default: by version parameter. This means that before FastAPI tries to route us to the correct route, Cadwyn will first decide on which version of the route to use based on a version parameter. Feel free to look at the example app with URL path version prefixes and arbitrary strings as versions [here](../how_to/version_with_paths_and_numbers_instead_of_headers_and_dates.md).

## API Version location

The version parameter can be passed in two different ways:

- As a custom header
- As a URL path parameter

Cadwyn will use the following defaults:

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

## API Version format

The version parameter can be formatted in two different ways:

- As an ISO date
- As an arbitrary string

Cadwyn uses the following default:

```python
from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_format="date",
    versions=VersionBundle(Version("2022-01-02")),
)
```

In which case only dates will be accepted as valid versions.

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

In which case any string will be accepted as a valid version. Notice how they do not have to be sortable or even ordered. Cadwyn will assume that their actual order is the same as the order of the versions in the `VersionBundle`.

### API Version waterfalling

For historical reasons, date-based routing also supports waterfalling the requests to the closest earlier version of the API if the request date parameter doesn't match any of the versions exactly.

If the app has two versions: 2022-01-02 and 2022-01-05, and the request date parameter is 2022-01-03, then the request will be routed to 2022-01-02 version as it the closest version, but lower than the request date parameter.

Exact match is always preferred over partial match and a request will never be matched to the higher versioned route.

We implement routing like this because Cadwyn was born in a microservice architecture and it is extremely convenient to have waterfalling there. For example, imagine that you have two Cadwyn services: Payables and Receivables, each defining its own API versions. Payables service might contain 10 versions while receivables service might contain only 2 versions because it didn't need as many breaking changes. If a client requests a version that does not exist in receivables -- we will just waterfall to some earlier version, making receivables behavior consistent even if API keeps getting new versions.
