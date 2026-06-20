"""Eval harness for measuring agent quality against a scenario corpus (B-060).

Inspired by the GAIA/AMD eval framework but tailored to CorpClaw Lite's typed
office tools and local-LLM target. The harness drives the full agent loop
headless, records trajectories, and scores them with a deterministic pre-check /
zero-rule layer plus an optional cloud LLM judge (7-dimension rubric).

The calibration package (:mod:`corpclaw_lite.calibration`) is the closest
existing analog and a self-improvement loop; this package reuses its
:class:`~corpclaw_lite.calibration.trajectory.TrajectoryRecorder` and the
build → run → capture pattern, but replaces the deterministic tool-subsequence
scorer with a GAIA-style correctness judge and adds A/B guard toggling.
"""

from __future__ import annotations

__all__: list[str] = []
