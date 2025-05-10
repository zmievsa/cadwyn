from datetime import date
from typing import Annotated

import typer
from rich.console import Console
from rich.syntax import Syntax

from cadwyn._render import render_model_by_path, render_module_by_path

_CONSOLE = Console()
_RAW_ARG = Annotated[bool, typer.Option(help="Output code without color")]

app = typer.Typer(
    name="cadwyn",
    add_completion=False,
    help="Modern Stripe-like API versioning in FastAPI",
)

render_subapp = typer.Typer(
    name="render",
    add_completion=False,
    help="Render pydantic models and enums from a certainn version and output them to stdout",
)

app.add_typer(render_subapp)


def version_callback(value: bool):
    if value:
        from . import __version__

        typer.echo(f"Cadwyn {__version__}")
        raise typer.Exit


def output_code(code: str, raw: bool):
    if raw:
        typer.echo(code)
    else:  # pragma: no cover
        _CONSOLE.print(Syntax(code, "python", line_numbers=True))


@render_subapp.command(
    name="model",
    help="Render a concrete pydantic model or enum from a certain version and output it to stdout",
    short_help="Render a single model or enum",
)
def render(
    model: Annotated[str, typer.Argument(metavar="<module>:<attribute>", help="Python path to the model to render")],
    app: Annotated[str, typer.Option(metavar="<module>:<attribute>", help="Python path to the main Cadwyn app")],
    version: Annotated[str, typer.Option(parser=lambda s: str(date.fromisoformat(s)), metavar="ISO-VERSION")],
    raw: _RAW_ARG = False,
) -> None:
    output_code(render_model_by_path(model, app, version), raw)


@render_subapp.command(
    name="module",
    help="Render all versioned models and enums within a module from a certain version and output them to stdout",
    short_help="Render all models and enums from an entire module",
)
def render_module(
    module: Annotated[str, typer.Argument(metavar="<module>", help="Python path to the module to render")],
    app: Annotated[str, typer.Option(metavar="<module>:<attribute>", help="Python path to the main Cadwyn app")],
    version: Annotated[str, typer.Option(parser=lambda s: str(date.fromisoformat(s)), metavar="ISO-VERSION")],
    raw: _RAW_ARG = False,
) -> None:
    output_code(render_module_by_path(module, app, version), raw)


@app.callback()
def main(
    version: bool = typer.Option(None, "-V", "--version", callback=version_callback, is_eager=True),
): ...


if __name__ == "__main__":
    app()
