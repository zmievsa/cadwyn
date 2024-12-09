# Concepts

This section covers the entirety of features and their rationale in Cadwyn. It can also be used as a reference documentation until we have a proper one. First, let's talk about the reasons for using Cadwyn at all.

Cadwyn aims to be the most accurate and sophisticated API Versioning model out there. First of all, you maintain **zero** duplicated code yourself. Usually, in API versioning you [would need to](../theory/how_we_got_here.md) duplicate and maintain at least some layer of your application. It could be the database, business logic, schemas, and endpoints. Cadwyn allows you to duplicate none of that. Internally, it duplicates endpoints and schemas at runtime but none of it becomes your tech debt, none of it becomes your code to support. If you test rigorously, then only [some small subset of your tests](./testing.md) will need to be duplicated when existing functionality is changed between versions.

You define your database, business logic, routes, and schemas only once. Then, whenever you release a new API version, you use Cadwyn's [version change DSL](./version_changes.md#version-changes) to describe how to convert your app to the previous version. So your business logic and database stay intact and always represent the latest version while the version changes make sure that your clients can continue using the previous versions without ever needing to update their code.

This allows you to effortlessly maintain **hundreds** of versions, unlike any other API versioning approach.
