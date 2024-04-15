import ast
import dataclasses
from dataclasses import InitVar, dataclass
from types import ModuleType

from cadwyn.exceptions import CadwynStructureError


@dataclass(slots=True)
class AlterModuleInstruction:
    module: ModuleType
    raw_import: InitVar[str]
    import_: ast.Import | ast.ImportFrom = dataclasses.field(init=False)

    def __post_init__(self, raw_import: str):
        parsed_body = ast.parse(raw_import).body
        if len(parsed_body) > 1:
            raise CadwynStructureError(
                f"You have specified more than just a single import. This is prohibited. "
                f"Problematic string: {raw_import}",
            )
        if not isinstance(parsed_body[0], ast.Import | ast.ImportFrom):
            raise CadwynStructureError(
                f"You have specified a non-import statement. This is prohibited. Problematic string: {raw_import}",
            )
        self.import_ = parsed_body[0]


@dataclass(slots=True)
class AlterModuleInstructionFactory:
    module: ModuleType

    def had(self, *, import_: str) -> AlterModuleInstruction:
        return AlterModuleInstruction(self.module, import_)


def module(module: ModuleType, /) -> AlterModuleInstructionFactory:
    return AlterModuleInstructionFactory(module)
