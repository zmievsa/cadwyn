from cadwyn import VersionChange, schema
from users import User


class ReplaceUserAddressesWithAddress(VersionChange):
    """Users now expose a single 'address' instead of 'addresses' because only
    one mailing address is supported.
    """

    instructions_to_migrate_to_previous_version = (
        schema(User).field("address").didnt_exist,
        schema(User).field("addresses").existed_as(type=list[str]),
    )
