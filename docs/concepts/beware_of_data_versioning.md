# Beware of data versioning

Oftentimes you will want to introduce a breaking change where one of the following is true:

* Old data cannot be automatically converted to the structure of the new response
* New response cannot be automatically migrated to an older response
* Old request cannot be automatically converted to the HEAD request

This means that you are not versioning your API, you are versioning your **data**. This is not and cannot be solved by an API versioning framework. It also makes it incredibly hard to version as you now cannot guarantee compatibility between versions. Avoid this at all costs -- all your API versions must be compatible between each other. Data versioning is not a result of a complicated use case, it is a result of **errors** when divising a new version. I am yet to meet a single case where data versioning is the right way to solve an API versioning problem.
