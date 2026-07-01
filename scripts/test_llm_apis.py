#!/usr/bin/env python3
"""
Test all frontier LLM API keys stored in AWS SSM Parameter Store.
Usage: python3 scripts/test_llm_apis.py

Models tested:
  Claude      → claude-sonnet-4-6        (Investigator + Guardian)
  Gemini      → gemini-2.5-flash          (Data Scientist)
  OpenAI      → gpt-5.4-mini             (Operator)
  Perplexity  → sonar                     (Researcher)
"""
import boto3, requests, json, sys, time

# ── Pricing (per 1M tokens, USD) ─────────────────────────────────────────────
PRICING = {
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00},
    "gemini-2.5-flash":  {"input": 0.075, "output": 0.30},
    "gpt-5.4-mini":      {"input": 0.40,  "output": 1.60},
    "sonar":             {"input": 1.00,  "output": 1.00},  # per-request ~$0.005
}

# ── SSM helper ───────────────────────────────────────────────────────────────
ssm = boto3.client('ssm', region_name='us-east-2')

def get_key(name: str) -> str:
    try:
        return ssm.get_parameter(
            Name=f'/mlops/{name}', WithDecryption=True
        )['Parameter']['Value']
    except Exception as e:
        return f"SSM_ERROR: {e}"

# ── Individual tests ──────────────────────────────────────────────────────────
def test_claude() -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=get_key('anthropic-api-key'))
    t0 = time.time()
    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=30,
        messages=[{'role': 'user', 'content': 'Reply with exactly: CLAUDE_OK'}]
    )
    elapsed = time.time() - t0
    p = PRICING["claude-sonnet-4-6"]
    cost = (resp.usage.input_tokens * p["input"] +
            resp.usage.output_tokens * p["output"]) / 1_000_000
    return {
        "response":      resp.content[0].text.strip(),
        "model":         "claude-sonnet-4-6",
        "input_tokens":  resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cost_usd":      cost,
        "latency_s":     round(elapsed, 2),
    }

def test_gemini() -> dict:
    from google import genai
    client = genai.Client(api_key=get_key('google-api-key'))
    t0 = time.time()
    resp = client.models.generate_content(
        model='gemini-2.5-flash',
        contents='Reply with exactly: GEMINI_OK'
    )
    elapsed = time.time() - t0
    # gemini-2.5-flash token counts via usage_metadata
    meta = getattr(resp, 'usage_metadata', None)
    in_tok  = getattr(meta, 'prompt_token_count', 10) if meta else 10
    out_tok = getattr(meta, 'candidates_token_count', 5) if meta else 5
    p = PRICING["gemini-2.5-flash"]
    cost = (in_tok * p["input"] + out_tok * p["output"]) / 1_000_000
    return {
        "response":      resp.text.strip(),
        "model":         "gemini-2.5-flash",
        "input_tokens":  in_tok,
        "output_tokens": out_tok,
        "cost_usd":      cost,
        "latency_s":     round(elapsed, 2),
    }

def test_openai() -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=get_key('openai-api-key'))
    t0 = time.time()
    resp = client.chat.completions.create(
        model='gpt-5.4-mini',
        max_completion_tokens=30,
        messages=[{'role': 'user', 'content': 'Reply with exactly: OPENAI_OK'}]
    )
    elapsed = time.time() - t0
    p = PRICING["gpt-5.4-mini"]
    cost = (resp.usage.prompt_tokens * p["input"] +
            resp.usage.completion_tokens * p["output"]) / 1_000_000
    return {
        "response":      resp.choices[0].message.content.strip(),
        "model":         "gpt-5.4-mini",
        "input_tokens":  resp.usage.prompt_tokens,
        "output_tokens": resp.usage.completion_tokens,
        "cost_usd":      cost,
        "latency_s":     round(elapsed, 2),
    }

def test_perplexity() -> dict:
    key = get_key('perplexity-api-key')
    t0 = time.time()
    resp = requests.post(
        'https://api.perplexity.ai/chat/completions',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={'model': 'sonar', 'max_tokens': 30,
              'messages': [{'role': 'user',
                            'content': 'Reply with exactly: PERPLEXITY_OK'}]},
        timeout=15
    )
    elapsed = time.time() - t0
    resp.raise_for_status()
    data    = resp.json()
    text    = data['choices'][0]['message']['content'].strip()
    usage   = data.get('usage', {})
    in_tok  = usage.get('prompt_tokens', 10)
    out_tok = usage.get('completion_tokens', 5)
    p = PRICING["sonar"]
    cost = (in_tok * p["input"] + out_tok * p["output"]) / 1_000_000
    return {
        "response":      text,
        "model":         "sonar",
        "input_tokens":  in_tok,
        "output_tokens": out_tok,
        "cost_usd":      cost,
        "latency_s":     round(elapsed, 2),
    }

# ── Runner ────────────────────────────────────────────────────────────────────
def main():
    tests = [
        ("Claude",      "anthropic-api-key",  test_claude),
        ("Gemini",      "google-api-key",      test_gemini),
        ("OpenAI",      "openai-api-key",      test_openai),
        ("Perplexity",  "perplexity-api-key",  test_perplexity),
    ]

    print("\n=== LLM API Health Check ===\n")

    results  = []
    all_ok   = True

    for label, ssm_key, test_fn in tests:
        raw_key = get_key(ssm_key)
        if raw_key.startswith("SSM_ERROR") or "REPLACE_ME" in raw_key or "YOUR" in raw_key:
            print(f"   {label:12s} — Key missing or placeholder in SSM (/mlops/{ssm_key})")
            all_ok = False
            results.append({"label": label, "ok": False, "cost_usd": 0,
                            "latency_s": 0, "input_tokens": 0, "output_tokens": 0})
            continue

        try:
            data = test_fn()
            print(f"   {label:12s} {data['response']:<30s} "
                  f"[{data['model']}]  "
                  f"{data['latency_s']:.2f}s  "
                  f"↑{data['input_tokens']}tok ↓{data['output_tokens']}tok  "
                  f"${data['cost_usd']:.6f}")
            results.append({"label": label, "ok": True, **data})
        except Exception as e:
            print(f"   {label:12s} {str(e)[:100]}")
            all_ok = False
            results.append({"label": label, "ok": False, "cost_usd": 0,
                            "latency_s": 0, "input_tokens": 0, "output_tokens": 0})

    # ── Cost summary ──────────────────────────────────────────────────────────
    total_cost    = sum(r["cost_usd"] for r in results)
    total_in_tok  = sum(r["input_tokens"] for r in results)
    total_out_tok = sum(r["output_tokens"] for r in results)
    avg_latency   = sum(r["latency_s"] for r in results if r["ok"]) / max(sum(1 for r in results if r["ok"]), 1)

    print("\n" + "─" * 72)
    print("Cost Breakdown:")
    for r in results:
        status = "" if r["ok"] else ""
        print(f"  {status}  {r['label']:12s}  ${r['cost_usd']:.6f}  "
              f"({r['input_tokens']} in + {r['output_tokens']} out tokens)  "
              f"{r['latency_s']:.2f}s")
    print("─" * 72)
    print(f"  {'TOTAL':12s}  ${total_cost:.6f}  "
          f"({total_in_tok} in + {total_out_tok} out tokens)  "
          f"avg latency {avg_latency:.2f}s")
    print()

    # Extrapolate to agent run cost
    # A full investigation uses ~10x tokens vs this health check
    est_per_investigation = total_cost * 10
    print(f"  Estimated cost per full agent investigation: ~${est_per_investigation:.4f}")
    print(f"  Estimated cost per 100 investigations/day:  ~${est_per_investigation * 100:.2f}")
    print()

    overall = "All APIs healthy" if all_ok else "One or more APIs failed"
    print(overall)
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    main()