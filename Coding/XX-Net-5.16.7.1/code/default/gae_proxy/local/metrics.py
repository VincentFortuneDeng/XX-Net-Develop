"""固定 Front 类别的白名单运行指标。"""

from front_base import metrics as front_metrics


def snapshot():
    return {"front.by_type": front_metrics.snapshot()}
