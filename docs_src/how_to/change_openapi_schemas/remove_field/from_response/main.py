from cadwyn import Cadwyn
from users import router
from versions import version_bundle

app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)
