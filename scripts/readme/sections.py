"""The tables in README.md that are generated from the code.

A generated block looks like this, and everything between the markers is
replaced by the generator of that name:

    <!-- BEGIN GENERATED: env -->
    ...
    <!-- END GENERATED: env -->

Descriptions come from the docstrings written under class attributes, which
Python throws away at runtime, so they are read from the source with `ast`.
"""

import ast
import re
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

_BLOCK = re.compile(
    r"(?P<begin><!-- BEGIN GENERATED: (?P<name>[a-z0-9-]+) -->\n)"
    + r"(?P<body>.*?)"
    + r"(?P<end><!-- END GENERATED: (?P=name) -->)",
    re.DOTALL,
)


# --- helpers ------------------------------------------------------------------


def attribute_docstrings(path: Path, class_name: str) -> dict[str, str]:
    """Maps each attribute of a class to the docstring written right under it."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        docs: dict[str, str] = {}
        for stmt, following in zip(node.body, node.body[1:]):
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = stmt.target.id
            elif (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                name = stmt.targets[0].id
            else:
                continue
            if (
                isinstance(following, ast.Expr)
                and isinstance(following.value, ast.Constant)
                and isinstance(following.value.value, str)
            ):
                docs[name] = " ".join(following.value.value.split())
        return docs
    raise LookupError(f"class {class_name} not found in {path}")


def code(value: str) -> str:
    return f"`{value}`" if value else ""


def _cell(value: str) -> str:
    # a | inside a cell ends it, even inside backticks, unless escaped
    return " ".join(value.split()).replace("|", "\\|")


def table(headers: list[str], rows: Iterable[list[str]]) -> str:
    lines = [
        "| " + " | ".join(_cell(h) for h in headers) + " |",
        "|" + "|".join(" --- " for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


# --- generators ---------------------------------------------------------------


def env() -> str:
    """Every environment variable, from the Settings model."""
    from pydantic import BaseModel

    from app.internal.env_settings import Settings

    source = ROOT / "app/internal/env_settings.py"
    rows: list[list[str]] = []
    # the application settings matter to everyone, the database ones to few
    groups = sorted(Settings.model_fields.items(), key=lambda item: item[0] != "app")
    for group, group_field in groups:
        model = group_field.annotation
        assert isinstance(model, type) and issubclass(model, BaseModel)
        docs = attribute_docstrings(source, model.__name__)
        for name, field in model.model_fields.items():
            default: object = field.default  # pyright: ignore[reportAny]
            if isinstance(default, bool):
                default = str(default).lower()
            rows.append(
                [
                    code(f"EARR_{group}__{name}".upper()),
                    code(str(default)),
                    docs.get(name, ""),
                ]
            )
    return table(["Variable", "Default", "Description"], rows)


def placeholders() -> str:
    from app.internal.library.naming import TOKEN_DESCRIPTIONS

    return table(
        ["Placeholder", "Value"],
        ([code("{" + token + "}"), text] for token, text in TOKEN_DESCRIPTIONS.items()),
    )


def template_examples() -> str:
    from app.internal.library.naming import (
        SAMPLE_BOOKS,
        TEMPLATE_EXAMPLES,
        render_preview,
    )

    return table(
        ["Template", *(sample.label for sample in SAMPLE_BOOKS)],
        (
            [code(template), *(code(path) for _, path in render_preview(template))]
            for template in TEMPLATE_EXAMPLES
        ),
    )


def notification_events() -> str:
    from app.internal.models import EventEnum

    docs = attribute_docstrings(ROOT / "app/internal/models.py", "EventEnum")
    return table(
        ["Event", "Sent when"],
        ([code(event.value), docs.get(event.name, "")] for event in EventEnum),
    )


# Descriptions for the variables found in the code. A variable that is missing
# here is still listed, just without a description.
VARIABLE_HINTS: dict[str, str] = {
    "declineReason": "The reason an admin gave for declining a request",
    "issueKind": "What kind of problem was reported, e.g. Bad audio",
    "issueMessage": "What the reporter wrote about it",
    "issueReporter": "Who reported the problem",
    "issueResolution": "How an admin resolved it",
    "eventType": "Name of the event, e.g. onNewRequest",
    "eventUser": "First user who requested the book",
    "eventUserExtraData": "That user's extra data, set under Settings > Users",
    "joinedUsers": "Everyone who requested the book, comma separated",
    "joinedUsersExtraData": "Their extra data, comma separated",
    "bookTitle": "Book title",
    "bookAuthors": "Authors, separated by commas",
    "bookNarrators": "Narrators, separated by commas",
    "bookCover": "Cover image URL, or a placeholder image when there is none",
    "bookASIN": "Audible ASIN",
    "sourceTitle": "Name of the release that was grabbed",
    "sourceSizeMB": "Size of that release in MB",
    "sourceProtocol": "torrent or usenet",
    "indexerName": "Indexer the release came from",
    "torrentInfoHash": "Info hash, for torrents",
    "targetPath": "Folder the book was organized into",
    "errorStatus": "HTTP status Prowlarr answered with",
    "errorReason": "Reason Prowlarr gave",
}


def _func_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _string_keys(node: ast.expr | None) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    return {
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _is_replacements(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id.endswith("replacements")


def find_notification_variables() -> set[str]:
    """Scans app/ for every {variable} a notification body can contain."""
    found: set[str] = set()
    for path in sorted((ROOT / "app").rglob("*.py")):
        is_notifications = path.name == "notifications.py"
        for node in ast.walk(ast.parse(path.read_text())):
            # template.replace("{bookTitle}", ...)
            if (
                is_notifications
                and isinstance(node, ast.Call)
                and _func_name(node.func) == "replace"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and (match := re.fullmatch(r"\{(\w+)\}", node.args[0].value))
            ):
                found.add(match.group(1))
            # additional_replacements["sourceTitle"] = ...
            elif (
                isinstance(node, ast.Subscript)
                and _is_replacements(node.value)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                found.add(node.slice.value)
            # send_all_notifications(event, asin, {"errorStatus": ...})
            elif isinstance(node, ast.Call) and re.fullmatch(
                r"send_\w*notifications?", _func_name(node.func)
            ):
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    found |= _string_keys(arg)
            # replacements = {"targetPath": ...}
            elif isinstance(node, ast.Assign) and any(
                _is_replacements(t) for t in node.targets
            ):
                found |= _string_keys(node.value)
            elif isinstance(node, ast.AnnAssign) and _is_replacements(node.target):
                found |= _string_keys(node.value)
    return found


def notification_variables() -> str:
    order = list(VARIABLE_HINTS)
    variables = sorted(
        find_notification_variables(),
        key=lambda v: (order.index(v) if v in order else len(order), v),
    )
    return table(
        ["Variable", "Value"],
        ([code("{" + v + "}"), VARIABLE_HINTS.get(v, "")] for v in variables),
    )


GENERATORS: dict[str, Callable[[], str]] = {
    "env": env,
    "placeholders": placeholders,
    "template-examples": template_examples,
    "notification-events": notification_events,
    "notification-variables": notification_variables,
}


# --- README -------------------------------------------------------------------


def render(text: str) -> str:
    """Fills every generated block in `text`."""
    seen: set[str] = set()

    def fill(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in GENERATORS:
            raise KeyError(f"README.md has a block for unknown generator {name!r}")
        seen.add(name)
        body = "\n" + GENERATORS[name]().rstrip() + "\n\n"
        return match.group("begin") + body + match.group("end")

    rendered = _BLOCK.sub(fill, text)
    if missing := set(GENERATORS) - seen:
        raise KeyError(f"README.md has no block for {', '.join(sorted(missing))}")
    return rendered


def update(check: bool = False) -> bool:
    """Regenerates README.md. Returns whether anything changed (or would)."""
    current = README.read_text()
    rendered = render(current)
    if rendered == current:
        return False
    if not check:
        _ = README.write_text(rendered)
    return True
