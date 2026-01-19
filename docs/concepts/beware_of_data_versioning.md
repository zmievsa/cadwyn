# Beware of data versioning

Often you may want to introduce a breaking change when one of the following is true:

* Old data cannot be automatically converted to the structure of the new response
* New response cannot be automatically migrated to an older response
* Old request cannot be automatically converted to the HEAD request

This means that you are not versioning your API, you are versioning your **data**. Such an issue cannot be solved using an API versioning framework. It also makes it incredibly hard to version as you now cannot guarantee compatibility between versions. Avoid this at all costs — all your API versions must be compatible between each other. Data versioning is not a result of a complicated use case, it is a result of **errors** when devising a new version. I have yet to see a single case where data versioning is the right way to solve an API versioning issue.
