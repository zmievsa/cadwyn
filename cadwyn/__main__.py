import importlib
import sys
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax
from typing_extensions import Annotated, Any

from cadwyn._render import render_model_by_path
from cadwyn.exceptions import CadwynError
from cadwyn.structure.versions import VersionBundle

_CONSOLE = Console()

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


@render_subapp.command(
    name="model",
    help="Render a concrete pydantic model or enum from a certain version and output it to stdout",
    short_help="Render a single model or enum",
)
def render(
    model: Annotated[str, typer.Argument(metavar="<module>:<attribute>", help="Python path to the model to render")],
    app: Annotated[str, typer.Option(metavar="<module>:<attribute>", help="Python path to the main Cadwyn app")],
    version: Annotated[str, typer.Option(parser=lambda s: str(date.fromisoformat(s)), metavar="ISO-VERSION")],
) -> None:
    rendered_model = render_model_by_path(model, app, version)
    _CONSOLE.print(Syntax(rendered_model, "python"))


@app.callback()
def main(
    version: bool = typer.Option(None, "-V", "--version", callback=version_callback, is_eager=True),
): ...


if __name__ == "__main__":
    app()
