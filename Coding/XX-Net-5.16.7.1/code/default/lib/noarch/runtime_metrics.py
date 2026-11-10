"""进程内白名单运行指标；只保存数值，不保存业务标识。"""

import math
import threading


class CounterSet:
    def __init__(self, names):
        self._values = {name: 0 for name in names}
        self._lock = threading.Lock()

    def increment(self, name, amount=1):
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise TypeError("counter increment must be an integer")
        if amount <= 0:
            raise ValueError("counter increment must be positive")
        with self._lock:
            if name not in self._values:
                raise KeyError(name)
            self._values[name] += amount

    def snapshot(self):
        with self._lock:
            return dict(self._values)


class NumberSum:
    """非负浮点累计值，避免把秒数塞进正整数 Counter。"""

    def __init__(self):
        self._value = 0.0
        self._lock = threading.Lock()

    def add(self, amount):
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise TypeError("number sum increment must be numeric")
        if amount < 0 or not math.isfinite(amount):
            raise ValueError("number sum increment must be finite and non-negative")
        with self._lock:
            self._value += amount

    def snapshot(self):
        with self._lock:
            return self._value
