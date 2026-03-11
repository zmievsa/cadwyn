# Methodology

Cadwyn implements a methodology that is based on the following set of principles:

* Each version consists of "version changes" or "compatibility gates" which describe **independent, atomic** differences from the previous version
* A new version is created if and only if there are breaking changes
* Versions must have little to no effect on the business logic
* Versions **must always** be compatible in terms of data
* Creating new versions is avoided at all costs
* Any backward-compatible features must be backported to all compatible versions

Following these rules enables you to have a large number of self-documenting versions while encapsulating their complexity in small version change classes, providing your users with a consistent and stable experience.

If a breaking change cannot be avoided, the general approach is to:

1. Introduce the breaking change in your schemas, routes, or business logic
2. Write a version change class (and sometimes [a little extra](./version_changes.md#version-changes-with-side-effects)) that describes the difference between the new version and the old version
