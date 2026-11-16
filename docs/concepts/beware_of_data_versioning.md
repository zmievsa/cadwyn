# Beware of data versioning

Often you may want to introduce a breaking change when one of the following is true:

* Old data cannot be automatically converted to the structure of the new response
* New response cannot be automatically migrated to an older response
* Old request cannot be automatically converted to the HEAD request

In that case you are versioning your **data** rather than your API. An API versioning framework cannot solve this. It also makes versioning very hard because you can no longer guarantee compatibility between versions. Avoid it at all costs: all your API versions must be compatible with each other. Data versioning comes from **errors** in designing a new version, even when the use case seems complicated. I have yet to see a single case where data versioning is the right way to solve an API versioning issue.
