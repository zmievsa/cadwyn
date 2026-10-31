from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_location="custom_header",
    api_version_parameter_name="x-api-version",
    versions=VersionBundle(Version("2022-01-02")),
)
