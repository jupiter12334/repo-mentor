"""基于 s15 四个扩展点的实例级钩子注册表。"""

from collections import defaultdict


class Hooks:
    def __init__(self):
        self.callbacks = defaultdict(list)

    def register(self, event: str, callback):
        self.callbacks[event].append(callback)

    def trigger(self, event: str, *args):
        # 保留课程的顺序执行与首个非空返回值语义。
        for callback in self.callbacks[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None
