# Mission

Cadwyn aims to be the most accurate and sophisticated API Versioning model out there. First of all, you maintain **zero** duplicated code yourself. Usually, in API versioning you [would need to](../theory/how_we_got_here.md) duplicate and maintain at least some layer of your applicaton. It could be the database, business logic, schemas, and endpoints. Cadwyn only duplicates your:

* schemas but you do not maintain the duplicates -- you only regenerate it when necessary
* endpoints but only in runtime so you do not need to maintain the duplicates

You define your database, business logic, routes, and schemas only once. Then, whenever you release a new API version, you use Cadwyn's [version change DSL](./version_changes.md#version-changes) to describe how to convert your app to the previous version. So your business logic and database stay intact and always represent the latest version while the version changes make sure that your clients can continue using the previous versions without ever needing to update their code.
