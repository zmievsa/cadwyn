# Concepts

This section covers the entirety of the features and their rationale in Cadwyn. To begin with, here are the reasons for using Cadwyn at all.

Cadwyn aims to be the most accurate and sophisticated API versioning framework out there. First of all, Cadwyn users maintain **no** duplicated code. Usually, API versioning [requires](../theory/how_we_got_here.md) duplicating and maintaining at least some layer of your application. It could be the database, business logic, schemas, or endpoints. With Cadwyn you avoid duplicating any of that. Internally, Cadwyn duplicates endpoints and schemas at runtime but none of it becomes your tech debt, none of it becomes your code to support. If you test rigorously, only [a small subset of your tests](./testing.md) needs to be duplicated when existing functionality changes between versions.

You define your database, business logic, routes, and schemas only once. Then, whenever you release a new API version, you use Cadwyn's [version change DSL](./version_changes.md#version-changes) to describe how to convert your app to the previous version. So your business logic and database remain intact and always represent the latest version while the version changes ensure that your clients can continue using the previous versions without updating their code.

This allows you to maintain **hundreds** of versions effortlessly, unlike other API versioning approaches.
