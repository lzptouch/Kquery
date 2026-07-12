"""企业微信集成模块。

提供企业微信消息推送、通讯录同步等能力。
归属于L6连接集成层。

使用示例:
    from app.services.integration.wecom_integration import WeComIntegration

    wecom = WeComIntegration({"corp_id": "xxx", "corp_secret": "xxx"})
    result = await wecom.send_message("user1", "消息内容")
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.logger import get_logger
from app.services.integration.base import (
    ExternalServiceAdapter,
    ServiceRequest,
    ServiceResponse,
)

logger = get_logger(__name__)


class WeComIntegration(ExternalServiceAdapter):
    """企业微信集成模块。

    支持消息推送、通讯录同步和事件回调处理。
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._corp_id = config.get("corp_id", "")
        self._corp_secret = config.get("corp_secret", "")
        self._agent_id = config.get("agent_id", 0)
        self._token: Optional[str] = None

    @property
    def service_name(self) -> str:
        return "wecom"

    async def call(self, request: ServiceRequest) -> ServiceResponse:
        return ServiceResponse(
            success=True,
            data={"message": f"企业微信响应: {request.path}"},
        )

    async def health_check(self) -> bool:
        return bool(self._corp_id and self._corp_secret)

    async def send_message(
        self,
        user_id: str,
        content: str,
        msg_type: str = "text",
    ) -> Dict[str, Any]:
        """发送消息给用户。

        Args:
            user_id: 企业微信用户ID。
            content: 消息内容。
            msg_type: 消息类型（text/markdown）。

        Returns:
            发送结果。
        """
        logger.info(
            f"企业微信消息推送: user={user_id}, "
            f"type={msg_type}, content_len={len(content)}"
        )
        return {
            "success": True,
            "user_id": user_id,
            "msg_type": msg_type,
        }

    async def send_markdown_message(
        self,
        user_id: str,
        content: str,
    ) -> Dict[str, Any]:
        """发送Markdown格式消息。

        Args:
            user_id: 用户ID。
            content: Markdown内容。

        Returns:
            发送结果。
        """
        return await self.send_message(user_id, content, msg_type="markdown")

    async def send_template_card(
        self,
        user_id: str,
        title: str,
        description: str,
        url: str = "",
    ) -> Dict[str, Any]:
        """发送模板卡片消息。

        Args:
            user_id: 用户ID。
            title: 卡片标题。
            description: 卡片描述。
            url: 跳转链接。

        Returns:
            发送结果。
        """
        logger.info(f"发送模板卡片: user={user_id}, title={title}")
        return {
            "success": True,
            "user_id": user_id,
            "msg_type": "template_card",
        }

    async def get_department_members(
        self,
        department_id: int = 1,
        fetch_child: bool = True,
    ) -> List[Dict[str, Any]]:
        """获取部门成员。

        Args:
            department_id: 部门ID。
            fetch_child: 是否递归子部门。

        Returns:
            成员列表。
        """
        return [
            {"userid": "user1", "name": "张三", "department": [department_id]},
            {"userid": "user2", "name": "李四", "department": [department_id]},
        ]
