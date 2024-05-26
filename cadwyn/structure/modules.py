import ast
import dataclasses
import inspect
from dataclasses import dataclass
from types import ModuleType

from cadwyn.exceptions import CadwynStructureError


@dataclass(slots=True)
class AlterModuleInstruction:
    module: ModuleType
    raw_import: str
    import_: ast.Import | ast.ImportFrom = dataclasses.field(init=False)

    def __post_init__(self):
        parsed_body = ast.parse(self.raw_import).body
        if len(parsed_body) > 1:
            raise CadwynStructureError(
                f"You have specified more than just a single import. This is prohibited. "
                f"Problematic string: {self.raw_import}",
            )
        if not isinstance(parsed_body[0], ast.Import | ast.ImportFrom):
            raise CadwynStructureError(
                f"You have specified a non-import statement. This is prohibited. Problematic string: {self.raw_import}",
            )
        self.import_ = parsed_body[0]

    def __hash__(self):
        return hash((inspect.getsourcefile(self.module), self.raw_import))


@dataclass(slots=True)
class AlterModuleInstructionFactory:
    module: ModuleType

    def had(self, *, import_: str) -> AlterModuleInstruction:
        return AlterModuleInstruction(self.module, import_)


def module(module: ModuleType, /) -> AlterModuleInstructionFactory:
    return AlterModuleInstructionFactory(module)
