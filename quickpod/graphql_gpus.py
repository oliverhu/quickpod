"""GraphQL ``gpuTypes`` list with ``communityPrice`` (RunPod SDK omits pricing)."""

from __future__ import annotations

from typing import Any

from runpod.api.graphql import run_graphql_query

# See https://graphql-spec.dev.runpod.io/ — GpuType.communityPrice
QUERY_GPU_TYPES_WITH_PRICING = """
query GpuTypesPricing {
  gpuTypes {
    id
    displayName
    memoryInGb
    communityPrice
  }
}
"""


def fetch_gpu_types_with_pricing(*, api_key: str) -> list[dict[str, Any]]:
    raw = run_graphql_query(QUERY_GPU_TYPES_WITH_PRICING, api_key=api_key)
    types = raw.get("data", {}).get("gpuTypes")
    if not isinstance(types, list):
        return []
    return types
