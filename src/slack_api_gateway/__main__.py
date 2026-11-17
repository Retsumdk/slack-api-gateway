"""``python -m slack_api_gateway`` delegates to the CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
