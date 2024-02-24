# Design Decisions

## Code vs Runtime generation

Cadwyn needs to generate at least two things: routes for old versions and schemas for old versions. It generates routes at runtime and schemas as code. Routes are generated at runtime to limit the size of the code base and because it's fairly easy to see which routes exist where using swagger. Schemas, on the other hand, change significantly more often and hence generating them using code might limit the number of human errors because any changes will be visible on code review.

Python would allow us to do everything at runtime which is fine. But a different programming language would have different constraints so a code-generating Cadwyn might be easier to implement in other languages/technologies too.

There are two problems with code generation: every version makes your repository bigger and code reviews might become more involved. For now it feels like they are worth it but the future will show.

## Which versions to apply code generation to

Cadwyn should probably code generate all versions, including latest. And `latest` directory should become `internal`. Then VersionBundle will also need a new optional argument: `instructions_to_migrate_from_internal_to_latest: Sequence[CodeGenerationInstruction]`.

This will mean that our user won't need to manually copy classes from latest to internal and our the whole weirdness with "InternalRepresentationOf" will be completely unnecessary! The only con with this is that yes, it is increasing complexity yet again.

However, this still leaves one problem: let's say an internal field is a union of v1 and v2 where v1 has type "dict" and v2 has type "list". If we keep migrations from internal to latest in VersionBundle, then once we deprecate v1 -- internal will still contain "dict". Maybe it makes more sense to add these "internal to latest" instructions to Version(...) or maybe even to VersionChange(...). Adding them to Version would allow us to deprecate them and would provide us with great visibility in terms of how latest/internal changed in different versions. Adding them to VersionChange would look bad and we would lose this visibility BUT we would be able to connect changes in latest to changes in the specific version change, i.e. we would know WHY these changes occurred.

## Context Variables

Cadwyn uses context variables for its version migration and side effect interfaces which is a big mistake. It introduces so much complexity and causes a huge lack of transparency in the entire framework. Yes, the interfaces will be a tiny bit uglier without context vars but it's worth the simplicity.
