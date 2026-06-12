"""Simple planar projection of DJI nadir close-up drone photos onto a DSM.

The public entry point is :func:`closeup_ortho.orthorectify.orthorectify_folder`.
"""

from closeup_ortho.orthorectify import orthorectify_folder
from closeup_ortho.labelbox import project_labels

__all__ = ["orthorectify_folder", "project_labels"]
__version__ = "0.1.0"
