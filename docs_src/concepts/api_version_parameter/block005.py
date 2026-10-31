from cadwyn import Cadwyn, Version, VersionBundle

app = Cadwyn(
    api_version_location="path",
    api_version_parameter_name="api_version",
    versions=VersionBundle(Version("2022-01-02")),
)
