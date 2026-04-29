"""
Lambda HTTP handler — API compatível OpenAI no Bedrock (mantle), alinhado ao app Streamlit.
Rotas: POST /api/models, POST /api/completion
Suporta AWS Lambda Function URL e API Gateway HTTP API (payload v2).
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from openai import OpenAI


def bedrock_base_url(region: str) -> str:
    return f"https://bedrock-mantle.{region}.api.aws/v1"


def json_response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """
    Sem cabeçalhos CORS aqui: com Lambda Function URL, o CORS vem da configuração
    da URL; duplicar Access-Control-* no retorno gera ``*, *`` e o navegador bloqueia.
    """
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def parse_event(event: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    """Retorna (method, path_normalizado, body_dict)."""
    rc = event.get("requestContext") or {}
    http = rc.get("http") or {}
    method = (http.get("method") or event.get("httpMethod") or "GET").upper()

    raw = event.get("rawPath") or event.get("path") or "/"
    path = urllib.parse.unquote(raw.split("?")[0])

    # Remove prefixos comuns de stage (/prod, /dev)
    for prefix in ("/prod", "/dev", "/staging"):
        if path.startswith(prefix + "/"):
            path = path[len(prefix) :]
            break

    body_raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        body_raw = base64.b64decode(body_raw).decode("utf-8")

    try:
        parsed = json.loads(body_raw) if body_raw else {}
    except json.JSONDecodeError:
        return method, path.rstrip("/") or "/", None

    return method, path.rstrip("/") or "/", parsed if isinstance(parsed, dict) else {}


def handle_models(body: dict[str, Any]) -> dict[str, Any]:
    api_key = (body.get("api_key") or "").strip()
    region = (body.get("region") or "us-east-1").strip()
    if not api_key:
        return {"error": "Informe api_key."}

    client = OpenAI(api_key=api_key, base_url=bedrock_base_url(region))
    models = client.models.list()
    ids = [m.id for m in models.data]
    return {"models": ids}


def handle_completion(body: dict[str, Any]) -> dict[str, Any]:
    api_key = (body.get("api_key") or "").strip()
    region = (body.get("region") or "us-east-1").strip()
    model_id = (body.get("model_id") or "").strip()
    messages = body.get("messages")
    max_tokens = int(body.get("max_tokens") or 200)
    stream_collect = bool(body.get("stream_collect"))

    if not api_key:
        return {"error": "Informe api_key."}
    if not model_id:
        return {"error": "Informe model_id."}
    if not isinstance(messages, list) or not messages:
        return {"error": "Informe messages (lista não vazia)."}

    client = OpenAI(api_key=api_key, base_url=bedrock_base_url(region))

    if stream_collect:
        chunks: list[str] = []
        stream = client.chat.completions.create(
            model=model_id,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            piece = getattr(delta, "content", None)
            if piece:
                chunks.append(piece)
        full = "".join(chunks).strip()
        return {"chunks": chunks, "content": full}

    response = client.chat.completions.create(
        model=model_id,
        messages=messages,
        max_tokens=max_tokens,
    )
    msg = response.choices[0].message
    content = (msg.content or "") if msg else ""
    usage_dict = None
    if response.usage:
        usage_dict = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        }
    out: dict[str, Any] = {"content": content}
    if usage_dict:
        out["usage"] = usage_dict
    return out


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method, path, body = parse_event(event)

    if method == "OPTIONS":
        return {
            "statusCode": 204,
            "headers": {},
            "body": "",
        }

    if body is None:
        return json_response(400, {"error": "Corpo da requisição não é JSON válido."})

    # Normaliza path para roteamento
    route = path
    if route.endswith("/api/models") or route == "/api/models":
        if method != "POST":
            return json_response(405, {"error": "Use POST."})
        try:
            result = handle_models(body)
            status = 400 if result.get("error") else 200
            return json_response(status, result)
        except Exception as exc:  # noqa: BLE001
            return json_response(500, {"error": str(exc)})

    if route.endswith("/api/completion") or route == "/api/completion":
        if method != "POST":
            return json_response(405, {"error": "Use POST."})
        try:
            result = handle_completion(body)
            status = 400 if result.get("error") else 200
            return json_response(status, result)
        except Exception as exc:  # noqa: BLE001
            return json_response(500, {"error": str(exc)})

    return json_response(
        404,
        {"error": "Rota não encontrada.", "path": path, "method": method},
    )
