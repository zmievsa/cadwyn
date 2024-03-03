# Module migrations

Oftentimes you start depending on new types in-between versions. For example, let's say that you depended on `Invoice` schema within your `data.head.users` in older versions but now you do not. This means that once we run code generation and this type gets back into some annotation of some schema in `data.head.users` -- it will not be imported because it was not imported in `latest`. To solve problems like this one, we have `module` instructions:

```python
from cadwyn.structure import VersionChange, module
import data.head.users


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        module(data.head.users).had(import_="from .invoices import Invoice"),
    )
```

Which will add-in this import at the top of `users` file in all versions before this version change.
