# Cadwyn

Production-ready community-driven modern [Stripe-like](https://stripe.com/blog/api-versioning) API versioning in FastAPI

---

<p align="center">
<a href="https://github.com/zmievsa/cadwyn/actions/workflows/ci.yaml?branch=main&event=push" target="_blank">
    <img src="https://github.com/zmievsa/cadwyn/actions/workflows/ci.yaml/badge.svg?branch=main&event=push" alt="Test">
</a>
<a href="https://codecov.io/gh/zmievsa/cadwyn" target="_blank">
    <img src="https://img.shields.io/codecov/c/github/zmievsa/cadwyn?color=%2334D058&logo=codecov" alt="Coverage">
</a>
<a href="https://pypi.org/project/cadwyn/" target="_blank">
    <img alt="PyPI" src="https://img.shields.io/pypi/v/cadwyn?color=%2334D058&logo=pypi&label=PyPI" alt="Package version">
</a>
<a href="https://pypi.org/project/cadwyn/" target="_blank">
    <img src="https://img.shields.io/pypi/pyversions/cadwyn?color=%2334D058&logo=python" alt="Supported Python versions">
</a>
<a href="https://discord.gg/yRmcWF7rxE" target="_blank">
    <img alt="Discord" src="https://img.shields.io/discord/1183145640407076864?color=%2334D058&logo=discord">
</a>
</p>

## Who is this for?

Cadwyn allows you to maintain the implementation just for your newest API version and get all the older versions generated automatically. You keep API backward compatibility encapsulated in small and independent "version change" modules while your business logic stays simple and knows nothing about versioning.

This [approach](https://docs.cadwyn.dev/theory/how_we_got_here/#ii-migration-based-response-building) may be useful if you want to:

1. Support many API versions for a long time
2. Have features and bugfixes automatically backported to older API versions

Whether you are a newbie in API versioning, a pro looking for a sophisticated tool, an experimenter looking to build a similar framework, or even someone who just wants to learn about all approaches to API versioning -- Cadwyn has the functionality, theory, and documentation to cover all the mentioned use cases.

## Get started

It is recommended to read the [quickstart tutorial](https://docs.cadwyn.dev/quickstart/setup/) first to get your feet wet with Cadwyn's approach
