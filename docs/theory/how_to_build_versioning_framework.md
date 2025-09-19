# How to build a versioning framework

## Questions to ask yourself when rating your framework

### How easy is it to create a version?

If it is too easy, it is probably a trap. The framework is probably hiding too much complexity from you and will shoot you in the back later. For example, early on we tried a simple "copy the entire business logic into a separate directory" approach which made it so simple to add new versions. We added too many of them in the end, thus it got hellishly hard to maintain or get rid of these versions.

### How easy is it to delete an old version?

Your framework must be able to let you clean up versions as simply as possible and cheaply whenever you need to. For example, if your framework tries to minimize the amount of code duplication in your repository by having new routes include old routes within them and new business logic inherit from classes from old business logic, then deleting an old version is going to be painful; oftentimes even dangerous as versions can quickly start interacting with each other in all sorts of ways, turning a single small application into a set of interconnected applications.

### How easy is it to see the differences between versions?

The easier it is, the better off our users are.

### What exactly do you need to duplicate to create a new version?

The less we duplicate and maintain manually, the easier it is to support. However, the less we duplicate, the higher the risk of breaking old versions with new releases.

### How easy is it to notice accidental data versioning?

Data versioning is an incredibly big issue when versioning your API.
So if your framework makes it hard to version data -- it's really good!
