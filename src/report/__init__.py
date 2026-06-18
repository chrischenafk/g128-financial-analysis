"""Report layer: local pre-processing for the external skill call.

``builder.py`` runs the skill's deterministic preprocessing scripts
(``engine/load_package.py`` → ``package.json``, ``engine/charts.py`` → chart
PNGs) as subprocesses, so the skill receives the exact ``package.json`` it
expects plus pre-rendered charts — instead of reconstructing them from
conversation text. The scripts under ``engine/`` are a verbatim mirror of the
external skill and are run, never imported. See ``engine/VENDORED.md``.
"""
