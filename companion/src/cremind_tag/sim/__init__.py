"""Simulator of the gateway, mesh, bridges and tags speaking the real serial protocol (docs/simulator.md)."""

from .bridge import BridgeFaults, SimBridge
from .core import SimClock, rng_stream
from .gateway import SimGateway
from .mesh import MeshFaults, MeshTiming
from .radio import AirFaults
from .tag import SimTag, TagFaults, TagNvs, TagSpec
from .world import Assign, BridgeSpec, FaultSpecError, SimConfig, SimFaults, Simulator, SimulatorThread, parse_fault

__all__ = [
    "AirFaults", "Assign", "BridgeFaults", "BridgeSpec", "FaultSpecError", "MeshFaults", "MeshTiming", "SimBridge",
    "SimClock", "SimConfig", "SimFaults", "SimGateway", "SimTag", "Simulator", "SimulatorThread", "TagFaults",
    "TagNvs", "TagSpec", "parse_fault", "rng_stream",
]
