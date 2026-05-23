"""Graphiti memory plugin — Temporal knowledge-graph memory for Hermes via Neo4j.

Stores every conversation turn as an *episode*, then lets Graphiti extract
entities and edges automatically.  Each agent/profile gets its own
``group_id`` so multiple Hermes profiles do not collide in the same graph.

Requires:
  - graphiti-core        (pip install graphiti-core)
  - neo4j running        (bolt://... or neo4j://...)
  - LLM endpoint         (OpenRouter / OpenAI / local) for extraction
  - Embedding endpoint   (OpenAI by default) for vector search

Env vars (all optional, fallbacks shown):
  GRAPHITI_NEO4J_URI          default: bolt://localhost:7687
  GRAPHITI_NEO4J_API_KEY      required — no default
  GRAPHITI_LLM_API_KEY        default: $OPENROUTER_API_KEY
  GRAPHITI_LLM_BASE_URL       default: https://openrouter.ai/api/v1
  GRAPHITI_LLM_MODEL          default: deepseek/deepseek-v4-flash
  GRAPHITI_EMBED_API_KEY      default: $OPENAI_API_KEY || $GRAPHITI_LLM_API_KEY
  GRAPHITI_EMBED_BASE_URL     default: https://api.openai.com/v1
  GRAPHITI_EMBED_MODEL        default: text-embedding-3-small
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import neo4j
from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

# Graphiti types used throughout
from graphiti_core.nodes import EpisodeType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_NEO4J_URI = "bolt://localhost:7687"

_DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_LLM_MODEL = "deepseek/deepseek-v4-flash"

_DEFAULT_EMBED_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_EMBED_MODEL = "text-embedding-3-small"


# ---------------------------------------------------------------------------
# Async bridge — Graphiti is async, Hermes is sync
# ---------------------------------------------------------------------------
class _AsyncRunner:
    """Dedicated thread + event-loop for executing Graphiti coroutines."""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="graphiti-async")
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout: float = 60.0):
        if self._loop is None or self._thread is None:
            raise RuntimeError("AsyncRunner not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)





# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------
_SEARCH_SCHEMA = {
    "name": "graphiti_search",
    "description": (
        "Semantic + graph search over the long-term memory graph. Returns facts, "
        "entities and relationships that match the query."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query to search the memory graph.",
            },
            "num_results": {
                "type": "integer",
                "description": "Max edges/facts to return (default 10).",
            },
        },
        "required": ["query"],
    },
}

_ADD_FACT_SCHEMA = {
    "name": "graphiti_add_fact",
    "description": (
        "Insert an explicit fact into the knowledge graph. The fact will be "
        "embedded and linked to existing entities automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "A concise factual statement to store.",
            },
            "source": {
                "type": "string",
                "description": "Optional source label, e.g. 'user', 'tool_result', 'observation'.",
            },
        },
        "required": ["fact"],
    },
}

_RELATE_SCHEMA = {
    "name": "graphiti_relate",
    "description": (
        "Create or update a directed relationship between two entities in the graph. "
        "Use when you discover a connection that Graphiti did not extract automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_entity": {
                "type": "string",
                "description": "Name of the source entity.",
            },
            "target_entity": {
                "type": "string",
                "description": "Name of the target entity.",
            },
            "relation": {
                "type": "string",
                "description": "Type of relationship, e.g. 'works_at', 'owns', 'friend_of'.",
            },
            "fact": {
                "type": "string",
                "description": "Human-readable sentence describing the relationship.",
            },
        },
        "required": ["source_entity", "target_entity", "relation", "fact"],
    },
}

_RECALL_SCHEMA = {
    "name": "graphiti_recall",
    "description": (
        "Retrieve past episodes (conversation turns) that are semantically "
        "relevant to the query. Good for 'what did we discuss about X last week?'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Topic or question to find past episodes about.",
            },
            "num_results": {
                "type": "integer",
                "description": "Max episodes to return (default 5).",
            },
        },
        "required": ["query"],
    },
}

_PROFILE_SCHEMA = {
    "name": "graphiti_profile",
    "description": (
        "Get a summary of the user's knowledge graph: key entities, top relationships, "
        "and recent topics. No parameters — returns the profile for the current group_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_ALL_TOOLS = [_SEARCH_SCHEMA, _ADD_FACT_SCHEMA, _RELATE_SCHEMA, _RECALL_SCHEMA, _PROFILE_SCHEMA]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class GraphitiMemoryProvider(MemoryProvider):
    """Graphiti-backed temporal knowledge graph memory."""

    def __init__(self):
        self._graphiti = None  # type: ignore
        self._runner = _AsyncRunner()
        self._group_id = "default"
        self._session_id = ""
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._sync_thread: Optional[threading.Thread] = None
        self._config: Dict[str, str] = {}
        self._initialized = False
        self._cron_skipped = False

    # --- MemoryProvider ABC -------------------------------------------------

    @property
    def name(self) -> str:
        return "graphiti"

    def is_available(self) -> bool:
        """Return True when graphiti-core is importable and Neo4j API key is present."""
        try:
            import graphiti_core  # noqa: F401
        except Exception:
            logger.debug("graphiti-core is not installed")
            return False

        api_key = os.environ.get("GRAPHITI_NEO4J_API_KEY", "")
        if not api_key:
            logger.debug("GRAPHITI_NEO4J_API_KEY not set — graphiti unavailable")
            return False
        return True

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "neo4j_uri",
                "description": "Neo4j bolt URI (e.g. bolt://localhost:7687)",
                "default": _DEFAULT_NEO4J_URI,
                "env_var": "GRAPHITI_NEO4J_URI",
            },
            {
                "key": "neo4j_api_key",
                "description": "Neo4j API key",
                "secret": True,
                "required": True,
                "env_var": "GRAPHITI_NEO4J_API_KEY",
            },
            {
                "key": "group_id",
                "description": "Group ID to scope the graph (default: auto-generated)",
                "default": "default",
                "env_var": "GRAPHITI_GROUP_ID",
            },
            {
                "key": "llm_api_key",
                "description": "API key for the LLM endpoint (OpenRouter / OpenAI)",
                "secret": True,
                "env_var": "GRAPHITI_LLM_API_KEY",
            },
            {
                "key": "llm_base_url",
                "description": "Base URL for LLM endpoint",
                "default": _DEFAULT_LLM_BASE_URL,
                "env_var": "GRAPHITI_LLM_BASE_URL",
            },
            {
                "key": "llm_model",
                "description": "Model name for LLM extraction",
                "default": _DEFAULT_LLM_MODEL,
                "env_var": "GRAPHITI_LLM_MODEL",
            },
            {
                "key": "embed_api_key",
                "description": "API key for embeddings (OpenAI, etc.)",
                "secret": True,
                "env_var": "GRAPHITI_EMBED_API_KEY",
            },
            {
                "key": "embed_base_url",
                "description": "Base URL for embedding endpoint",
                "default": _DEFAULT_EMBED_BASE_URL,
                "env_var": "GRAPHITI_EMBED_BASE_URL",
            },
            {
                "key": "embed_model",
                "description": "Embedding model name",
                "default": _DEFAULT_EMBED_MODEL,
                "env_var": "GRAPHITI_EMBED_MODEL",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        """Spin up the async runner and connect to Neo4j + Graphiti."""
        agent_context = kwargs.get("agent_context", "")
        if agent_context in ("cron", "flush"):
            logger.debug("Graphiti skipped: cron/flush context")
            self._cron_skipped = True
            return

        # Resolve config from kwargs (Hermes config) or fallback to env vars
        neo4j_uri = kwargs.get("neo4j_uri") or os.environ.get("GRAPHITI_NEO4J_URI", _DEFAULT_NEO4J_URI)
        neo4j_api_key = kwargs.get("neo4j_api_key") or os.environ.get("GRAPHITI_NEO4J_API_KEY", "")
        group_id = kwargs.get("group_id") or os.environ.get("GRAPHITI_GROUP_ID", "")

        llm_api_key = kwargs.get("llm_api_key") or os.environ.get("GRAPHITI_LLM_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))
        llm_base_url = kwargs.get("llm_base_url") or os.environ.get("GRAPHITI_LLM_BASE_URL", _DEFAULT_LLM_BASE_URL)
        llm_model = kwargs.get("llm_model") or os.environ.get("GRAPHITI_LLM_MODEL", _DEFAULT_LLM_MODEL)

        embed_api_key = kwargs.get("embed_api_key") or os.environ.get(
            "GRAPHITI_EMBED_API_KEY",
            os.environ.get("OPENAI_API_KEY", llm_api_key),
        )
        embed_base_url = kwargs.get("embed_base_url") or os.environ.get("GRAPHITI_EMBED_BASE_URL", _DEFAULT_EMBED_BASE_URL)
        embed_model = kwargs.get("embed_model") or os.environ.get("GRAPHITI_EMBED_MODEL", _DEFAULT_EMBED_MODEL)

        if not neo4j_api_key:
            logger.warning("Graphiti: NEO4J_API_KEY not set — cannot initialize")
            return

        # group_id scoping — one graph per agent profile or user
        self._group_id = group_id or kwargs.get("agent_identity") or kwargs.get("user_id") or session_id or "default"
        self._session_id = session_id
        logger.info("Graphiti: using group_id=%s", self._group_id)

        # Build LLM + embedder + cross-encoder clients (native Graphiti clients)
        from graphiti_core.llm_client import OpenAIClient
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.embedder import OpenAIEmbedder
        from graphiti_core.embedder.openai import OpenAIEmbedderConfig
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

        llm_cfg = LLMConfig(
            api_key=llm_api_key,
            base_url=llm_base_url,
            model=llm_model,
            temperature=0.1,
        )
        llm_client = OpenAIClient(config=llm_cfg)

        embed_cfg = OpenAIEmbedderConfig(
            api_key=embed_api_key,
            base_url=embed_base_url,
            embedding_model=embed_model,
        )
        embedder = OpenAIEmbedder(config=embed_cfg)

        cross_encoder = OpenAIRerankerClient(config=llm_cfg)

        # Start async runner (for async Graphiti methods)
        self._runner.start()

        # Initialize Graphiti (synchronous constructor)
        try:
            from graphiti_core import Graphiti
            self._graphiti = Graphiti(
                uri=neo4j_uri,
                user="neo4j",
                password=neo4j_api_key,
                llm_client=llm_client,
                embedder=embedder,
                cross_encoder=cross_encoder,
            )
            logger.info("Graphiti connected to %s", neo4j_uri)
        except Exception as e:
            logger.error("Graphiti connection failed: %s", e)
            self._graphiti = None
            return

        # Build indices / constraints (async — run via runner)
        try:
            self._runner.run(self._graphiti.build_indices_and_constraints())
            logger.debug("Graphiti indices built")
        except Exception as e:
            logger.error("Graphiti index build failed: %s", e)

        self._initialized = True

    def system_prompt_block(self) -> str:
        if self._cron_skipped or not self._initialized:
            return ""
        return (
            "# Graphiti Long-Term Memory\n"
            "Active in this session. Relevant past context is injected automatically, "
            "and you can use graphiti_search, graphiti_add_fact, graphiti_relate, "
            "graphiti_recall and graphiti_profile to interact with the memory graph."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return relevant graph context before the turn."""
        if self._cron_skipped or not self._initialized or not self._graphiti:
            return ""

        try:
            edges = self._runner.run(
                self._graphiti.search(
                    query=query,
                    group_ids=[self._group_id],
                    num_results=8,
                )
            )
            if not edges:
                return ""
            parts = ["## Recalled Memory Graph Context"]
            for edge in edges:
                parts.append(f"- {edge.name}: {edge.fact}")
            return "\n".join(parts)
        except Exception as e:
            logger.debug("Graphiti prefetch error: %s", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # Background prefetch is handled synchronously in prefetch() for simplicity;
        # a future optimization could queue a coroutine and return cached results.
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Persist the turn as a Graphiti episode (non-blocking thread)."""
        if self._cron_skipped or not self._initialized or not self._graphiti:
            return

        timestamp = datetime.now(timezone.utc)
        body = f"User: {user_content}\nAssistant: {assistant_content}"
        name = f"turn-{timestamp.isoformat()}"

        def _add():
            try:
                self._runner.run(
                    self._graphiti.add_episode(
                        name=name,
                        episode_body=body,
                        source_description="hermes_conversation_turn",
                        reference_time=timestamp,
                        source=EpisodeType.message,
                        group_id=self._group_id,
                    ),
                    timeout=45.0,
                )
                logger.debug("Graphiti episode added: %s", name)
            except Exception as e:
                logger.error("Graphiti add_episode failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(target=_add, daemon=True, name="graphiti-sync")
        self._sync_thread.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)

    def shutdown(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        if self._graphiti is not None:
            try:
                self._runner.run(self._graphiti.close())
            except Exception as e:
                logger.debug("Graphiti close error: %s", e)
        self._runner.stop()

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, **kwargs) -> None:
        if reset:
            self._session_id = new_session_id
        else:
            self._session_id = new_session_id

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if self._cron_skipped:
            return []
        return list(_ALL_TOOLS)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if self._cron_skipped or not self._initialized or not self._graphiti:
            return tool_error("Graphiti is not initialized.")

        try:
            if tool_name == "graphiti_search":
                return self._tool_search(args)
            elif tool_name == "graphiti_add_fact":
                return self._tool_add_fact(args)
            elif tool_name == "graphiti_relate":
                return self._tool_relate(args)
            elif tool_name == "graphiti_recall":
                return self._tool_recall(args)
            elif tool_name == "graphiti_profile":
                return self._tool_profile(args)
            else:
                return tool_error(f"Unknown Graphiti tool: {tool_name}")
        except Exception as e:
            logger.error("Graphiti tool %s failed: %s", tool_name, e)
            return tool_error(f"Graphiti {tool_name} error: {e}")

    # --- Internal tools ------------------------------------------------------

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "").strip()
        if not query:
            return tool_error("Missing parameter: query")
        num = min(int(args.get("num_results", 10)), 20)

        edges = self._runner.run(
            self._graphiti.search(
                query=query,
                group_ids=[self._group_id],
                num_results=num,
            ),
            timeout=30.0,
        )
        if not edges:
            return json.dumps({"result": "No relevant facts found in the graph."})

        results = []
        for edge in edges:
            results.append(
                {
                    "relation": edge.name,
                    "fact": edge.fact,
                    "created_at": edge.created_at.isoformat() if edge.created_at else None,
                }
            )
        return json.dumps({"result": results})

    def _tool_add_fact(self, args: Dict[str, Any]) -> str:
        fact = args.get("fact", "").strip()
        if not fact:
            return tool_error("Missing parameter: fact")
        source = args.get("source", "explicit")

        self._runner.run(
            self._graphiti.add_episode(
                name=f"fact-{time.time()}",
                episode_body=fact,
                source_description=source,
                reference_time=datetime.now(timezone.utc),
                source=EpisodeType.text,
                group_id=self._group_id,
            ),
            timeout=45.0,
        )
        return json.dumps({"result": "Fact added to the knowledge graph."})

    def _tool_relate(self, args: Dict[str, Any]) -> str:
        src = args.get("source_entity", "").strip()
        tgt = args.get("target_entity", "").strip()
        rel = args.get("relation", "").strip()
        fact = args.get("fact", "").strip()
        if not all([src, tgt, rel, fact]):
            return tool_error("Missing one of: source_entity, target_entity, relation, fact")

        # Graphiti's add_triplet is the cleanest way to inject a manual edge
        self._runner.run(
            self._graphiti.add_triplet(
                source_node_name=src,
                edge_name=rel,
                target_node_name=tgt,
                fact=fact,
                group_id=self._group_id,
                created_at=datetime.now(timezone.utc),
            ),
            timeout=30.0,
        )
        return json.dumps({"result": f"Relationship added: {src} --{rel}--> {tgt}"})

    def _tool_recall(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "").strip()
        if not query:
            return tool_error("Missing parameter: query")
        num = min(int(args.get("num_results", 5)), 20)

        episodes = self._runner.run(
            self._graphiti.retrieve_episodes(
                query=query,
                group_ids=[self._group_id],
                num_results=num,
            ),
            timeout=30.0,
        )
        if not episodes:
            return json.dumps({"result": "No relevant past episodes found."})

        results = []
        for ep in episodes:
            results.append(
                {
                    "name": ep.name,
                    "content": ep.content[:500] if hasattr(ep, "content") else str(ep),
                    "time": ep.reference_time.isoformat() if hasattr(ep, "reference_time") and ep.reference_time else None,
                }
            )
        return json.dumps({"result": results})

    def _tool_profile(self, _args: Dict[str, Any]) -> str:
        # Query for everything in the group to build a simple profile summary
        edges = self._runner.run(
            self._graphiti.search(
                query="*",
                group_ids=[self._group_id],
                num_results=50,
            ),
            timeout=30.0,
        )
        if not edges:
            return json.dumps({"result": "The memory graph is empty for this profile."})

        entities = set()
        relationships = []
        for edge in edges:
            entities.add(edge.source_node_uuid)
            entities.add(edge.target_node_uuid)
            relationships.append(edge.fact)

        return json.dumps(
            {
                "group_id": self._group_id,
                "entity_count_estimate": len(entities),
                "top_facts": relationships[:15],
            }
        )

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if action != "add" or target != "user" or not content or not self._initialized:
            return
        # Mirror built-in memory writes into the graph as explicit facts
        try:
            self._runner.run(
                self._graphiti.add_episode(
                    name=f"memory-write-{time.time()}",
                    episode_body=content,
                    source_description="built_in_memory_tool",
                    reference_time=datetime.now(timezone.utc),
                    source=EpisodeType.text,
                    group_id=self._group_id,
                ),
                timeout=20.0,
            )
        except Exception as e:
            logger.debug("Graphiti mirror memory write failed: %s", e)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------
def register(ctx) -> None:
    ctx.register_memory_provider(GraphitiMemoryProvider())
