# hermes-plugins-graphiti-neo4j

Long-term memory provider for [Hermes AI Agent](https://github.com/NousResearch/hermes-agent) powered by [Graphiti](https://github.com/getzep/graphiti) + Neo4j.

> **¿Qué es esto?** Un plugin que le da a Hermes **memoria persistente de largo plazo**. Todo lo conversado se guarda en una base de datos de grafo (Neo4j), donde cada entidad (personas, empresas, proyectos, conceptos) y sus relaciones se extraen y conectan automáticamente. Cuando vuelves a hablar con tu agente, recuerda lo que hablaste hace días, semanas o meses.

---

## ¿Por qué Graphiti en lugar de memoria tradicional?

| Memoria built-in de Hermes | Graphiti + Neo4j |
|----------------------------|------------------|
| Por sesión (se pierde al cerrar) | **Persistente entre sesiones** |
| Lista de notas planas | **Grafo de entidades y relaciones** |
| Búsqueda por keywords | **Búsqueda semántica + grafo** |
| Un solo contexto | **Scope por agente (`group_id`)** |
| No conecta datos entre sí | **Conexiones automáticas entre hechos** |

**Ejemplo real:**
- Turno 1: "Trabajo en VERLA SAS" → Graphiti crea nodo `Jhalmar`, nodo `VERLA SAS`, relación `works_at`
- Turno 15: "VERLA SAS usa n8n y Proxmox" → Graphiti enlaza empresa con herramientas
- Turno 200: "¿Qué herramientas usamos en mi empresa?" → El agente navega el grafo y responde, aunque haya pasado semanas

---

## Requisitos

- Hermes Agent >= 0.14
- Neo4j 5.x (Community o Enterprise)
- Python >= 3.11
- **graphiti-core** (dependencia de Python requerida)

**IMPORTANTE:** Después de clonar el plugin, debes instalar las dependencias en el entorno de Hermes:

```bash
# En el entorno virtual de Hermes
pip install -r requirements.txt
```

Si no usas requirements.txt, instala manualmente:
```bash
pip install graphiti-core
```

---

## Instalación

### 1. Clonar el plugin

```bash
cd ~/.hermes/plugins  # o $HERMES_HOME/plugins
git clone https://github.com/Jhalmarm/hermes-plugins-graphiti-neo4j.git graphiti-neo4j
```

### 2. Instalar dependencias

```bash
pip install graphiti-core
```

**Si usas el contenedor Docker de Hermes:**
```bash
docker exec hermes-jhalmar /usr/local/bin/uv pip install graphiti-core
```

### 3. Iniciar Neo4j

```bash
# Con Docker
docker run -d \
  --name neo4j-graphiti \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/hermes-graphiti-local-2026 \
  neo4j:5-community
```

O usa el `docker-compose.yml` incluido:
```bash
docker compose up -d neo4j
```

### 4. Configurar en el archivo de configuración de Hermes

Edita `~/.hermes/config.yaml` y configura el provider y los parámetros del plugin:

```yaml
memory:
  provider: graphiti
  neo4j_uri: "bolt://10.101.10.18:7687"
  neo4j_user: "neo4j"
  neo4j_password: "hermes-graphiti-local-2026"
  group_id: "hermes_test"
  llm_api_key: "${OPENROUTER_API_KEY}"
  llm_base_url: "https://openrouter.ai/api/v1"
  llm_model: "deepseek/deepseek-v4-flash"
  embed_api_key: "${OPENAI_API_KEY}"
  embed_base_url: "https://api.openai.com/v1"
  embed_model: "text-embedding-3-small"
```

**Nota:** También puedes usar variables de entorno como respaldo (ej: `${OPENROUTER_API_KEY}`), pero la configuración prioritaria es desde el archivo `config.yaml`.

### 5. Reiniciar Hermes

```bash
docker compose restart gateway dashboard
# o si está local:
hermes gateway run
```

---

## Parámetros de configuración

| Parámetro | Descripción | Default |
|-----------|-------------|---------|
| `neo4j_uri` | URI de conexión a Neo4j | `bolt://localhost:7687` |
| `neo4j_user` | Usuario de Neo4j | `neo4j` |
| `neo4j_password` | **Requerido** — Contraseña de Neo4j | — |
| `group_id` | Identificador del grafo (para aislar datos) | `default` |
| `llm_api_key` | API key para LLM (OpenRouter, OpenAI, etc.) | `$OPENROUTER_API_KEY` |
| `llm_base_url` | Base URL del LLM | `https://openrouter.ai/api/v1` |
| `llm_model` | Modelo para extracción de entidades | `deepseek/deepseek-v4-flash` |
| `embed_api_key` | API key para embeddings | `$OPENAI_API_KEY` |
| `embed_base_url` | Base URL del servicio de embeddings | `https://api.openai.com/v1` |
| `embed_model` | Modelo de embeddings | `text-embedding-3-small` |

---

## Variables de entorno (respaldos)

Aunque la configuración principal se hace en `config.yaml`, el plugin lee estas variables de entorno como respaldo:

| Variable | Descripción |
|----------|-------------|
| `GRAPHITI_NEO4J_URI` | URI de Neo4j |
| `GRAPHITI_NEO4J_USER` | Usuario de Neo4j |
| `GRAPHITI_NEO4J_PASSWORD` | Contraseña de Neo4j |
| `GRAPHITI_GROUP_ID` | Group ID para el grafo |
| `GRAPHITI_LLM_API_KEY` | API key para LLM |
| `GRAPHITI_LLM_BASE_URL` | Base URL del LLM |
| `GRAPHITI_LLM_MODEL` | Modelo LLM |
| `GRAPHITI_EMBED_API_KEY` | API key para embeddings |
| `GRAPHITI_EMBED_BASE_URL` | Base URL de embeddings |
| `GRAPHITI_EMBED_MODEL` | Modelo de embeddings |

---

## Tools expuestas al modelo

Hermes puede llamar estas 5 tools para interactuar con el grafo:

| Tool | ¿Qué hace? |
|------|-----------|
| `graphiti_search` | Busca hechos relevantes en el grafo (semántica + grafo) |
| `graphiti_add_fact` | Inserta un hecho explícito en el grafo |
| `graphiti_relate` | Crea una relación manual entre dos entidades |
| `graphiti_recall` | Recupera episodios (turnos de conversación) pasados |
| `graphiti_profile` | Muestra resumen del grafo del usuario actual |

---

## ¿Cómo funciona la memoria temporal?

Graphiti maneja **líneas de tiempo** en cada relación (`valid_at`, `expired_at`). Esto permite:

```
Turno 1:  "Trabajo en VERLA SAS"
          → works_at(Jhalmar, VERLA SAS) valid_at=2026-05-22

Turno 50: "Dejé VERLA, ahora estoy en TreeNet"
          → works_at(Jhalmar, VERLA SAS) expired_at=2026-05-22
          → works_at(Jhalmar, TreeNet) valid_at=2026-05-22
```

El grafo **no borra** el hecho anterior, solo lo marca como expirado. Así el agente puede responder "¿Dónde trabajabas antes?" vs "¿Dónde trabajas ahora?".

---

## Scope por agente (`group_id`)

Cada perfil/agente de Hermes usa su propio `group_id`, así que **múltiples agentes comparten la misma base Neo4j sin colisionar**:

| Agente | `group_id` | Grafo |
|--------|-----------|-------|
| `donna` (técnica-ejecutiva) | `donna` | Aislado |
| `coder` (programador) | `coder` | Aislado |
| `hermes_test` (pruebas) | `hermes_test` | Aislado |
| Subagente cron | *skip* | No escribe (protección cron) |

Configura el `group_id` en `config.yaml`:
```yaml
memory:
  provider: graphiti
  group_id: "hermes_test"
```

---

## Estructura del plugin

```
graphiti-neo4j/
├── plugin.yaml      # Manifest del plugin
└── __init__.py      # GraphitiMemoryProvider
```

Solo 2 archivos. Sin dependencias externas más allá de `graphiti-core` y `neo4j`.

---

## Troubleshooting

**Neo4j no responde:**
```bash
docker logs neo4j-graphiti
# Verificar que el puerto 7687 esté abierto
nc -zv 10.101.10.18 7687
```

**Error "api_key client option must be set":**
- Faltan parámetros en `config.yaml`
- Verifica que `neo4j_api_key`, `llm_api_key` y `embed_api_key` estén configurados

**El plugin no aparece en `hermes plugins list`:**
- Verificar que esté en `$HERMES_HOME/plugins/graphiti-neo4j/`
- Verificar que `__init__.py` tenga la función `register(ctx)`

**Error de conexión a Neo4j:**
- Verifica la URI en `config.yaml` (ej: `bolt://10.101.10.18:7687`)
- Asegúrate de que el puerto 7687 esté abierto en el servidor
- Si usas TLS/Aura, usa `neo4j+s://` en lugar de `bolt://`

---

## Licencia

MIT
