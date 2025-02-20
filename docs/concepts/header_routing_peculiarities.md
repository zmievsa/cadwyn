# Header routing peculiarities

Cadwyn adds another routing layer to FastAPI by default: by version header. This means that before FastAPI tries to route us to the correct route, Cadwyn will first decide on which version of the route to use based on a version header.

For historical reasons, header-routing also supports waterfalling the requests to the closest earlier version of the API if the request header doesn't match any of the versions exactly.

If the app has two versions: 2022-01-02 and 2022-01-05, and the request header is 2022-01-03, then the request will be routed to 2022-01-02 version as it the closest version, but lower than the request header.

Exact match is always preferred over partial match and a request will never be matched to the higher versioned route.

We implement routing like this because Cadwyn was born in a microservice architecture and it is extremely convenient to have waterfalling there. For example, imagine that you have two Cadwyn services: Payables and Receivables, each defining its own API versions. Payables service might contain 10 versions while receivables service might contain only 2 versions because it didn't need as many breaking changes. If a client requests a version that does not exist in receivables -- we will just waterfall to some earlier version, making receivables behavior consistent even if API keeps getting new versions.
