"""Pytest collection policy.

These files are executable GH/ЯД smoke jobs whose historical names end in
``_test.py``.  They require cloud credentials, media and heavyweight optional
dependencies, so importing them as unit tests is both misleading and unsafe.
"""

collect_ignore = [
    "supervision_test.py",
    "transition_assemble_test.py",
    "screenplay_pipeline/whisper_stage_test.py",
]
