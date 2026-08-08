import pytest

from app.plugins.manager import PluginManager
from app.plugins.registry import plugin_registry, PluginRegistry
from app.plugins.base import BasePlugin, PluginMetadata, ToolPlugin


class DemoToolPlugin(ToolPlugin):
    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(name="demo", version="1.0.0")

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def get_tool_definition(self):
        return {"name": "demo"}

    async def execute(self, **kwargs) -> dict:
        return {"ok": True}


@pytest.mark.unit
class TestPluginRegistry:
    async def test_register_and_lookup(self) -> None:
        registry = PluginRegistry()
        plugin = DemoToolPlugin()
        registry.register(plugin)
        assert registry.get_plugin("demo") is plugin

    async def test_lookup_missing(self) -> None:
        registry = PluginRegistry()
        assert registry.get_plugin("missing") is None


@pytest.mark.unit
class TestPluginManager:
    async def test_initialize_all(self) -> None:
        # manager 内部维护 _loaded_plugins，手动加载后初始化/关闭
        plugin = DemoToolPlugin()
        manager = PluginManager()
        manager._loaded_plugins.append(plugin)
        await manager.initialize_all()
        await manager.shutdown_all()

    async def test_broken_plugin_isolation(self) -> None:
        class BrokenToolPlugin(ToolPlugin):
            @property
            def metadata(self) -> PluginMetadata:
                return PluginMetadata(name="broken", version="1.0.0")

            async def initialize(self) -> None:
                raise RuntimeError("boom")

            async def shutdown(self) -> None:
                pass

            def get_tool_definition(self):
                return {"name": "broken"}

            async def execute(self, **kwargs) -> dict:
                return {}

        plugin = BrokenToolPlugin()
        manager = PluginManager()
        manager._loaded_plugins.append(plugin)
        # 初始化异常应被隔离，不抛错
        await manager.initialize_all()
        # 损坏插件仍被记录但初始化失败；shutdown_all 不应抛错
        await manager.shutdown_all()
