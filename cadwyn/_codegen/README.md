# Codegen

main.py -- contains the real core of codegen
asts.py -- contains the majority of ast-related utilities in cadwyn
common.py -- contains all the data structures used by cadwyn code generation and its plugins
plugins/class_rebuilding.py -- plugin for building enums + schemas from their versions in context
plugins/class_renaming.py -- plugin for globally renaming classes such as schemas or enums
plugins/import_auto_adding.py -- plugin for adding missing imports from other files or libraries
plugins/latest_version_aliasing.py -- plugin for using `from latest import *` within the latest **generated** parallel directory instead of just duplicating everything

You are free to overwrite any and all default plugins using the optional arguments of `generate_code_for_versioned_packages`. Please note our use of `ast_comments` module for preserving comments that we add. Please also note that codegen migrations apply strictly to the toplevel of the module. So if you want to visit some inner structure -- you'd have to find it yourself.
