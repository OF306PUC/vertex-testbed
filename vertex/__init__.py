"""Testbed for distributed and multi-agent control over real heterogeneous radio links.

Import subpackages directly -- ``from vertex.wire import StatePacket`` --

Subpackages, cheapest first:

``vertex.numeric``      fixed-point boundary and the rounding rule (stdlib only)
``vertex.net``          ports, addressing, interface resolution (stdlib only)
``vertex.wire``         state packet codec and per-link quality accounting
``vertex.controllers``  coordination algorithms (numpy)
``vertex.topology``     manifests, generators, graph preconditions (pydantic, networkx, yaml)
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
