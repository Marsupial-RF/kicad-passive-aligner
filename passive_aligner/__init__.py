"""KiCad plugin entry point; CLI/tests explicitly opt out of registration."""
import os

if os.environ.get('PASSIVE_ALIGNER_HEADLESS') != '1':
    from .plugin import PassiveAligner
    PassiveAligner().register()
