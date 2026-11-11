"""Deterministic planning for experiment micro-task runs.

A planner writes only the experiment matrix (``plan.yaml``). Everything else in planning is
mechanical and lossless: digesting result files, gathering Kronig context, verifying extracted
requirements against raw files, listing questions, and rendering stage tasks.
"""
