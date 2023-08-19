from .schemas.unions.users import UserCreateRequestUnion  # pyright: ignore[reportMissingImports]


class UserScenario:
    async def create_user(self, payload: UserCreateRequestUnion):
        return {
            "id": 83,
            "_prefetched_addresses": [{"id": 100, "value": payload.default_address}],
        }

    async def get_user(self, user_id: int):
        return {
            "id": user_id,
            "_prefetched_addresses": (await self.get_user_addresses(user_id))["data"],
        }

    async def get_user_addresses(self, user_id: int):
        return {
            "data": [
                {"id": 83, "value": "123 Example St"},
                {"id": 91, "value": "456 Main St"},
            ],
        }
