import ast


def get_absolute_python_path_of_import(node: ast.ImportFrom, module_python_path: str):
    python_path = ".".join(module_python_path.split(".")[0 : -node.level])
    result = []
    if node.module:
        result.append(node.module)
    if python_path:
        result.append(python_path)
    return ".".join(result)


def get_pythonpath(cls: type):
    return f"{cls.__module__}.{cls.__name__}"
