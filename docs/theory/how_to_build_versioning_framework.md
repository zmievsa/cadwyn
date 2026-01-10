# How to build a versioning framework

## Questions to consider when evaluating your framework

### How simple is it to create a new API version?

If it is too simple, it is probably a trap. The framework is probably hiding too much complexity from you. For example, early on the Cadwyn dev team tried a straightforward "copy the entire business logic into a separate directory" approach which made it so easy to add new versions. We added too many of them in the end, thus it got hellishly hard to maintain or get rid of those versions.

### How simple is it to delete an old API version?

Your framework should allow you to clean up versions as simply and cheaply as possible. For example, if your framework tries to minimize the amount of code duplication in your repository by new routes including old routes and by new business logic inheriting from classes from old business logic, then deleting an old version is going to be painful (often even dangerous) as versions can quickly start interacting with each other turning a single small application into a set of interconnected applications.

### How simple is it to see the differences between API versions?

The simpler it is, the better off your users are.

### What exactly do you need to duplicate in order to create a new API version?

The less you duplicate and maintain manually, the simpler it is to support. However, the less you duplicate, the greater the risk of breaking older versions in subsequent releases.

### How simple is it to detect accidental data versioning?

Data versioning is an important issue when versioning your API. If your framework makes it hard to version data -- it's really good!
