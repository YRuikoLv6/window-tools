"""JSON 持久化：原子写入，避免异常中断导致配置损坏。"""
from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Dict


def _merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


class JsonStore:
    def __init__(self, path: str, default: Dict[str, Any]):
        self.path = path
        self.default = copy.deepcopy(default)
        self.data: Dict[str, Any] = copy.deepcopy(default)
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    self.data = _merge(copy.deepcopy(self.default), loaded)
            except (OSError, ValueError, TypeError):
                # 配置损坏时保留默认值，不阻断启动
                self.data = copy.deepcopy(self.default)

    def save(self) -> bool:
        with self._lock:
            try:
                folder = os.path.dirname(self.path)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                return True
            except (OSError, TypeError, ValueError):
                return False

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)
