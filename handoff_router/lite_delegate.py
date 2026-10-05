"""
title: Lite Delegate
description: Selects a Lite subagent for the current user request.
version: 1.1.0
required_open_webui_version: 0.11.1
"""

import json


class Tools:
    def lite_delegate(self, agent_id: str) -> str:
        """Delegate the current user request to one advertised Lite subagent.

        :param agent_id: Exact agent ID advertised to the orchestrator.
        :return: A handoff marker for Lite Handoff Router.
        """
        return json.dumps(
            {
                "__lite_delegate__": "v2",
                "agent_id": str(agent_id or "").strip(),
            },
            ensure_ascii=False,
        )
