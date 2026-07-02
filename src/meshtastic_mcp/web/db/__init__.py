# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SQLite registry for FleetSuite (aiosqlite). One ``Database`` owns the
connection; the ``repo_*`` modules are stateless helpers that take it as their
first argument."""
