"""The hub: orchestration and collection. No control law and no telemetry.

It configures every agent from one manifest, triggers them, waits, stops them, and
pulls their logs back. The agents own their data until it is fetched; the hub owns
the schedule.
"""
from .runner import ExperimentRunner, NodeOutcome, RunReport

__all__ = ["ExperimentRunner", "RunReport", "NodeOutcome"]
