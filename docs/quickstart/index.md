
# Quickstart

This guide provides a step-by-step tutorial for setting up automatic API versioning using Cadwyn. I will illustrate this with an example of a User API, where we will be implementing changes to a User's address. You can also see the advanced version of the service from this tutorial [here](https://github.com/zmievsa/cadwyn/tree/main/tests/tutorial).

Adding a new API version in Cadwyn consists of three main steps:

1. Make the breaking change
2. Use Cadwyn to describe how to revert the breaking change
3. Use Cadwyn to generate schemas and routes that existed before the breaking change

In this guide, we'll prepare an environment for working with Cadwyn effectively with a basic usage example, make a breaking change, and then show how Cadwyn can help us keep the old API versions untouched.
