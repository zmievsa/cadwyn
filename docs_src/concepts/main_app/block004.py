from cadwyn import Cadwyn, Version, VersionBundle

my_version_bundle = VersionBundle(Version("2022-11-16"))

app = Cadwyn(
    versions=my_version_bundle,
    swagger_js_url="/static/swagger-ui-bundle.js",
    swagger_css_url="/static/swagger-ui.css",
    swagger_favicon_url="/static/favicon.png",
    redoc_js_url="/static/redoc.standalone.js",
    redoc_favicon_url="/static/favicon.png",
)
