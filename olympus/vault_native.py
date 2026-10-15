"""Compatibility name for the qualified captured-directory implementation.

The shared legacy adapter remains separate until its callers are qualified.
"""
from .vault_storage import _CapturedDirectory as Directory
