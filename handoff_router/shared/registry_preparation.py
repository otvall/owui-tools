"""Registry initialization stage shared by Router and standalone adapters."""

import asyncio
import json
from dataclasses import dataclass

from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.env import BYPASS_MODEL_ACCESS_CONTROL
from open_webui.models.models import Models
from open_webui.models.skills import Skills
from open_webui.models.users import Users
from open_webui.utils.models import check_model_access
from handoff_router.shared.request_runtime import RequestRuntime
from handoff_router.shared.skill_preparation import normalize_skill_ids


def normalize_ids(values) -> list[str]:
    result = []
    seen = set()
    for raw_value in values or []:
        value = str(raw_value or "").strip()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


@dataclass(frozen=True)
class AgentSpec:
    model_id: str
    routing_skill_id: str
    name: str


@dataclass(frozen=True)
class RegistrySnapshot:
    agents: tuple[AgentSpec, ...]

    @property
    def registry(self) -> dict[str, dict]:
        return {
            agent.model_id: {
                "model_id": agent.model_id,
                "routing_skill_id": agent.routing_skill_id,
                "name": agent.name,
            }
            for agent in self.agents
        }

    @property
    def skill_ids(self) -> list[str]:
        return [agent.routing_skill_id for agent in self.agents]


class RegistryMap(dict):
    """Dictionary wire format plus already-validated routing Skill IDs."""

    def __init__(self, snapshot: RegistrySnapshot):
        super().__init__(snapshot.registry)
        self.skill_ids = snapshot.skill_ids


class ModelAccessPolicy:
    def __init__(self, debug):
        self._debug = debug

    async def can_access(self, *, request, user, model_id: str) -> bool:
        runtime_model = request.app.state.MODELS.get(model_id)
        if runtime_model is None:
            self._debug("runtime model unavailable: %s", model_id)
            return False

        model_info = await Models.get_model_by_id(model_id)
        if model_info is None:
            is_pipe = (
                isinstance(runtime_model, dict)
                and isinstance(runtime_model.get("pipe"), dict)
                and runtime_model["pipe"].get("type") == "pipe"
            )
            if not is_pipe:
                self._debug("workspace model or pipe not found: %s", model_id)
                return False
            return BYPASS_MODEL_ACCESS_CONTROL or user.role == "admin"

        if not model_info.is_active:
            self._debug("workspace model inactive: %s", model_id)
            return False
        if BYPASS_MODEL_ACCESS_CONTROL:
            return True
        if user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL:
            return True

        try:
            await check_model_access(user, runtime_model, model_info=model_info)
        except Exception as exc:
            if str(exc) != "Model not found":
                raise
            self._debug("user=%s cannot access model=%s", user.id, model_id)
            return False
        return True


class SkillAvailabilityValidator:
    @staticmethod
    async def validate(skill_ids: list[str], *, strict: bool = True) -> list[str]:
        ids = normalize_skill_ids(skill_ids)
        skills = await asyncio.gather(*(Skills.get_skill_by_id(skill_id) for skill_id in ids))
        missing = [
            skill_id
            for skill_id, skill in zip(ids, skills, strict=True)
            if skill is None or not skill.is_active
        ]
        if strict and missing:
            raise ValueError(
                "Configured model-bound Skills are unavailable: " + ", ".join(missing)
            )
        return [
            skill_id
            for skill_id, skill in zip(ids, skills, strict=True)
            if skill is not None and skill.is_active
        ]


class SubagentCatalog:
    def __init__(self, entries: dict[str, str], access_policy: ModelAccessPolicy, debug):
        self._entries = entries
        self._access_policy = access_policy
        self._debug = debug

    async def build(self, *, request, user) -> RegistrySnapshot:
        candidates = await asyncio.gather(
            *(
                self._load_agent(
                    request=request,
                    user=user,
                    model_id=str(model_id or "").strip(),
                    routing_skill_id=next(iter(normalize_skill_ids([skill_id])), ""),
                )
                for model_id, skill_id in self._entries.items()
            )
        )
        by_model_id = {}
        for agent in candidates:
            if agent is not None:
                by_model_id[agent.model_id] = agent
        return RegistrySnapshot(tuple(by_model_id.values()))

    async def _load_agent(
        self,
        *,
        request,
        user,
        model_id: str,
        routing_skill_id: str,
    ) -> AgentSpec | None:
        if not model_id or not routing_skill_id:
            return None
        if not await self._access_policy.can_access(
            request=request,
            user=user,
            model_id=model_id,
        ):
            return None

        skill = await Skills.get_skill_by_id(routing_skill_id)
        if skill is None or not skill.is_active:
            self._debug("routing skill unavailable: %s", routing_skill_id)
            return None
        return AgentSpec(
            model_id=model_id,
            routing_skill_id=routing_skill_id,
            name=skill.name or routing_skill_id,
        )


class RegistryPreparation:
    def __init__(self, entries: dict[str, str], debug):
        self._debug = debug
        self._skill_validator = SkillAvailabilityValidator()
        self._access_policy = ModelAccessPolicy(debug)
        self._catalog = SubagentCatalog(entries, self._access_policy, debug)

    async def _can_access_model(self, *, request, user, model_id: str) -> bool:
        return await self._access_policy.can_access(
            request=request,
            user=user,
            model_id=model_id,
        )

    async def _validate_skill_ids(
        self,
        skill_ids: list[str],
        *,
        strict: bool = True,
    ) -> list[str]:
        return await self._skill_validator.validate(skill_ids, strict=strict)

    async def _build_registry(self, *, request, user) -> dict[str, dict]:
        return RegistryMap(await self._catalog.build(request=request, user=user))

    async def prepare(
        self,
        body: dict,
        __user__: dict | None = None,
        __request__=None,
        *,
        base_tool_ids: list[str],
        base_skill_ids: list[str],
    ) -> dict:
        if __request__ is None:
            raise ValueError("Lite Subagent Registry requires __request__")
        # Observe the request before lookups can fail; initialization follows validation.
        if isinstance(body.get("metadata"), dict):
            RequestRuntime(__request__, body["metadata"]).bind_context_request()
        user_id = (__user__ or {}).get("id")
        if not user_id:
            raise ValueError("Missing user id")
        user = await Users.get_user_by_id(user_id)
        if user is None:
            raise ValueError("User not found")

        registry = await self._build_registry(request=__request__, user=user)
        base_skill_ids = normalize_skill_ids(base_skill_ids)
        if isinstance(registry, RegistryMap):
            await self._validate_skill_ids(base_skill_ids, strict=True)
            routing_skill_ids = registry.skill_ids
        else:
            # Preserve test/custom subclass compatibility without reloading in production.
            routing_skill_ids = normalize_skill_ids(registry)
            await self._validate_skill_ids(
                [*base_skill_ids, *registry],
                strict=True,
            )
        orchestrator_skill_ids = normalize_skill_ids([*base_skill_ids, *routing_skill_ids])

        router_model_id = str(body.get("model") or "").strip()
        router_model = await Models.get_model_by_id(router_model_id)
        if router_model is None or not router_model.is_active:
            raise ValueError("Router Model is unavailable")
        router_owner_id = str(router_model.user_id or "").strip()
        if not router_owner_id:
            raise ValueError("Router Model owner is unavailable")

        metadata = body.get("metadata")
        if metadata is None:
            metadata = {}
            body["metadata"] = metadata
        if not isinstance(metadata, dict):
            raise TypeError("Lite Subagent Registry metadata must be an object")
        RequestRuntime(__request__, metadata).start_request(
            body,
            lite_agents=dict(registry),
            lite_router_model_id=router_model_id,
            lite_router_owner_id=router_owner_id,
            lite_base_tool_ids=normalize_ids(base_tool_ids),
            lite_orchestrator_skill_ids=orchestrator_skill_ids,
            lite_registry_applied=True,
        )
        self._debug("registry=%s", json.dumps(dict(registry), ensure_ascii=False))
        self._debug("model-bound base tools=%s", metadata["lite_base_tool_ids"])
        self._debug("model-bound skill count=%s", len(orchestrator_skill_ids))
        return body
