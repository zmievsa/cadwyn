# Design Decisions

## Code vs Runtime generation

Cadwyn needs to generate at least two things: routes for old versions and schemas for old versions. It generates routes at runtime and schemas as code. Routes are generated at runtime to limit the size of the code base and because it's fairly easy to see which routes exist where using swagger. Schemas, on the other hand, change significantly more often and hence generating them using code might limit the number of human errors because any changes will be visible on code review.

Python would allow us to do everything at runtime which is fine. But a different programming language would have different constraints so a code-generating Cadwyn might be easier to implement in other languages/technologies too.

There are two problems with code generation: every version makes your repository bigger and code reviews might become more involved. For now it feels like they are worth it but the future will show.

## Context Variables

Cadwyn uses context variables for its version migration and side effect interfaces which is a big mistake. It introduces so much complexity and causes a huge lack of transparency in the entire framework. Yes, the interfaces will be a tiny bit uglier without context vars but it's worth the simplicity.
