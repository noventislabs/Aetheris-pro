"""Position intelligence: brains, metrics and the decision orchestrator.

Pure arithmetic over a position, its recorded thesis and observed market
data. Nothing here performs I/O, submits an order, moves a stop or changes
leverage -- the whole package is read-and-reason, and the orchestrator's
output is advisory.
"""
