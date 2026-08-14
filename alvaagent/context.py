"""Runtime context object — replaces the single-file module globals."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading
from typing import Callable, Optional

@dataclass
class Runtime:
    data_dir: str
    cfg: dict = field(default_factory=dict)
    store: dict = field(default_factory=dict)
    tool_mode: str = "core"
    approved: set = field(default_factory=set)
    cancel: threading.Event = field(default_factory=threading.Event)
    on_permission: Optional[Callable] = None
    on_tool: Optional[Callable] = None
    spinner: object = None
    skin: str = "midnight"
    session: str = "default"
    history: list = field(default_factory=list)
    last_turn: dict = field(default_factory=dict)

    @property
    def active_cfg(self) -> dict:
        return self.cfg["profiles"][self.cfg["active"]]

    @property
    def skills_dir(self) -> str:
        return os.path.join(self.data_dir, "skills")


def build_runtime(data_dir=None):
    import alvaagent.config as config
    import alvaagent.store as store
    if data_dir is None:
        data_dir = config.data_dir()
    rt = Runtime(data_dir=data_dir)
    rt.cfg = config.load_state(data_dir)
    rt.tool_mode = rt.cfg.get("tool_mode", "core")
    rt.skin = rt.cfg.get("skin", "midnight")
    store.load(rt)
    return rt
