# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetSuite service layer — identity reconciliation, control gating, the
build orchestrator, the pytest runner, and the Datadog forwarder. These hold
the harness's domain logic; the REST/WS layer in ``app.py`` is a thin shell
over them."""
