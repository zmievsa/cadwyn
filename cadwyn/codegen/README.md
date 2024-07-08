# Codegen

main.py -- contains the real core of codegen
asts.py -- contains the majority of ast-related utilities in cadwyn
common.py -- contains all the data structures used by cadwyn code generation and its plugins
plugins/class_rebuilding.py -- plugin for building enums + schemas from their versions in context
plugins/class_renaming.py -- plugin for globally renaming classes such as schemas or enums
plugins/import_auto_adding.py -- plugin for adding missing imports from other files or libraries

You are free to overwrite any and all default plugins using the optional arguments of `generate_code_for_versioned_packages`.