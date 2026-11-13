# Cadwyn

Cadwyn versions APIs Stripe-style: business logic sees only HEAD, and version changes migrate data between versions.

## Language

### Versions

**Head version**:
The version whose models you write by hand and your business logic uses; Cadwyn generates every other version from it.
_Avoid_: latest (when meaning head)

**Latest version**:
The newest version that clients see, generated from head.
_Avoid_: head, current

### Data migrations

**Data migration**:
A function that converts request or response data between two adjacent versions.
_Avoid_: converter

**Migration by schema**:
A data migration picked by the model of the body.
_Avoid_: schema-based migration

**Migration by path**:
A data migration picked by the path and method of the endpoint.
_Avoid_: path-based migration

### Standalone use

**Standalone use**:
Using Cadwyn's versioning (version changes, data migrations, versioned models) without FastAPI or any other web framework, including wiring it into another framework by hand.
_Avoid_: headless, framework-free, sans-FastAPI

**Standalone install**:
An installation of Cadwyn for standalone use, without FastAPI and its dependencies. Planned in [#203](https://github.com/zmievsa/cadwyn/issues/203); not available yet.
_Avoid_: slim install, core install
