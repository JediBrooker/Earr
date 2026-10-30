import argparse
import sys

from scripts.readme import sections


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.readme",
        description="Regenerate the parts of README.md that come from the code.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    tables = commands.add_parser(
        "tables", help="rewrite the generated tables in README.md"
    )
    _ = tables.add_argument(
        "--check",
        action="store_true",
        help="only report whether README.md is out of date, exiting 1 if it is",
    )
    _ = commands.add_parser(
        "images", help="take the screenshots and render the logo and feature graphic"
    )
    _ = commands.add_parser("all", help="tables, then images")
    args = parser.parse_args()
    command = str(args.command)  # pyright: ignore[reportAny]

    if command in ("tables", "all"):
        check = bool(getattr(args, "check", False))
        changed = sections.update(check=check)
        if check and changed:
            print("README.md is out of date. Run: just readme tables", file=sys.stderr)
            return 1
        print("README.md tables " + ("updated" if changed else "already up to date"))

    if command in ("images", "all"):
        # imported late: it needs playwright, which only the readme group installs
        from scripts.readme import capture

        capture.main()

    return 0


if __name__ == "__main__":
    sys.exit(main())
