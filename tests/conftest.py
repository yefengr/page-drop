import pytest


@pytest.fixture(autouse=True)
def isolate_client_config_directory(monkeypatch):
    # 不让宿主或容器的私有配置覆盖测试夹具；专项测试可在用例内显式设置。
    monkeypatch.delenv("PAGE_DROP_CONFIG_DIR", raising=False)
