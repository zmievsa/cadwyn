# How we got here

Over the years the Cadwyn dev team have come across a number of approaches to do API versioning. There are three major approaches described below, ordered by decreasing level of duplication.

## Approaches to API versioning

There are three main approaches to version an API, each one less safe than the last but more convenient to both the API clients and maintainers. ([source 1](https://smartlogic.io/blog/2012-12-12-developing-an-api/), [source 2](https://thenewstack.io/tricks-api-versioning/))  They can be classified by which layers of [MVC](https://en.wikipedia.org/wiki/Model%E2%80%93view%E2%80%93controller) they version.

### 1. Versioning a proxy, which routes requests to versioned apps

This approach versions all three layers: separate data, separate business logic, separate representation. Actually a completely different app is created for each version. The versions are independent and cannot affect each other. Any changes can be made in future versions without worrying about breaking the old ones.

This approach is the most expensive to support but if breaking the old functionality is unacceptable and if a small number of versions (1-3) needs to be supported, then this option is relevant.

Note that this approach is essentially **data** or **application** versioning, not **API** versioning. If it is impossible for your user to freely move between API versions (back and forth), then you are probably doing a bit of **data versioning** yourself. It can simplify your app's logic but will significantly inconvenience your users because they will not be able to easily switch API versions without waiting for your team to help. Additionally, a single client will never be able to use two versions at the same time. At least not easily.

*Mostly used in older-style apps or in critical infrastructure where no mistakes are permitted*

### 2. One router, which routes requests to versioned controllers

This approach versions business logic and representation layers while leaving data layer the same. You still have to duplicate all of your business logic but now your clients will be able to migrate between versions easily and you will be able to share some of the code between versions, thus lowering the amount of things you would need to duplicate.

The problem with this approach is that any refactoring will most likely have to happen in all versions at once. Any changes in the libraries they depend on will also require a change in all versions. When the number of versions starts to rise (>2), this becomes a significant problem for the performance and morale of API maintainers.

This is also the approach the Cadwyn dev team have originally started with. It is likely the worst one out there due to its fake simplicity and actual complexity. In the long run, this approach is one of the hardest to support but most importantly: it is probably the **hardest to migrate from**.

*Popular in [.NET environment](https://github.com/dotnet/aspnet-api-versioning) and is likely the first choice of any API due to the simplicity of its implementation*

### 3. One router, shared controllers, which respond with versioned representations

This approach versions only the API itself. The business logic and data structures are the same for all API versions (with rare exceptions) so API maintainers have the pleasure of maintaining only one API version while users have the added benefit that non-breaking featurees and bugfixes will automatically be ported to their version. This is the only approach that allows you to support a large number of versions because it has the least amount of duplication of all approaches. This is usually accomplished by adding a separate layer that builds responses out of the data that your service returns. It can be a separate service, a framework, or just a set of functions.

Note that in this approach, the usage of **data versioning** now becomes an inconvenience to **both** API users and maintainers. See, when you have a single business logic for all versions, you might need additional conditionals and checks for versions where data structure or data itself has changed. That is **in addition** to pre-existing inconveniences for the users. However, sometimes it might still happen so our goal is to minimize the frequency and impact of data versioning.

*Popular in API-First tech giants that have to support backwards compatibility for a long time for a large number of clients*

Note that this approach actually has two important subtypes:

#### i. Duplication-based response building

The simplest possible builder: for each API version a new request/response builder is defined that builds the full response for the altered API routes or migrates the user request to the latest version. It is incredibly simple to implement but is not scalable at all. Adding values to all builders will require going through all of them with the hope of not making mistakes or typos. Trying to support more than 8-12 versions with this approach will be challenging.

One might think of smart ways of automating this approach to support a larger number of versions. For example, to avoid duplicating the entire builder logic every time, one can pick a template builder and only define differences in child builders. You can pick the latest-version builder as template because it will never be deprecated or deleted and your developers will have the most familiarity with it. Then you need to figure out a format to define changes between builders. You can remove a field from response, add a field, change the value of a field somehow, and/or change the type of a field. You will need some DSL to describe all possible changes.

Then it is time to start thinking about API route differences. How to describe them? Maybe just duplicate all routes? Maybe use inheritance? No matter what, one will eventually come to a DSL, which is why some tech giants have chosen [approach ii](#ii-migration-based-response-building).

A code generation yaml-based version of this approach [was used at SuperJob](https://habr.com/ru/companies/superjob/articles/577650/).

#### ii. Migration-based response building

This is an automated version of [approach i](#i-duplication-based-response-building). It has the minimal possible amount of duplication compared to all other approaches. Using a specialized DSL, schema migrations are defined for changes in request and response schemas, compatibility gates are defined to migrate data in accordance with schema changes, and route migrations are defined to change/delete/add any routes.

This is the approach that [Stripe](https://stripe.com/blog/api-versioning), [Linkedin](https://engineering.linkedin.com/blog/2022/-under-the-hood--how-we-built-api-versioning-for-linkedin-market), and [Intercom](
https://www.intercom.com/blog/api-versioning/) have chosen and this is the approach that **Cadwyn** implements for you.
