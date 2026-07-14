"""Per-family CLI command modules for ``akcli`` (see ``..cli``).

Each module exposes ``register(subparsers, common)`` (attaches its argparse
subparsers, wiring each to a handler) plus its ``_cmd_*`` handlers. Shared
helpers live in ``._shared``. ``..cli.build_parser`` calls every module's
``register`` in turn; ``..cli.main`` dispatches to the selected handler.
"""
