from typing_extensions import assert_never

from cadwyn.codegen._common import GlobalCodegenContext
from cadwyn.structure.modules import AlterModuleInstruction


def module_migration_plugin(context: GlobalCodegenContext):
    for version_change in context.current_version.version_changes:
        for alter_module_instruction in version_change.alter_module_instructions:
            module = alter_module_instruction.module
            mutable_module = context.modules[module.__name__]
            if isinstance(alter_module_instruction, AlterModuleInstruction):
                mutable_module.extra_imports.append(alter_module_instruction.import_)
            else:
                assert_never(alter_module_instruction)
