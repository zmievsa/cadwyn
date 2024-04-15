# How to build a versioning framework

## Questions to ask yourself when rating your framework

### How easy it is to create a version?

If it is too easy: it is probably a trap. The framework is probably hiding too much complexity from you and will shoot you in the back later. For example, early on we have tried a simple "copy entire business logic into a separate directory" approach which made it so simple to add new versions that we added too many of them -- at the end, maintaining and getting rid of these versions has gotten hellishly hard.

### How easy is it to delete an old version?

Your framework must make it as simple as possible to make sure that you can clean up versions cheaply whenever you need to. For example, if your framework tries to minimize the amount of code duplication in your repository by having new routes include old routes within them and new business logic inherit from classes from old business logic, then deleting an old version is going to be painful; oftentimes even dangerous as versions can quickly start interacting with each other in all sorts of ways, turning a single small application into a set of interconnected applications.

### How easy is it to see the differences between versions?

The easier it is -- the better off our users are.

### What exactly do you need to duplicate to create a new version?

The less we duplicate and maintain by hand -- the easier it is to support. However, the less we duplicate -- the more chance there is to break the old versions with new releases.

### How easy is it to notice accidental data versioning?

Data versioning is an incredibly big problem when versioning your API so if your framework makes it hard to version data -- it's really good!
