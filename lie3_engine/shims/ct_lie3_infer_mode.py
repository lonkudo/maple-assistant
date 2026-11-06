"""Stand-in for the vendor's ``ct_lie3_infer_mode`` (lives inside its frozen exe).

``onnx_local_hub`` only asks whether CT should use local ONNX. We answer False so
that this module can never flip the vendor's other code paths on our behalf; the
engine decides its own inference mode.
"""
from __future__ import annotations


def ct_uses_local_onnx_infer() -> bool:
    return False


def ct_lie3_infer_mode() -> str:
    return "local_onnx"


__all__ = ["ct_uses_local_onnx_infer", "ct_lie3_infer_mode"]
