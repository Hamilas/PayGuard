"""
MLOps Multi-Agent System — Day-7 Complete Rewrite
==================================================
New in Day-7:
  - Redis Job 1: Dedup lock in alert_intake (5-min TTL)
  - Redis Job 2: Resolution cache in triage (24h TTL)
  - ChromaDB: semantic incident memory (RAG)
  - Triage 3-step cascade: Redis → ChromaDB → Phi3/Ollama
  - fast_resolver node: handles all Tier 0/1/2 fast-path exits
  - memory_retriever node: RAG query before investigator_initial
  - Parallel researcher + data_scientist (LangGraph Send API)
  - Two-pass Investigator: initial hypotheses → synthesis
  - memory_writer node: writes to ChromaDB + Redis resolution cache
  - Flask webhook: Prometheus alertmanager → auto-trigger
  - google-generativeai → google-genai migration
"""

import json, time, logging, boto3, requests, uuid, hashlib, os
from datetime import datetime
from typing import TypedDict, Annotated, List, Optional
import operator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
log = logging.getLogger("mlops-agent")

# ── Constants ─────────────────────────────────────────────────────────────────
OLLAMA_HOST       = "10.43.129.9"
CHROMA_HOST       = "10.43.66.211"
REDIS_HOST         = "10.43.74.23"
REDIS_PORT        = 6379
OLLAMA_URL         = f"http://{OLLAMA_HOST}:11434"
CHROMADB_HOST      = "10.43.66.211"
CHROMADB_PORT     = 8000
DEDUP_TTL         = 300    # 5 minutes — dedup lock
RESOLUTION_TTL    = 86400  # 24 hours — resolution cache

# ── SSM key cache ─────────────────────────────────────────────────────────────
_ssm = boto3.client('ssm', region_name='us-east-2')
_key_cache = {}

def get_key(name: str) -> str:
    if name not in _key_cache:
        try:
            _key_cache[name] = _ssm.get_parameter(
                Name=f'/mlops/{name}', WithDecryption=True
            )['Parameter']['Value']
        except Exception:
            _key_cache[name] = ""
    return _key_cache[name]

# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_json(text: str) -> dict:
    """
    Robust JSON extraction — 4-strategy parser.
    Handles: markdown fences, literal newlines inside strings,
    trailing commas, and preamble text before the JSON block.
    """
    import json as _json

    if not text or not text.strip():
        return {"parse_error": True, "raw": ""}

    # Strip markdown fences
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
    if t.endswith("```"):
        t = t.rsplit("```", 1)[0]
    t = t.strip()

    # Strategy 1: direct parse
    try:
        return _json.loads(t)
    except Exception:
        pass

    # Strategy 2: collapse literal newlines inside quoted strings
    # Claude sometimes puts real newlines inside string values (invalid JSON)
    def sanitize(s):
        result = []
        in_str = False
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\\" and i + 1 < len(s):
                result.append(ch)
                result.append(s[i+1])
                i += 2
                continue
            if ch == '"':
                in_str = not in_str
            if in_str and ch in ("\n", "\r", "\t"):
                result.append(" ")
                i += 1
                continue
            result.append(ch)
            i += 1
        return "".join(result)

    try:
        return _json.loads(sanitize(t))
    except Exception:
        pass

    # Strategy 3: find first { ... } block (handles preamble before JSON)
    try:
        start = t.index("{")
        depth = 0
        end = start
        for idx, ch in enumerate(t[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = idx
                    break
        block = t[start:end + 1]
        # Sanitize extracted block
        block2 = sanitize(block)
        return _json.loads(block2)
    except Exception:
        pass

    # Strategy 4: truncation recovery — JSON cut off by max_tokens
    # Attempt to close open braces/brackets and re-parse
    try:
        truncated = t
        # Count unclosed braces and brackets
        depth_curly  = truncated.count('{') - truncated.count('}')
        depth_square = truncated.count('[') - truncated.count(']')
        # Close any open string first (odd number of unescaped quotes)
        in_str = False
        for ch in truncated:
            if ch == '"': in_str = not in_str
        if in_str:
            truncated += '"'  # close open string
        # Close open arrays then objects
        truncated += ']' * depth_square + '}' * depth_curly
        result = _json.loads(truncated)
        result['_truncated'] = True  # flag so caller knows
        return result
    except Exception:
        pass

    # Strategy 4: last resort
    return {"parse_error": True, "raw": text[:500]}


def alert_fingerprint(alert_name: str, severity: str) -> str:
    """Stable fingerprint for dedup + resolution cache keys."""
    return hashlib.md5(f"{alert_name}:{severity}".encode()).hexdigest()[:16]

# ── Redis client ──────────────────────────────────────────────────────────────
_redis_client = None

def get_redis():
    global _redis_client
    if _redis_client is None:
        import redis
        try:
            _redis_client = redis.Redis(
                host=REDIS_HOST,
                decode_responses=True, socket_timeout=2
            )
            _redis_client.ping()
            log.info("Redis connected")
        except Exception as e:
            log.warning(f"Redis unavailable: {e} — running without cache")
            _redis_client = None
    return _redis_client

# ── ChromaDB client ───────────────────────────────────────────────────────────
_chroma_collection = None

def get_chroma_collection():
    """Returns ChromaDB HTTP client collection for incident memory."""
    global _chroma_collection
    if _chroma_collection is None:
        try:
            import chromadb
            client = chromadb.HttpClient(
                host=CHROMADB_HOST,
                port=CHROMADB_PORT,
            )
            _chroma_collection = client.get_or_create_collection(
                name="mlops_incidents",
                metadata={"hnsw:space": "cosine"}
            )
            log.info(f"ChromaDB connected "
                     f"({_chroma_collection.count()} incidents stored)")
        except Exception as e:
            log.warning(f"ChromaDB unavailable: {e} — running without RAG")
            _chroma_collection = None
    return _chroma_collection

# ── Embeddings ────────────────────────────────────────────────────────────────
_embedder = None

def get_embedder():
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer('all-MiniLM-L6-v2', cache_folder='/home/ec2-user/.cache/sentence_transformers')
            log.info("Embedder loaded")
        except Exception as e:
            log.warning(f"Embedder unavailable: {e}")
    return _embedder

def embed_text(text: str) -> list:
    embedder = get_embedder()
    if embedder is None:
        return []
    return embedder.encode(text).tolist()

# ── Platform collectors ───────────────────────────────────────────────────────
def _prom_url() -> str:
    import subprocess
    r = subprocess.run(
        ["kubectl","get","svc","-n","monitoring",
         "kube-prometheus-stack-prometheus",
         "-o","jsonpath={.spec.clusterIP}"],
        capture_output=True, text=True
    )
    ip = r.stdout.strip()
    return f"http://{ip}:9090" if ip else "http://localhost:9090"

def query_prom(q: str) -> str:
    try:
        r = requests.get(
            f"{_prom_url()}/api/v1/query",
            params={"query": q}, timeout=5
        )
        results = r.json().get("data", {}).get("result", [])
        return results[0]["value"][1] if results else "no_data"
    except Exception:
        return "error"

def collect_context() -> dict:
    log.info("Collecting platform metrics...")
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "serving": {
            "request_rate_rps": query_prom(
                'sum(rate(fraud_api_requests_total{path="/predict"}[5m]))'),
            "p99_latency_ms":   query_prom(
                'histogram_quantile(0.99,sum(rate(fraud_api_request_duration_seconds_bucket'
                '{path="/predict"}[5m]))by(le))*1000'),
            "fraud_rate":       query_prom(
                'sum(rate(fraud_predictions_total{result="fraud"}[5m]))'
                '/sum(rate(fraud_predictions_total[5m]))'),
            "error_rate":       query_prom(
                '1-(sum(rate(fraud_api_requests_total{path="/predict",status="200"}[5m]))'
                '/sum(rate(fraud_api_requests_total{path="/predict"}[5m])))'),
        },
        "drift": {
            "drift_score":    query_prom("evidently_dataset_drift_score"),
            "drift_detected": query_prom("evidently_drift_detected"),
        },
        "mlflow": _mlflow_registry(),
        "active_alerts": _active_alerts(),
    }

def _mlflow_registry() -> dict:
    try:
        r = requests.get(
            "http://localhost:32001/api/2.0/mlflow/registered-models/list",
            timeout=5
        )
        return {
            m["name"]: m.get("latest_versions",[{}])[-1].get("current_stage","?")
            for m in r.json().get("registered_models",[])
        }
    except Exception:
        return {}

def _active_alerts() -> list:
    try:
        r = requests.get(f"{_prom_url()}/api/v1/alerts", timeout=5)
        return [
            {"name": a["labels"].get("alertname",""),
             "severity": a["labels"].get("severity",""),
             "state": a["state"]}
            for a in r.json().get("data",{}).get("alerts",[])
            if a["state"] == "firing"
        ]
    except Exception:
        return []

# ── State ─────────────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    # Incident metadata
    incident_id:          str
    alert_name:           str
    alert_severity:       str
    fingerprint:          str

    # Platform data
    context:              dict

    # Memory / RAG
    past_incidents:       list        # ChromaDB hits from memory_retriever
    memory_hit:           bool        # True if fast-path from Redis/ChromaDB

    # Triage
    triage_decision:      str
    triage_confidence:    float
    triage_path:          str         # REDIS_HIT | CHROMADB_HIT | PHI3_SIMPLE | PHI3_COMPLEX

    # Agent outputs
    investigator_hypothesis:  dict    # First pass — before specialists
    researcher_report:        dict
    ds_report:                dict
    investigator_report:      dict    # Second pass — synthesis of all findings

    # Remediation
    operator_plan:        dict
    guardian_decision:    str
    actions_executed:     Annotated[List[str], operator.add]

    # Resolution
    incident_resolved:    bool
    verify_result:           dict
    post_fix_monitor_result: dict
    triage_retrospective:     dict
    requires_human:       bool
    resolution_summary:   str

    # Cost tracking
    total_cost:           Annotated[float, operator.add]
    agent_costs:          Annotated[dict, lambda a, b: {**a, **b}]
    summary:              str


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 0 — alert_intake (Redis dedup lock — Job 1)
# ═══════════════════════════════════════════════════════════════════════════════
def node_alert_intake(state: AgentState) -> AgentState:
    """
    Redis Job 1: Dedup lock.
    Checks if this exact alert is already being investigated.
    Sets a 5-minute lock so duplicate Prometheus firings are dropped.
    """
    log.info(" [ALERT_INTAKE] Checking dedup lock...")
    fp = alert_fingerprint(state["alert_name"], state["alert_severity"])
    r = get_redis()

    if r is not None:
        dedup_key = f"dedup:{fp}"
        if r.exists(dedup_key):
            ttl = r.ttl(dedup_key)
            log.info(f"DUPLICATE — already investigating "
                     f"{state['alert_name']} (TTL: {ttl}s remaining). Skipping.")
            # Mark as duplicate — graph will exit early
            return {**state, "fingerprint": fp,
                    "triage_decision": "DUPLICATE",
                    "incident_resolved": True}
        else:
            r.setex(dedup_key, DEDUP_TTL, "investigating")
            log.info(f"New incident — dedup lock set ({DEDUP_TTL}s TTL)")
    else:
        log.warning("Redis unavailable — skipping dedup check")

    log.info(f"Alert: {state['alert_name']} [{state['alert_severity']}]")
    return {**state, "fingerprint": fp}


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 1 — context_collector
# ═══════════════════════════════════════════════════════════════════════════════
def node_context_collector(state: AgentState) -> AgentState:
    log.info(" [CONTEXT_COLLECTOR] Pulling platform metrics...")
    ctx = collect_context()
    drift = ctx["drift"].get("drift_score","no_data")
    rps   = ctx["serving"].get("request_rate_rps","no_data")
    log.info(f"Drift score: {drift} | RPS: {rps}")
    return {**state, "context": ctx}


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 2 — memory_retriever (ChromaDB RAG — before investigator)
# ═══════════════════════════════════════════════════════════════════════════════
def node_memory_retriever(state: AgentState) -> AgentState:
    """
    RAG pattern: query ChromaDB for semantically similar past incidents.
    Results are injected into investigator_initial's prompt as context.
    """
    log.info(" [MEMORY_RETRIEVER] Querying incident memory (RAG)...")
    collection = get_chroma_collection()
    past = []

    if collection is not None and collection.count() > 0:
        query_text = (
            f"Alert: {state['alert_name']} "
            f"Severity: {state['alert_severity']} "
            f"Drift: {state['context']['drift'].get('drift_score','?')} "
            f"Alerts: {[a['name'] for a in state['context'].get('active_alerts',[])]}"
        )
        embedding = embed_text(query_text)
        if embedding:
            try:
                results = collection.query(
                    query_embeddings=[embedding],
                    n_results=min(3, collection.count()),
                    include=["documents","metadatas","distances"]
                )
                for i, (doc, meta, dist) in enumerate(zip(
                    results["documents"][0],
                    results["metadatas"][0],
                    results["distances"][0]
                )):
                    similarity = 1 - dist  # cosine distance → similarity
                    if similarity > 0.50:  # Only include relevant past incidents
                        past.append({
                            "similarity": round(similarity, 3),
                            "alert_name": meta.get("alert_name","?"),
                            "root_cause": meta.get("root_cause","?"),
                            "resolution": meta.get("resolution","?"),
                            "timestamp": meta.get("timestamp","?"),
                        })
                        log.info(
                            f"   Past incident #{i+1}: "
                            f"{meta.get('alert_name','?')} "
                            f"(similarity: {similarity:.2f})"
                        )
            except Exception as e:
                log.warning(f"ChromaDB query error: {e}")
        else:
            log.info("Embedder unavailable — skipping RAG")
    else:
        log.info("No past incidents in memory yet (first run)")

    log.info(f"Found {len(past)} relevant past incidents")
    return {**state, "past_incidents": past}


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 3 — triage (3-step cascade: Redis → ChromaDB → Phi3)
# ═══════════════════════════════════════════════════════════════════════════════
def node_triage(state: AgentState) -> AgentState:
    """
    3-step cost cascade:
      Step 1: Redis resolution cache   ($0, sub-ms)
      Step 2: ChromaDB semantic match  ($0, local)
      Step 3: Phi3/Ollama classify     ($0, local inference)
    """
    log.info(" [TRIAGE] Running 3-step cost cascade...")
    fp = state["fingerprint"]

    # ── Step 1: Redis resolution cache (Job 2) ──────────────────────────────
    r = get_redis()
    if r is not None:
        try:
            res_key = f"resolution:{fp}"
            cached  = r.get(res_key)
            if cached:
                log.info("TIER 1 — Redis HIT: replaying cached resolution")
                cached_resolution = parse_json(cached)
                if not cached_resolution.get("parse_error"):
                    return {
                        **state,
                        "triage_decision":    "CACHED_RESOLUTION",
                        "triage_confidence":  0.99,
                        "triage_path":        "REDIS_HIT",
                        "memory_hit":         True,
                        "investigator_report": cached_resolution,
                        "resolution_summary":  cached_resolution.get(
                            "root_cause", "Cached resolution replayed"
                        ),
                    }
                else:
                    log.warning("Redis HIT but parse failed — continuing to Tier 2")
            else:
                log.info(f"TIER 1 — Redis MISS (key: {res_key}) — proceeding to Tier 2")
        except Exception as e:
            log.warning(f"Redis Tier 1 error: {e} — continuing to Tier 2")

    # ── Step 2: ChromaDB semantic search ────────────────────────────────────
    if state.get("past_incidents"):
        best = state["past_incidents"][0]  # Already sorted by similarity
        sim  = best.get("similarity", 0)
        log.info(f"TIER 2 — ChromaDB best match: {sim:.2f} similarity")

        if sim > 0.92:
            log.info("TIER 2 — ChromaDB HIT (>0.92): replaying resolution")
            return {
                **state,
                "triage_decision":    f"CHROMADB_MATCH:{best['alert_name']}",
                "triage_confidence":  sim,
                "triage_path":        "CHROMADB_HIT",
                "memory_hit":         True,
                "resolution_summary": best.get("resolution","ChromaDB resolution"),
            }
        elif sim > 0.75:
            log.info(f"TIER 2 — Partial match ({sim:.2f}) — routing to full pipeline with context")
            # Will still go to full pipeline but past_incidents already loaded
    else:
        log.info("TIER 2 — No ChromaDB matches (first investigation or low similarity)")

    # ── Step 3: Phi3/Ollama classification ──────────────────────────────────
    log.info("TIER 3 — Phi3/Ollama local classification...")
    name        = state["alert_name"]
    severity    = state["alert_severity"]
    raw_drift = state["context"]["drift"].get("drift_score", "0")
    try:
        drift_score = float(raw_drift) if raw_drift not in ("no_data","error","") else 0.0
    except (ValueError, TypeError):
        drift_score = 0.0
    rps         = state["context"]["serving"].get("request_rate_rps", "0")

    phi3_decision = _phi3_classify(name, severity, drift_score, rps)

    log.info(f"Phi3 decision: {phi3_decision['category']} "
             f"({phi3_decision['confidence']:.0%}) — {phi3_decision['route']}")
    # ── GATE 1: Triage confidence checkpoint ──────────────────────────────
    _phi3_conf = phi3_decision.get("confidence", 0)
    if isinstance(_phi3_conf, str):
        try: _phi3_conf = float(_phi3_conf.strip("%")) / 100
        except: _phi3_conf = 0.0
    _gate1_pass = float(_phi3_conf) >= 0.70
    _g1_label = " PASS" if _gate1_pass else "  MARGINAL"
    log.info(f"GATE 1 (Triage): {float(_phi3_conf):.0%} {_g1_label} (threshold: 70%)")

    return {
        **state,
        "triage_decision":   phi3_decision["category"],
        "triage_confidence": phi3_decision["confidence"],
        "triage_path":       phi3_decision["route"],
        "memory_hit":        False,
    }


def _phi3_classify(name: str, severity: str,
                   drift_score: float, rps: str) -> dict:
    """Call Phi3 via Ollama for zero-cost local classification."""
    prompt = f"""You are an MLOps alert classifier. Classify this alert.

Alert: {name}
Severity: {severity}
Drift Score: {drift_score}
Request Rate: {rps} rps

Classify as one of:
- SELF_HEALING: transient, will resolve automatically (pod restart, brief spike)
- SIMPLE_KNOWN: known pattern, standard fix applies
- DATA_DRIFT: model drift detected, retraining likely needed
- LATENCY_SLO: serving latency breach
- CRITICAL_INFRA: infrastructure failure requiring immediate action
- FRAUD_ANOMALY: unusual fraud rate pattern
- UNKNOWN: cannot classify confidently

Respond with JSON only:
{{"category":"DATA_DRIFT","confidence":0.88,"route":"PHI3_COMPLEX","reasoning":"one sentence"}}"""

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": "phi3:mini", "prompt": prompt,
                  "stream": False, "format": "json"},
            timeout=5
        )
        if resp.status_code == 200:
            result = resp.json()
            parsed = json.loads(result.get("response", "{}"))
            category   = parsed.get("category", "UNKNOWN")
            confidence = float(parsed.get("confidence", 0.5))
            # Route decision: simple/self-healing → fast_resolver
            # complex/unknown → full pipeline
            if category in ("SELF_HEALING",) or confidence < 0.60:
                route = "PHI3_SIMPLE"
            else:
                route = "PHI3_COMPLEX"
            return {"category": category, "confidence": confidence,
                    "route": route,
                    "reasoning": parsed.get("reasoning","")}
    except Exception as e:
        log.warning(f"Phi3 unavailable ({e}) — using rule-based fallback")

    # Rule-based fallback if Ollama is down
    if "Down" in name:         return {"category":"CRITICAL_INFRA","confidence":0.95,"route":"PHI3_COMPLEX","reasoning":"infra down"}
    elif "Drift" in name or drift_score > 0.7: return {"category":"DATA_DRIFT","confidence":0.88,"route":"PHI3_COMPLEX","reasoning":"drift detected"}
    elif "Latency" in name:    return {"category":"LATENCY_SLO","confidence":0.82,"route":"PHI3_COMPLEX","reasoning":"latency breach"}
    elif "FraudRate" in name:  return {"category":"FRAUD_ANOMALY","confidence":0.79,"route":"PHI3_COMPLEX","reasoning":"fraud anomaly"}
    else:                      return {"category":"UNKNOWN","confidence":0.45,"route":"PHI3_COMPLEX","reasoning":"unknown pattern"}


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 4 — fast_resolver (handles Redis HIT + ChromaDB HIT + PHI3_SIMPLE)
# ═══════════════════════════════════════════════════════════════════════════════
def node_fast_resolver(state: AgentState) -> AgentState:
    """
    Fast-path exit — no frontier LLM calls.
    Handles: Redis cache HIT, ChromaDB high-similarity HIT, Phi3 SIMPLE/SELF_HEALING.
    Goes straight to memory_writer → incident_closer.
    """
    path = state.get("triage_path","")
    log.info(f" [FAST_RESOLVER] Resolving via {path} (zero LLM cost)")

    if path == "REDIS_HIT":
        resolution = state.get("resolution_summary", "Replayed from Redis cache")
        log.info(f"Redis replay: {resolution[:80]}")
    elif path == "CHROMADB_HIT":
        best = state["past_incidents"][0] if state.get("past_incidents") else {}
        resolution = (
            f"Similar incident resolved previously: "
            f"{best.get('resolution','See ChromaDB record')}"
        )
        log.info(f"ChromaDB replay: similarity "
                 f"{best.get('similarity',0):.2f}")
    else:  # PHI3_SIMPLE
        resolution = (
            f"{state['triage_decision']} — classified as self-healing "
            f"by Phi3 (confidence: {state['triage_confidence']:.0%}). "
            f"No action required."
        )
        log.info(f"Phi3 simple resolution: {state['triage_decision']}")

    cached_report = state.get("investigator_report", {})
    impact = cached_report.get("business_impact", "") or cached_report.get("impact", "")
    summary = f"""
╔══════════════════════════════════════════════════════════════╗
║  FAST-PATH RESOLUTION  —  {state['incident_id'][:8].upper()}
╚══════════════════════════════════════════════════════════════╝
Alert:      {state['alert_name']} [{state['alert_severity'].upper()}]
Path:       {path}
Resolution: {resolution}
Impact:     {impact or 'See previous investigation'}
LLM Cost:   $0.0000  (fast-path — no frontier LLM calls)
Timestamp:  {datetime.utcnow().isoformat()}Z
"""
    print(summary)
    return {
        **state,
        "incident_resolved":  True,
        "resolution_summary": resolution,
        "summary":            summary,
        "total_cost":         0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 5 — investigator_initial (Claude — 1st pass, hypothesis formation)
# ═══════════════════════════════════════════════════════════════════════════════
def node_investigator_initial(state: AgentState) -> AgentState:
    """
    Claude's FIRST pass — forms hypotheses BEFORE specialist agents run.
    Has access to memory_retriever's past incidents (RAG context).
    """
    log.info(" [INVESTIGATOR_INITIAL/Claude] Forming hypotheses (Pass 1)...")
    import anthropic
    client = anthropic.Anthropic(api_key=get_key('anthropic-api-key'))

    # Build RAG context from past incidents
    memory_ctx = ""
    if state.get("past_incidents"):
        memory_ctx = "\nRELEVANT PAST INCIDENTS (from incident memory):\n"
        for p in state["past_incidents"]:
            memory_ctx += (
                f"  - [{p['similarity']:.0%} match] {p['alert_name']}: "
                f"root_cause={p['root_cause']}, "
                f"resolution={p['resolution']}\n"
            )

    prompt = f"""You are the Lead Investigator for an MLOps fraud detection platform.
CRITICAL: Respond with ONLY a single valid JSON object. No markdown. No newlines inside string values. No explanation before or after the JSON.
This is your FIRST PASS — form hypotheses before specialist agents investigate.

ALERT: {state['alert_name']} (Severity: {state['alert_severity']})
TRIAGE: {state['triage_decision']} (confidence: {state['triage_confidence']:.0%})
PATH: {state.get('triage_path','')}

PLATFORM CONTEXT:
{json.dumps(state['context'], indent=2)}
{memory_ctx}
Form 2-3 hypotheses. Specialists (Perplexity researcher + Gemini data scientist)
will investigate in parallel. You'll synthesize their findings after.

Respond ONLY with valid JSON:
{{
    "hypotheses": [
        {{"id":"H1","statement":"hypothesis","confidence":0.8,"evidence":["e1"]}}
    ],
    "primary_hypothesis": "H1",
    "investigation_focus": {{
        "for_researcher": "what to search externally",
        "for_data_scientist": "what to analyze statistically"
    }},
    "severity_assessment": "HIGH",
    "requires_immediate_human": false
}}
IMPORTANT: Return ONLY valid JSON. No markdown. All string values must be on a single line with no embedded newlines."""

    t0   = time.time()
    resp = client.messages.create(
        model='claude-sonnet-4-6', max_tokens=1024,
        messages=[{'role':'user','content':prompt}]
    )
    cost   = (resp.usage.input_tokens * 3 + resp.usage.output_tokens * 15) / 1_000_000
    result = parse_json(resp.content[0].text)

    primary = result.get("primary_hypothesis","H1")
    hyps    = result.get("hypotheses",[])
    log.info(f"Primary hypothesis: {primary} — "
             f"{next((h['statement'][:60] for h in hyps if h.get('id')==primary),'?')}")
    log.info(f"${cost:.4f} | {time.time()-t0:.1f}s")

    costs = {**state.get('agent_costs',{}), 'investigator_initial': cost}
    return {
        **state,
        "investigator_hypothesis": result,
        "requires_human":          result.get("requires_immediate_human", False),
        "total_cost":              cost,
        "agent_costs":             costs,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NODES 6 + 7 — researcher + data_scientist (parallel via Send API)
# ═══════════════════════════════════════════════════════════════════════════════
def node_researcher(state: AgentState) -> AgentState:
    log.info(" [RESEARCHER/Perplexity] External research (parallel)...")
    focus = state.get("investigator_hypothesis",{}).get(
        "investigation_focus",{}).get("for_researcher",
        state['alert_name']
    )
    cost = 0.0
    try:
        resp = requests.post(
            'https://api.perplexity.ai/chat/completions',
            headers={
                'Authorization': f"Bearer {get_key('perplexity-api-key')}",
                'Content-Type': 'application/json'
            },
            json={
                'model': 'sonar', 'max_tokens': 400,
                'messages': [{'role':'user','content':
                    f"MLOps investigation focus: {focus}\n"
                    f"Alert type: {state['alert_name']}\n"
                    f"Find known causes, patterns, and solutions. "
                    f"4-5 specific bullet points."}]
            },
            timeout=25
        )
        findings = resp.json()['choices'][0]['message']['content']
        cost     = 0.001
        # Log a short gist of findings for visibility
        gist = findings.replace("\n", " ")[:200]
        log.info(f"Research complete | ${cost:.4f}")
        log.info(f"Perplexity gist: {gist}...")
    except Exception as e:
        findings = f"Research unavailable: {str(e)[:80]}"
        log.warning(f"Perplexity error: {e}")

    costs = {**state.get('agent_costs',{}), 'researcher': cost}
    return {
        "researcher_report": {"findings": findings, "focus": focus},
        "total_cost":        cost,
        "agent_costs":       costs,
    }


def node_data_scientist(state: AgentState) -> AgentState:
    log.info(" [DATA_SCIENTIST/Gemini] Statistical analysis (parallel)...")
    focus = state.get("investigator_hypothesis",{}).get(
        "investigation_focus",{}).get("for_data_scientist",
        "Analyze drift and model metrics"
    )
    drift_score = state["context"]["drift"].get("drift_score","0")

    prompt = f"""You are a Data Scientist analyzing ML model health.
INVESTIGATION FOCUS: {focus}
PRIMARY HYPOTHESIS: {json.dumps(state.get('investigator_hypothesis',{}).get('hypotheses',[])[:1])}
DRIFT SCORE: {drift_score}
SERVING METRICS: {json.dumps(state['context'].get('serving',{}), indent=2)}

Respond ONLY with valid JSON:
{{
    "drift_confirmed": true,
    "drift_type": "data_drift",
    "statistical_significance": "high",
    "retraining_recommended": true,
    "retraining_urgency": "immediate",
    "key_findings": ["finding 1","finding 2"],
    "analysis_summary": "2-sentence statistical summary",
    "confidence": 0.85
}}"""

    cost     = 0.0
    analysis = {}
    try:
        from google import genai
        client = genai.Client(api_key=get_key('google-api-key'))
        t0   = time.time()
        resp = client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt
        )
        analysis = parse_json(resp.text)
        cost     = 0.0002
        log.info(f"Drift confirmed: {analysis.get('drift_confirmed','?')} | "
                 f"Retrain urgency: {analysis.get('retraining_urgency','?')} | "
                 f"${cost:.4f} | {time.time()-t0:.1f}s")
    except Exception as e:
        log.warning(f"Gemini error — Claude fallback: {str(e)[:60]}")
        import anthropic
        fb = anthropic.Anthropic(api_key=get_key('anthropic-api-key'))
        r2 = fb.messages.create(
            model='claude-sonnet-4-6', max_tokens=400,
            messages=[{'role':'user','content':prompt}]
        )
        analysis = parse_json(r2.content[0].text)
        cost     = (r2.usage.input_tokens * 3 + r2.usage.output_tokens * 15) / 1_000_000

    costs = {**state.get('agent_costs',{}), 'data_scientist': cost}
    return {
        "ds_report":   analysis,
        "total_cost":  cost,
        "agent_costs": costs,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 8 — parallel_join (fan-in — waits for researcher + data_scientist)
# ═══════════════════════════════════════════════════════════════════════════════
def node_parallel_join(state: AgentState) -> AgentState:
    """
    Fan-in node. LangGraph ensures both researcher + data_scientist
    have completed before this node runs. No LLM call — pure join.
    Also performs conflict detection: if specialists reach different
    conclusions, flags it so investigator_synthesis must explicitly reconcile.
    """
    log.info(" [PARALLEL_JOIN] Both specialists complete — merging findings...")
    researcher_done     = bool(state.get("researcher_report"))
    data_scientist_done = bool(state.get("ds_report"))
    log.info(f"Researcher: {'' if researcher_done else ''}  "
             f"Data Scientist: {'' if data_scientist_done else ''}")

    # ── Conflict Detection ────────────────────────────────────────────────────
    # Extract conclusion category from each specialist report
    def extract_category(report: dict) -> str:
        """Infer conclusion category from specialist report fields."""
        if not report:
            return "UNKNOWN"
        text = str(report).upper()
        # Data Scientist signals
        if report.get("drift_confirmed") is True:
            return "DATA_DRIFT"
        if report.get("retrain_urgency") in ("immediate", "high"):
            return "DATA_DRIFT"
        # Researcher signals
        if any(k in text for k in ("INFRA", "KUBERNETES", "KUBE", "CONTROL PLANE",
                                    "SCHEDULER", "ETCD", "NODE FAILURE")):
            return "CRITICAL_INFRA"
        if any(k in text for k in ("DRIFT", "DISTRIBUTION", "FEATURE SHIFT",
                                    "COVARIATE", "DATA QUALITY")):
            return "DATA_DRIFT"
        if any(k in text for k in ("PIPELINE", "INGESTION", "STALE", "FEATURE STORE")):
            return "PIPELINE_FAILURE"
        return "UNKNOWN"

    researcher_cat     = extract_category(state.get("researcher_report", {}))
    data_scientist_cat = extract_category(state.get("ds_report", {}))

    conflict = {}
    if (researcher_cat != "UNKNOWN" and
        data_scientist_cat != "UNKNOWN" and
        researcher_cat != data_scientist_cat):
        conflict = {
            "detected":                   True,
            "researcher_conclusion":      researcher_cat,
            "data_scientist_conclusion":  data_scientist_cat,
            "conflict_note": (
                f"Specialists DISAGREE — "
                f"Researcher: {researcher_cat} vs "
                f"Data Scientist: {data_scientist_cat}. "
                f"Claude MUST explicitly state which finding it accepts and why."
            ),
        }
        log.warning(f"CONFLICT DETECTED: "
                    f"Researcher={researcher_cat} vs "
                    f"Data Scientist={data_scientist_cat}")
        log.warning(f"→ Synthesis prompt will require explicit reconciliation")
    else:
        if researcher_cat == data_scientist_cat and researcher_cat != "UNKNOWN":
            log.info(f"Specialists AGREE: both concluded {researcher_cat}")
        else:
            log.info(f"Categories: "
                     f"Researcher={researcher_cat}, "
                     f"DataScientist={data_scientist_cat} (no conflict)")

    return {**state, "specialist_conflict": conflict}


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 9 — investigator_synthesis (Claude — 2nd pass, root cause synthesis)
# ═══════════════════════════════════════════════════════════════════════════════
def node_investigator_synthesis(state: AgentState) -> AgentState:
    """
    Claude's SECOND pass — now with ALL specialist findings.
    Produces the definitive root cause analysis.
    """
    log.info(" [INVESTIGATOR_SYNTHESIS/Claude] Synthesizing findings (Pass 2)...")
    import anthropic
    client = anthropic.Anthropic(api_key=get_key('anthropic-api-key'))

    prompt = f"""You are the Lead Investigator. You now have findings from all specialists.
CRITICAL: Respond with ONLY a single valid JSON object. No markdown. No newlines inside string values.
Synthesize everything into a definitive root cause analysis.

ORIGINAL HYPOTHESES:
{json.dumps(state.get('investigator_hypothesis',{}), indent=2)}

RESEARCHER FINDINGS (Perplexity):
{json.dumps(state.get('researcher_report',{}), indent=2)}

DATA SCIENTIST ANALYSIS (Gemini):
{json.dumps(state.get('ds_report',{}), indent=2)}

ALERT: {state['alert_name']} [{state['alert_severity']}]
CONTEXT: {json.dumps(state['context'], indent=2)}

Compare your original hypotheses against specialist findings.
Which hypothesis was confirmed? What did specialists reveal that you missed?

{{conflict_block}}
Respond ONLY with valid JSON:
{{
    "root_cause": "definitive one-sentence root cause",
    "confidence": 0.91,
    "hypothesis_validated": "H1",
    "evidence": ["confirmed evidence 1","confirmed evidence 2","specialist finding"],
    "severity": "HIGH",
    "recommended_actions": [
        {{"action":"description","type":"autonomous","reversible":true,"priority":1}}
    ],
    "requires_immediate_human": false,
    "business_impact": "impact description",
    "resolution_summary": "concise resolution for memory storage"
}}
IMPORTANT: Return ONLY valid JSON. No markdown. All string values must be on a single line with no embedded newlines."""

    # ── Build conflict block if specialists disagreed ────────────────────────
    conflict = state.get("specialist_conflict", {})
    if conflict.get("detected"):
        conflict_block = (
            f"\n  SPECIALIST CONFLICT DETECTED:\n"
            f"   Researcher concluded: {conflict.get('researcher_conclusion','?')}\n"
            f"   Data Scientist concluded: {conflict.get('data_scientist_conclusion','?')}\n"
            f"   {conflict.get('conflict_note','')}\n"
            f"   You MUST explicitly address this disagreement in your root_cause.\n"
            f"   State which specialist you agree with and exactly why.\n"
            f"   Do NOT average or hedge — make a definitive call.\n"
        )
        log.warning(f"Injecting conflict block into synthesis prompt")
    else:
        conflict_block = ""

    prompt = prompt.replace("{{conflict_block}}", conflict_block)

    t0   = time.time()
    resp = client.messages.create(
        model='claude-sonnet-4-6', max_tokens=2048,
        messages=[{'role':'user','content':prompt}]
    )
    cost   = (resp.usage.input_tokens * 3 + resp.usage.output_tokens * 15) / 1_000_000
    report = parse_json(resp.content[0].text)
    # DEBUG — log raw Claude response so we can see parse failures
    _raw_synthesis = resp.content[0].text
    if report.get("parse_error"):
        with open("/tmp/synthesis_raw.txt", "w") as _dbf:
            _dbf.write(_raw_synthesis)
        log.warning(f"parse_json FAILED — raw saved to /tmp/synthesis_raw.txt")
        log.warning(f"First 200 chars: {_raw_synthesis[:200]}")
    log.info(f"Root cause: {report.get('root_cause','?')[:80]}")
    # ── GATE 2: Synthesis confidence checkpoint ───────────────────────────────
    synth_conf = report.get('confidence', 0)
    if isinstance(synth_conf, (int, float)):
        gate2_pass = float(synth_conf) >= 0.70
    else:
        try: gate2_pass = float(str(synth_conf).strip('%')) / 100 >= 0.70
        except: gate2_pass = False
    log.info(f"GATE 2 (Synthesis confidence): {synth_conf} {' PASS' if gate2_pass else ' FAIL — will trigger human escalation'} (threshold: 70%)")
    log.info(f"Confidence: {report.get('confidence',0):.0%} | ${cost:.4f} | {time.time()-t0:.1f}s")

    costs = {**state.get('agent_costs',{}), 'investigator_synthesis': cost}
    return {
        **state,
        "investigator_report": report,
        "requires_human":      report.get("requires_immediate_human", False),
        "resolution_summary":  report.get("resolution_summary",""),
        "total_cost":          cost,
        "agent_costs":         costs,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 10 — operator (GPT-4o-mini — remediation plan)
# ═══════════════════════════════════════════════════════════════════════════════
def node_operator(state: AgentState) -> AgentState:
    log.info(" [OPERATOR/GPT-5.4-mini] Generating remediation plan...")
    from openai import OpenAI
    client = OpenAI(api_key=get_key('openai-api-key'))

    prompt = f"""You are an MLOps Operator. Generate precise remediation.
ROOT CAUSE: {state['investigator_report'].get('root_cause','')}
STATISTICAL ANALYSIS: {json.dumps(state['ds_report'], indent=2)}
RESEARCHER FINDINGS: {state['researcher_report'].get('findings','')}
RECOMMENDED ACTIONS: {json.dumps(state['investigator_report'].get('recommended_actions',[]), indent=2)}

Respond ONLY with valid JSON:
{{
    "autonomous_actions": [
        {{"id":"A1","description":"clear description",
          "command":"exact kubectl or python command",
          "safe_to_auto_execute":true,"reversible":true,"timeout_seconds":60}}
    ],
    "human_runbook": {{
        "title":"runbook title",
        "steps":["step 1","step 2","step 3"],
        "estimated_minutes":10
    }},
    "post_fix_validation":"prometheus_query_to_verify"
}}"""

    t0   = time.time()
    resp = client.chat.completions.create(
        model='gpt-5.4-mini', max_completion_tokens=600,
        response_format={"type": "json_object"},
        messages=[
            {'role':'system','content':'Precise MLOps operator. JSON only.'},
            {'role':'user','content':prompt}
        ]
    )
    raw = resp.choices[0].message.content or "{}"
    plan = parse_json(raw)
    cost = (resp.usage.prompt_tokens * 0.15 +
            resp.usage.completion_tokens * 0.60) / 1_000_000
    log.info(f"{len(plan.get('autonomous_actions',[]))} actions | "
             f"${cost:.4f} | {time.time()-t0:.1f}s")

    costs = {**state.get('agent_costs',{}), 'operator': cost}
    return {
        **state,
        "operator_plan": plan,
        "total_cost":    cost,
        "agent_costs":   costs,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 11 — guardian (Claude — safety gate)
# ═══════════════════════════════════════════════════════════════════════════════
def node_guardian(state: AgentState) -> AgentState:
    log.info("[GUARDIAN/Claude] Safety check...")
    import anthropic
    client = anthropic.Anthropic(api_key=get_key('anthropic-api-key'))

    prompt = f"""You are the Guardian — safety authority for autonomous MLOps actions.

IMPORTANT CONTEXT:
- "PHI3_COMPLEX" is a Phi3 LLM triage classification label — it has NOTHING to do with
  Protected Health Information (PHI) or healthcare data. Do NOT treat it as sensitive health data.
- "TRIAGE_CATEGORY: DATA_DRIFT" means the Phi3 model classified the alert type, not that
  actual patient data is involved. This is a fraud detection ML platform, not a healthcare system.

DECISION RULES (apply in order):
1. BLOCK if root cause confidence < 0.70
2. BLOCK if an autonomous action directly modifies production data or is truly irreversible
3. APPROVE if all proposed actions are type "human_required" — these are escalations, not
   autonomous executions. Flagging for human review is always safe to approve.
4. APPROVE if all autonomous actions are safe, reversible, and well-understood
5. If no actions are present but confidence >= 0.70, APPROVE to allow investigation to complete

ROOT CAUSE CONFIDENCE: {state['investigator_report'].get('confidence', 0)}
SEVERITY:              {state['investigator_report'].get('severity', 'UNKNOWN')}
ALERT NAME:            {state.get('alert_name', '')}
TRIAGE CATEGORY:       {state.get('triage_decision', '')} (this is a Phi3 ML classification label)

PROPOSED ACTIONS:
{json.dumps(state['operator_plan'].get('autonomous_actions', []), indent=2)}

Respond ONLY with valid JSON:
{{
    "decision": "APPROVE",
    "reasoning": "one sentence explaining your decision",
    "approved_action_ids": ["A1"],
    "blocked_action_ids": [],
    "escalate_to_human": false
}}"""

    resp   = client.messages.create(
        model='claude-sonnet-4-6', max_tokens=300,
        messages=[{'role':'user','content':prompt}]
    )
    decision = parse_json(resp.content[0].text)
    cost     = (resp.usage.input_tokens * 3 +
                resp.usage.output_tokens * 15) / 1_000_000
    log.info(f"{decision.get('decision','?')} — {decision.get('reasoning','')}")
    log.info(f"${cost:.4f}")

    costs = {**state.get('agent_costs',{}), 'guardian': cost}
    return {
        **state,
        "guardian_decision": decision.get("decision","BLOCK"),
        "requires_human":    decision.get("escalate_to_human", False),
        "total_cost":        cost,
        "agent_costs":       costs,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 12 — executor
# ═══════════════════════════════════════════════════════════════════════════════
def node_executor(state: AgentState) -> AgentState:
    if state["guardian_decision"] != "APPROVE":
        log.info("[EXECUTOR] Skipped — Guardian did not APPROVE")
        return state
    log.info(" [EXECUTOR] Running approved actions (dry-run)...")
    executed = []
    for a in state["operator_plan"].get("autonomous_actions", []):
        if a.get("safe_to_auto_execute") and a.get("reversible"):
            log.info(f"[DRY-RUN] {a.get('id')}: {a.get('description')}")
            log.info(f"Command: {a.get('command','')}")
            executed.append(f"{a.get('id')}: {a.get('description')}")
        else:
            log.info(f"SKIPPED (unsafe/irreversible): {a.get('id')}")
    return {
        **state,
        "actions_executed":  executed,
        "incident_resolved": len(executed) > 0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 13 — human_escalation
# ═══════════════════════════════════════════════════════════════════════════════
def node_human_escalation(state: AgentState) -> AgentState:
    log.warning(" [ESCALATE] Human intervention required")
    log.warning(f"{state['investigator_report'].get('root_cause','')}")
    runbook = state.get('operator_plan',{}).get('human_runbook',{})
    for step in runbook.get('steps',[]):
        log.warning(f"→ {step}")
    return {**state, "incident_resolved": False}


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 14 — memory_writer (ChromaDB + Redis resolution cache)
# ═══════════════════════════════════════════════════════════════════════════════
# ── Node: verify ─────────────────────────────────────────────────────────────
def node_verify(state: AgentState) -> AgentState:
    """
    Task 4 — Post-fix validation.
    Re-queries Prometheus immediately after executor runs to check
    whether the metrics that triggered the alert have improved.
    """
    log.info(" [VERIFY] Post-fix validation — re-querying metrics...")

    alert_name  = state.get("alert_name", "")
    context     = state.get("context", {})
    actions     = state.get("actions_executed", [])

    # If no actions were executed (guardian BLOCK or 0 actions), skip
    if not actions:
        log.info("No actions were executed — skipping verify")
        return {
            "verify_result": {
                "skipped": True,
                "reason": "no_actions_executed",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            },
            "incident_resolved": False,
        }

    # Re-query Prometheus for the same metrics collected in context_collector
    verify_metrics = {}
    prom_base = "http://localhost:32090"

    queries = {
        "drift_score":  'fraud_drift_score',
        "request_rate": 'rate(fraud_api_requests_total[2m])',
        "error_rate":   'rate(fraud_api_requests_total{status="error"}[2m])',
        "fraud_rate":   'fraud_detection_rate',
    }

    for metric_name, promql in queries.items():
        try:
            import requests as _req
            resp = _req.get(
                f"{prom_base}/api/v1/query",
                params={"query": promql},
                timeout=5
            )
            data = resp.json()
            results = data.get("data", {}).get("result", [])
            if results:
                verify_metrics[metric_name] = float(results[0]["value"][1])
            else:
                verify_metrics[metric_name] = "no_data"
        except Exception:
            verify_metrics[metric_name] = "no_data"

    # Compare against pre-fix metrics
    pre_fix  = context.get("drift", {})
    pre_drift = pre_fix.get("drift_score", "no_data")
    post_drift = verify_metrics.get("drift_score", "no_data")

    # Determine if resolved
    resolved = False
    resolution_note = ""

    if post_drift == "no_data" and pre_drift == "no_data":
        resolution_note = "Metrics still unavailable (infrastructure likely still recovering)"
        resolved = False
    elif post_drift != "no_data" and (pre_drift == "no_data" or str(pre_drift) == "no_data"):
        resolution_note = f"Metrics recovered: drift_score now {post_drift:.3f}"
        resolved = True
    elif post_drift != "no_data" and pre_drift != "no_data":
        if float(post_drift) < float(pre_drift) * 0.8:
            resolution_note = f"Drift improved: {pre_drift:.3f} → {post_drift:.3f}"
            resolved = True
        else:
            resolution_note = f"Drift unchanged: {pre_drift} → {post_drift}"
            resolved = False
    else:
        resolution_note = "Unable to determine resolution status"
        resolved = False

    verify_result = {
        "skipped":         False,
        "pre_fix_metrics": {"drift_score": str(pre_drift)},
        "post_fix_metrics": verify_metrics,
        "resolved":        resolved,
        "resolution_note": resolution_note,
        "timestamp":       datetime.utcnow().isoformat() + "Z",
    }

    log.info(f"Pre-fix  drift: {pre_drift}")
    log.info(f"Post-fix drift: {post_drift}")
    log.info(f"Resolved: {resolved} — {resolution_note}")

    return {
        "verify_result":    verify_result,
        "incident_resolved": resolved,
    }


# ── Node: post_fix_monitor ────────────────────────────────────────────────────
def node_post_fix_monitor(state: AgentState) -> AgentState:
    """
    Task 5 — Watch metrics for MONITOR_WINDOW seconds after executor runs.
    Polls Prometheus every POLL_INTERVAL seconds.
    Writes stability verdict to state for memory_writer to store.
    """
    MONITOR_WINDOW = 60   # seconds to watch (2 min in prod, 60s for demo)
    POLL_INTERVAL  = 20   # seconds between polls

    verify_result = state.get("verify_result", {})

    # Skip if no actions ran or verify already confirmed resolved
    if verify_result.get("skipped"):
        log.info("[POST_FIX_MONITOR] Skipping — no actions were executed")
        return {
            "post_fix_monitor_result": {
                "skipped": True,
                "reason":  "no_actions_executed",
            }
        }

    log.info(f" [POST_FIX_MONITOR] Watching metrics for {MONITOR_WINDOW}s...")

    import requests as _req
    import time as _time

    prom_base  = "http://localhost:32090"
    samples    = []
    start_time = _time.time()

    while _time.time() - start_time < MONITOR_WINDOW:
        sample = {"timestamp": datetime.utcnow().isoformat() + "Z"}
        for metric, promql in [
            ("drift_score",  "fraud_drift_score"),
            ("fraud_rate",   "fraud_detection_rate"),
            ("error_rate",   'rate(fraud_api_requests_total{status="error"}[2m])'),
        ]:
            try:
                r = _req.get(
                    f"{prom_base}/api/v1/query",
                    params={"query": promql},
                    timeout=5
                )
                res = r.json().get("data", {}).get("result", [])
                sample[metric] = float(res[0]["value"][1]) if res else "no_data"
            except Exception:
                sample[metric] = "no_data"

        samples.append(sample)
        elapsed = int(_time.time() - start_time)
        log.info(f"[{elapsed:3d}s] drift={sample.get('drift_score','?')}  "
                 f"fraud_rate={sample.get('fraud_rate','?')}  "
                 f"error_rate={sample.get('error_rate','?')}")
        _time.sleep(POLL_INTERVAL)

    # Verdict: stable if last sample shows no_data resolved or drift < 0.5
    last = samples[-1] if samples else {}
    last_drift = last.get("drift_score", "no_data")

    if last_drift == "no_data":
        verdict = "INCONCLUSIVE"
        verdict_note = "Metrics still unavailable at end of monitoring window — infrastructure may still be recovering"
    elif float(last_drift) < 0.5:
        verdict = "STABLE"
        verdict_note = f"Drift score stable at {last_drift:.3f} — below 0.5 threshold"
    elif float(last_drift) < 0.75:
        verdict = "IMPROVING"
        verdict_note = f"Drift score {last_drift:.3f} — improving but not yet below threshold"
    else:
        verdict = "DEGRADED"
        verdict_note = f"Drift score {last_drift:.3f} — still above threshold after {MONITOR_WINDOW}s"

    result = {
        "skipped":      False,
        "verdict":      verdict,
        "verdict_note": verdict_note,
        "samples":      samples,
        "monitor_window_seconds": MONITOR_WINDOW,
        "timestamp":    datetime.utcnow().isoformat() + "Z",
    }

    log.info(f"Verdict: {verdict} — {verdict_note}")

    return {"post_fix_monitor_result": result}


def node_memory_writer(state: AgentState) -> AgentState:
    """
    Closes the self-improving loop.
    Write 1: ChromaDB — semantic memory for future RAG queries
    Write 2: Redis resolution cache — exact-match fast-path (24h TTL)
    """
    log.info(" [MEMORY_WRITER] Storing incident to memory...")

    # Skip if this was already a fast-path replay (avoid overwriting with less info)
    if state.get("triage_path") in ("REDIS_HIT", "CHROMADB_HIT"):
        log.info("Fast-path replay — skipping memory write (already stored)")
        return state

    report    = state.get("investigator_report", {})
    root_cause = report.get("root_cause", "")
    resolution = state.get("resolution_summary", root_cause)

    if not root_cause:
        log.info("No root cause — skipping memory write")
        return state

    # ── Write 1: ChromaDB ──────────────────────────────────────────────────
    collection = get_chroma_collection()
    if collection is not None:
        doc_text = (
            f"Alert: {state['alert_name']} "
            f"Severity: {state['alert_severity']} "
            f"Root cause: {root_cause} "
            f"Resolution: {resolution}"
        )
        embedding = embed_text(doc_text)
        # ── GATE 3: Post-fix quality checkpoint ─────────────────────────
        _monitor  = state.get("post_fix_monitor_result", {})
        _verdict  = _monitor.get("verdict", "INCONCLUSIVE")
        _g3_pass  = _verdict in ("STABLE", "IMPROVING")
        _quality  = "verified" if _g3_pass else "unverified"
        _g3_label = (" verified resolution" if _g3_pass
                     else "  unverified")
        log.info(f"GATE 3 (Post-fix): verdict={_verdict} {_g3_label}")
        if embedding:
            try:
                collection.add(
                    ids=[state["incident_id"]],
                    embeddings=[embedding],
                    documents=[doc_text],
                    metadatas=[{
                        "alert_name":  state["alert_name"],
                        "severity":    state["alert_severity"],
                        "root_cause":  root_cause[:500],
                        "resolution":  resolution[:500],
                        "triage_path": state.get("triage_path",""),
                        "confidence":  str(report.get("confidence",0)),
                        "cost":        str(state.get("total_cost",0)),
                        "timestamp":   datetime.utcnow().isoformat(),
                        "resolved":    str(state.get("incident_resolved",False)),
                    }]
                )
                log.info(f"ChromaDB: incident stored "
                         f"(collection now has {collection.count()} incidents)")
            except Exception as e:
                log.warning(f"ChromaDB write error: {e}")
        else:
            log.warning("Embedder unavailable — ChromaDB write skipped")

    # ── Write 2: Redis resolution cache ───────────────────────────────────
    r = get_redis()
    if r is not None and root_cause and float(report.get("confidence", 0)) >= 0.70:
        try:
            res_key = f"resolution:{state['fingerprint']}"
            cache_payload = {
                "root_cause":  root_cause,
                "resolution":  resolution,
                "confidence":  report.get("confidence",0),
                "timestamp":   datetime.utcnow().isoformat(),
                "alert_name":  state["alert_name"],
            }
            r.setex(res_key, RESOLUTION_TTL, json.dumps(cache_payload))
            log.info(f"Redis: resolution cached "
                     f"(key: resolution:{state['fingerprint']}, TTL: 24h)")
        except Exception as e:
            log.warning(f"Redis write error: {e}")

    return state


# ═══════════════════════════════════════════════════════════════════════════════
# NODE 15 — incident_closer (final summary)
# ═══════════════════════════════════════════════════════════════════════════════
# ── Node: triage_retrospective ───────────────────────────────────────────────
def node_triage_retrospective(state: AgentState) -> AgentState:
    """
    Task 7 — Self-improving loop.
    Compares Phi3 triage classification vs Claude final root cause.
    Logs agreement/disagreement to build a fine-tuning signal over time.
    Stores retrospective to Redis for future analysis.
    """
    log.info(" [TRIAGE_RETROSPECTIVE] Comparing Phi3 vs Claude diagnoses...")

    triage_cat   = state.get("triage_decision", state.get("triage_category", "UNKNOWN"))   # Phi3 output
    triage_path  = state.get("triage_path", "UNKNOWN")

    # Fast-path runs: Phi3 made no classification — skip accuracy scoring
    if triage_path in ("REDIS_HIT", "CHROMADB_HIT"):
        log.info("Fast-path run — skipping Phi3 accuracy scoring")
        return {**state}
    phi3_conf    = state.get("triage_confidence", 0)

    inv_report   = state.get("investigator_report", {})
    root_cause   = inv_report.get("root_cause", "")
    claude_conf  = inv_report.get("confidence", 0)
    severity     = inv_report.get("severity", "UNKNOWN")

    # Map Claude root cause to a category for comparison
    root_lower = root_cause.lower()
    if any(k in root_lower for k in ["kubernetes", "k8s", "etcd", "control plane",
                                       "kubeproxy", "kubescheduler", "infrastructure"]):
        claude_category = "CRITICAL_INFRA"
    elif any(k in root_lower for k in ["drift", "distribution", "feature", "schema"]):
        claude_category = "DATA_DRIFT"
    elif any(k in root_lower for k in ["latency", "timeout", "slow", "p99"]):
        claude_category = "LATENCY_SLO"
    elif any(k in root_lower for k in ["fraud", "anomaly", "false positive", "rate"]):
        claude_category = "FRAUD_ANOMALY"
    elif any(k in root_lower for k in ["false positive", "no_data", "null", "blackout"]):
        claude_category = "FALSE_POSITIVE"
    else:
        claude_category = "UNKNOWN"

    # Agreement check
    # Note: DATA_DRIFT alert can be CRITICAL_INFRA root cause (false positive)
    # This is a correct disagreement — Phi3 classified the alert type,
    # Claude diagnosed the root cause. Track both.
    agreed = (triage_cat == claude_category)

    # Special case: if Phi3 said DATA_DRIFT but root cause is CRITICAL_INFRA,
    # that is a known false-positive pattern — not a Phi3 error per se
    false_positive_pattern = (
        triage_cat == "DATA_DRIFT" and
        claude_category == "CRITICAL_INFRA" and
        "false positive" in root_lower
    )

    if false_positive_pattern:
        verdict = "FALSE_POSITIVE_DETECTED"
        verdict_note = "Phi3 correctly routed the alert type; Claude correctly identified the infra root cause as false positive trigger"
    elif agreed:
        verdict = "AGREEMENT"
        verdict_note = f"Phi3 and Claude both classified as {triage_cat}"
    else:
        verdict = "DISAGREEMENT"
        verdict_note = f"Phi3 said {triage_cat}, Claude concluded {claude_category}"

    retrospective = {
        "incident_id":     state.get("incident_id", ""),
        "alert_name":      state.get("alert_name", ""),
        "phi3_category":   triage_cat,
        "phi3_confidence": phi3_conf,
        "phi3_route":      triage_path,
        "claude_category": claude_category,
        "claude_confidence": claude_conf,
        "agreed":          agreed,
        "false_positive_pattern": false_positive_pattern,
        "verdict":         verdict,
        "verdict_note":    verdict_note,
        "timestamp":       datetime.utcnow().isoformat() + "Z",
    }

    log.info(f"Phi3  said: {triage_cat} ({phi3_conf:.0%} confidence)")
    log.info(f"Claude said: {claude_category} ({claude_conf:.0%} confidence)")
    log.info(f"Verdict: {verdict}")
    log.info(f"Note: {verdict_note}")

    # Store to Redis for aggregate analysis (list, capped at 100 entries)
    try:
        r = get_redis()
        if r:
            import json as _json
            r.lpush("triage_retrospective_log", _json.dumps(retrospective))
            r.ltrim("triage_retrospective_log", 0, 99)  # keep last 100
            total = r.llen("triage_retrospective_log")
            log.info(f"Retrospective stored (total log entries: {total})")

            # Running accuracy stats
            all_entries = [_json.loads(x) for x in r.lrange("triage_retrospective_log", 0, -1)]
            agreements  = sum(1 for e in all_entries if e.get("agreed") or
                             e.get("false_positive_pattern"))
            accuracy    = agreements / len(all_entries) if all_entries else 0
            log.info(f"Phi3 accuracy (lifetime): {accuracy:.0%} "
                     f"({agreements}/{len(all_entries)} correct)")
    except Exception as e:
        log.warning(f"Retrospective storage failed: {e}")

    return {"triage_retrospective": retrospective}


def node_incident_closer(state: AgentState) -> AgentState:
    """
    Task 6 — Structured incident output.
    Produces a human-readable report AND a machine-readable
    JSON ticket payload compatible with JIRA/PagerDuty/ServiceNow.
    """
    report   = state.get("investigator_report", {})
    ds       = state.get("ds_report", {})
    costs    = state.get("agent_costs", {})
    total    = sum(costs.values())
    actions  = state.get("actions_executed", [])

    # ── Structured ticket payload ─────────────────────────────────────────────
    severity_map = {"CRITICAL": "P1", "HIGH": "P2", "MEDIUM": "P3", "LOW": "P4"}
    sev_raw  = str(report.get("severity", state.get("alert_severity","HIGH"))).upper()
    priority = severity_map.get(sev_raw, "P2")

    gate_verdicts = {
        "gate1_triage":    "PASS" if state.get("triage_confidence", 0) >= 0.70 else "FAIL",
        "gate2_synthesis": "PASS" if report.get("confidence", 0) >= 0.70 else "FAIL",
        "gate3_postfix":   state.get("post_fix_monitor_result", {}).get("verdict", "SKIPPED")
                           if isinstance(state.get("post_fix_monitor_result"), dict) else "SKIPPED",
    }

    ticket = {
        "summary":          f"[MLOps-Agent] {state['alert_name']}: {str(report.get('root_cause','Unknown'))[:80]}",
        "priority":         priority,
        "labels":           ["mlops-agent", "autonomous", state["alert_name"], state.get("triage_path","unknown")],
        "incident_key":     state["fingerprint"],
        "severity":         state.get("alert_severity", "warning").lower(),
        "alert_name":       state["alert_name"],
        "root_cause":       report.get("root_cause", "Unknown"),
        "confidence":       report.get("confidence", 0),
        "hypothesis_validated": report.get("hypothesis_validated", ""),
        "evidence":         report.get("evidence", []),
        "business_impact":  report.get("business_impact", ""),
        "recommended_actions": report.get("recommended_actions", []),
        "actions_executed": actions,
        "requires_human":   state.get("requires_human", False),
        "guardian_decision":state.get("guardian_decision", ""),
        "triage_path":      state.get("triage_path", ""),
        "triage_confidence":state.get("triage_confidence", 0),
        "triage_category":  state.get("triage_decision", ""),
        "fast_path":        state.get("memory_hit", False),
        "gate_verdicts":    gate_verdicts,
        "all_gates_passed": all(v == "PASS" for v in gate_verdicts.values() if v != "SKIPPED"),
        "specialist_conflict": state.get("specialist_conflict", {}),
        "drift_confirmed":  ds.get("drift_confirmed", False),
        "retrain_urgency":  ds.get("retrain_urgency", ds.get("retraining_urgency", "none")),
        "llm_cost_usd":     round(total, 6),
        "cost_breakdown":   {k: round(v, 6) for k, v in costs.items()},
        "fast_path_savings":round(0.035 - total, 6) if state.get("memory_hit") else 0.0,
        "stored_to_memory": not state.get("memory_hit", False),
        "rag_hits":         len(state.get("past_incidents", [])),
        "incident_id":      state.get("incident_id", ""),
        "timestamp":        datetime.utcnow().isoformat() + "Z",
        "resolution_status":"resolved_dry_run" if state["incident_resolved"] else "requires_human",
    }

    # Pre-compute display strings (no backslashes in f-strings)
    _resolved_str = " (dry-run)" if state["incident_resolved"] else "Requires human"
    _memory_str   = " Stored to ChromaDB + Redis" if not state.get("memory_hit") else " Fast-path (already in memory)"
    _actions_str  = chr(10).join(f"  • {a}" for a in actions) or "  • None executed"
    _costs_str    = chr(10).join(f"  {k:35s} ${v:.4f}" for k,v in costs.items())
    _gates_str    = "" if ticket["all_gates_passed"] else ""
    _sep          = "─" * 42

    summary = (
        f"\n╔══════════════════════════════════════════════════════════════╗"
        f"\n║  MLOPS INCIDENT REPORT  —  {state['incident_id'][:8].upper()}"
        f"\n╚══════════════════════════════════════════════════════════════╝"
        f"\nAlert:        {state['alert_name']} [{state['alert_severity'].upper()}]"
        f"\nTriage Path:  {state.get('triage_path','')} ({state['triage_confidence']:.0%})"
        f"\nTriage Cat:   {state['triage_decision']}"
        f"\n"
        f"\nRoot Cause:   {report.get('root_cause','N/A')}"
        f"\nConfidence:   {report.get('confidence',0):.0%}"
        f"\nImpact:       {report.get('business_impact','N/A')}"
        f"\n"
        f"\nRetrain:      {ds.get('drift_confirmed','?')} ({ds.get('retrain_urgency', ds.get('retraining_urgency','?'))})"
        f"\nGuardian:     {state.get('guardian_decision','N/A')}"
        f"\nResolved:     {_resolved_str}"
        f"\n"
        f"\nActions:\n{_actions_str}"
        f"\n"
        f"\nMemory:       {_memory_str}"
        f"\n"
        f"\nAgent Costs:\n{_costs_str}"
        f"\n  {_sep}"
        f"\n  {'TOTAL':35s} ${total:.4f}"
        f"\n"
        f"\nTimestamp:    {datetime.utcnow().isoformat()}Z"
        f"\n"
    )

    log.info(summary)
    log.info(
        f" Ticket: priority={ticket['priority']} "
        f"confidence={ticket['confidence']:.0%} "
        f"gates={_gates_str} "
        f"cost=${ticket['llm_cost_usd']:.4f} "
        f"actions={len(actions)}"
    )

    return {**state, "summary": summary, "ticket": ticket}

# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════════════════════
def route_after_triage(state: AgentState) -> str:
    """Route triage output to fast_resolver or full pipeline."""
    path = state.get("triage_path", "")
    if state.get("triage_decision") == "DUPLICATE":
        return "node_incident_closer"
    if path in ("REDIS_HIT", "CHROMADB_HIT", "PHI3_SIMPLE"):
        return "node_fast_resolver"
    return "node_investigator_initial"

def route_after_guardian(state: AgentState) -> str:
    return "node_human_escalation" if state.get("requires_human") else "node_executor"

def build_graph():
    from langgraph.graph import StateGraph, END
    from langgraph.types import Send

    g = StateGraph(AgentState)

    # Register all nodes
    for name, fn in [
        ("node_alert_intake",           node_alert_intake),
        ("node_context_collector",      node_context_collector),
        ("node_memory_retriever",       node_memory_retriever),
        ("node_triage",                 node_triage),
        ("node_fast_resolver",          node_fast_resolver),
        ("node_investigator_initial",   node_investigator_initial),
        ("node_researcher",             node_researcher),
        ("node_data_scientist",         node_data_scientist),
        ("node_parallel_join",          node_parallel_join),
        ("node_investigator_synthesis", node_investigator_synthesis),
        ("node_operator",               node_operator),
        ("node_guardian",               node_guardian),
        ("node_executor",               node_executor),
        ("node_verify",                node_verify),
        ("node_post_fix_monitor",      node_post_fix_monitor),
        ("node_triage_retrospective", node_triage_retrospective),
        ("node_human_escalation",       node_human_escalation),
        ("node_memory_writer",          node_memory_writer),
        ("node_incident_closer",        node_incident_closer),
    ]:
        g.add_node(name, fn)

    # ── Edges ──────────────────────────────────────────────────────────────
    g.set_entry_point("node_alert_intake")
    g.add_edge("node_alert_intake",       "node_context_collector")
    g.add_edge("node_context_collector",  "node_memory_retriever")
    g.add_edge("node_memory_retriever",   "node_triage")

    # Triage conditional — fast path OR full pipeline
    g.add_conditional_edges(
        "node_triage",
        route_after_triage,
        {
            "node_fast_resolver":        "node_fast_resolver",
            "node_investigator_initial": "node_investigator_initial",
            "node_incident_closer":      "node_incident_closer",
        }
    )

    # Fast path → memory_writer → incident_closer
    g.add_edge("node_fast_resolver",         "node_memory_writer")

    # Full pipeline — parallel fan-out after investigator_initial
    g.add_edge("node_investigator_initial",  "node_researcher")
    g.add_edge("node_investigator_initial",  "node_data_scientist")

    # Fan-in
    g.add_edge("node_researcher",            "node_parallel_join")
    g.add_edge("node_data_scientist",        "node_parallel_join")

    # Synthesis → operator → guardian
    g.add_edge("node_parallel_join",         "node_investigator_synthesis")
    g.add_edge("node_investigator_synthesis","node_operator")
    g.add_edge("node_operator",              "node_guardian")

    # Guardian gate
    g.add_conditional_edges(
        "node_guardian",
        route_after_guardian,
        {
            "node_executor":         "node_executor",
            "node_human_escalation": "node_human_escalation",
        }
    )

    # Both executor + escalation → memory_writer → incident_closer
    g.add_edge("node_executor",          "node_verify")
    g.add_edge("node_verify",            "node_post_fix_monitor")
    g.add_edge("node_post_fix_monitor",  "node_memory_writer")
    g.add_edge("node_human_escalation",  "node_memory_writer")
    g.add_edge("node_memory_writer",        "node_triage_retrospective")
    g.add_edge("node_triage_retrospective", "node_incident_closer")
    g.add_edge("node_incident_closer",   END)

    return g.compile()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINTS
# ═══════════════════════════════════════════════════════════════════════════════
def run_investigation(alert_name: str, severity: str = "warning") -> dict:
    log.info(f"\n{'='*60}")
    log.info(f" INCIDENT: {alert_name} [{severity.upper()}]")
    log.info(f"{'='*60}")
    graph = build_graph()
    return graph.invoke(AgentState(
        incident_id=str(uuid.uuid4()),
        alert_name=alert_name,
        alert_severity=severity,
        fingerprint="",
        context={},
        past_incidents=[],
        memory_hit=False,
        triage_decision="",
        triage_confidence=0.0,
        triage_path="",
        investigator_hypothesis={},
        researcher_report={},
        ds_report={},
        investigator_report={},
        operator_plan={},
        guardian_decision="",
        actions_executed=[],
        incident_resolved=False,
        verify_result={},
        post_fix_monitor_result={},
        triage_retrospective={},
        requires_human=False,
        resolution_summary="",
        total_cost=0,
        agent_costs={},
        summary="",
        ticket={},
    ))


# ── Flask webhook (Task 11 — Prometheus → auto-trigger) ──────────────────────
def run_webhook(port: int = 5001):
    """
    Alertmanager webhook receiver.
    Alertmanager POSTs here when a Prometheus rule fires.
    """
    from flask import Flask, request, jsonify
    import threading

    app = Flask("mlops-webhook")

    @app.route("/webhook/alert", methods=["POST"])
    def receive_alert():
        payload = request.get_json(force=True, silent=True) or {}
        alerts  = payload.get("alerts", [])
        log.info(f" Webhook received: {len(alerts)} alert(s)")

        for alert in alerts:
            if alert.get("status") != "firing":
                continue
            alert_name = alert.get("labels",{}).get("alertname","UnknownAlert")
            severity   = alert.get("labels",{}).get("severity","warning")
            log.info(f"→ Dispatching investigation: {alert_name} [{severity}]")
            t = threading.Thread(
                target=run_investigation,
                args=(alert_name, severity),
                daemon=True
            )
            t.start()

        return jsonify({"status": "accepted", "count": len(alerts)}), 202

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "mlops-agent-webhook"}), 200

    log.info(f" Webhook listening on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--webhook":
        run_webhook(port=5001)
    else:
        result = run_investigation("DataDriftDetected", "warning")
        print(f"\nTotal cost: ${sum(result.get('agent_costs',{}).values()):.4f}")