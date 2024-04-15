# Methodology

Cadwyn implements a methodology that is based on the following set of principles:

* Each version is made up of "version changes" or "compatibility gates" which describe **independent atomic** differences between it and previous version
* We make a new version if an only if we have breaking changes
* Versions must have little to no effect on the business logic
* Versions **must always** be compatible in terms of data
* Creating new versions is avoided at all costs
* Any backwards compatible features must be backported to all compatible versions

These rules give us an ability to have a large number of self-documenting versions while encapsulating their complexity in small version change classes, providing a consistent and stable experience to our users.

So if we see that we need to make a breaking change, our general approach is to:

1. Make the breaking change in your schemas, routes, or business logic
2. Write a version change class (and sometimes [a little extra](./version_changes.md#version-changes-with-side-effects)) that describes the difference between the new version and the old version
